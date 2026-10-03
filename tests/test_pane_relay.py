"""Conversation relay contract, including actual byte-driven menu state transitions."""
import json
import multiprocessing
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import pane_dispatch as d
import test_pane_dispatch as base
from test_pane_dispatch import FakePane


class RelayTests(unittest.TestCase):
    setUp = base.DispatchTests.setUp
    start = base.DispatchTests.start

    def waiting(self):
        self.start(cli='claude')
        self.fake.activity = 'idle'
        self.fake.message = ('Your message contained only pasted text.\n'
                             '1. Proceed?\n2. Draft or ready?\nReply "go".')
        return d.status(self.store, 'task-1')['conversation']['event_id']

    def test_already_idle_baseline_preserves_ordinary_question_without_report(self):
        self.waiting()
        result = d.wait(self.store, 'task-1', 1)
        self.assertEqual(result['outcome'], 'update')
        record = result['task']
        self.assertEqual(record['state'], 'needs_attention')
        self.assertIsNone(record['evidence'])
        self.assertEqual(record['conversation']['excerpt'], self.fake.message)
        self.assertIsNone(record['conversation']['question'])  # no invented semantics
        self.assertEqual(record['conversation']['options'], [])  # prose is not a TUI
        self.assertFalse(record['conversation']['new'])
        self.assertTrue(record['conversation']['replyable'])
        self.assertIn('pane= pane-1'.replace(' ', ''), record['pane_link'])

    def test_final_without_pr_never_success(self):
        self.waiting()
        self.fake.message = 'All done, tests passed.'
        record = d.status(self.store, 'task-1')
        self.assertEqual(record['state'], 'needs_attention')
        self.assertIsNone(record['pr'])

    def test_fallback_screen_is_evidence_not_invented_question_and_ignores_spinner(self):
        self.waiting()
        self.fake.message = ''
        self.fake.screen = 'Waiting for input\n✻ Working for 12s…'
        one = d.status(self.store, 'task-1')
        self.fake.screen = 'Waiting for input   \n✽ Working for 13s…'
        two = d.status(self.store, 'task-1')
        self.assertEqual(one['conversation']['event_id'], two['conversation']['event_id'])
        self.assertFalse(two['conversation']['new'])
        self.assertEqual(two['conversation']['provenance'], 'pane_terminal_screen')
        self.assertIsNone(two['conversation']['question'])
        self.fake.activity = 'active'
        self.assertFalse(d.status(self.store, 'task-1')['conversation']['replyable'])

    def test_reply_exactly_once_and_new_followup_and_resume_evidence(self):
        event = self.waiting()
        text = 'Proceed ready; literal $(echo untouched) and `not shell`.'
        result = d.reply(self.store, 'task-1', text, event, 'reply-1')
        self.assertEqual(result['delivery'], 'sent')
        self.assertIsNone(result['resume_evidence'])
        args = self.fake.sends[0]
        self.assertEqual(args[:2], ('panels', 'submit'))
        self.assertEqual(args[args.index('--panel') + 1], 'panel-1')
        self.assertEqual(Path(args[args.index('--input-file') + 1]).read_text(), text)
        self.assertEqual(d.reply(self.store, 'task-1', text, event, 'reply-1'), result)
        with self.assertRaisesRegex(d.DispatchError, 'mismatch'):
            d.reply(self.store, 'task-1', 'different', event, 'reply-1')
        with self.assertRaisesRegex(d.DispatchError, 'stale'):
            d.reply(self.store, 'task-1', text, event, 'reply-2')
        self.fake.activity = 'active'
        working = d.status(self.store, 'task-1')
        self.assertEqual(working['state'], 'working')
        self.assertIsNotNone(working['replies']['reply-1']['resume_evidence'])
        self.fake.activity = 'idle'
        # An old transcript surviving an activity cycle does not authorize another send.
        self.assertFalse(d.status(self.store, 'task-1')['conversation']['replyable'])
        self.fake.message = 'A new question: Which test environment?'
        followup = d.status(self.store, 'task-1')['conversation']
        self.assertNotEqual(followup['event_id'], event)
        self.assertTrue(followup['new'])
        d.reply(self.store, 'task-1', 'Local fixture', followup['event_id'], 'reply-3')
        self.assertEqual(len(self.fake.sends), 2)

    def test_wait_does_not_reemit_answered_transcript_on_busy_transition(self):
        event = self.waiting()
        d.reply(self.store, 'task-1', 'Proceed ready', event, 'r')
        self.fake.activity = 'active'
        self.fake.events = [dict(gen=8, kind='agent.busy', paneId='pane-1', panelId='panel-1')]
        original = self.fake
        waited = []
        def silent(*args, **kwargs):
            if args[0] == 'watch' and args[args.index('--timeout-ms') + 1] != '0':
                waited.append(True)
                raise d.DispatchError('process_timeout')
            return original(*args, **kwargs)
        with patch.object(d, 'pane', silent):
            result = d.wait(self.store, 'task-1', .1)
        self.assertEqual(result['outcome'], 'timeout')
        self.assertTrue(waited)
        conversation = result['task']['conversation']
        self.assertEqual(conversation['event_id'], event)
        self.assertTrue(conversation['consumed'])
        self.assertFalse(conversation['new'])
        self.assertFalse(conversation['replyable'])
        self.fake.message = 'Next question: red or blue?'
        self.fake.activity = 'idle'
        followup = d.wait(self.store, 'task-1', .1)
        self.assertEqual(followup['outcome'], 'update')
        self.assertTrue(followup['task']['conversation']['new'])
        self.assertFalse(followup['task']['conversation']['consumed'])

    def test_answered_transcript_does_not_hide_terminal_event(self):
        event = self.waiting()
        d.reply(self.store, 'task-1', 'Proceed ready', event, 'r')
        self.fake.events = [dict(gen=9, kind='panel.exited', paneId='pane-1', panelId='panel-1', exitCode=1)]
        result = d.wait(self.store, 'task-1', .1)
        self.assertEqual(result['outcome'], 'update')
        self.assertEqual(result['task']['state'], 'needs_attention')
        self.assertEqual(result['task']['terminal_event']['kind'], 'panel.exited')
        self.assertFalse(result['task']['conversation']['new'])
        self.assertFalse(result['task']['conversation']['replyable'])

    def test_receipt_artifacts_do_not_enter_task_scan(self):
        event = self.waiting()
        d.reply(self.store, 'task-1', 'Proceed ready', event, 'r')
        receipts = self.store.root / 'receipts'
        receipts.mkdir(mode=0o700)
        d.private_write(receipts / 'shell-completion.json', '{"shell_handle":"fixture"}')
        self.assertEqual([r['task_id'] for r in self.store.all()], ['task-1'])
        self.assertEqual(list(self.store.root.glob('*.json')), [self.store.path('task-1')])
        self.assertEqual([r['task_id'] for r in d.active(self.store)], ['task-1'])

    def test_stale_prompt_and_wrong_identity_rejected(self):
        event = self.waiting()
        self.fake.message = 'Changed choice: delete or keep?'
        with self.assertRaisesRegex(d.DispatchError, 'stale'):
            d.reply(self.store, 'task-1', 'go', event, 'r')
        for field, value in [('id', 'wrong-pane'), ('worktreePath', '/wrong'), ('repoId', 99)]:
            old = self.fake.panes[0].copy()
            self.fake.panes[0][field] = value
            with self.assertRaises(d.DispatchError):
                d.reply(self.store, 'task-1', 'go', event, 'r')
            self.fake.panes[0] = old
        self.fake.panels[0]['paneId'] = 'wrong-pane'
        with self.assertRaisesRegex(d.DispatchError, 'panel_identity_changed'):
            d.reply(self.store, 'task-1', 'go', event, 'r')
        self.assertEqual(self.fake.sends, [])

    def test_ambiguous_send_and_crash_intent_never_retry(self):
        event = self.waiting()
        self.fake.send_failure = 'process_timeout'
        result = d.reply(self.store, 'task-1', 'go', event, 'r')
        self.assertEqual(result['delivery'], 'unknown')
        self.fake.send_failure = None
        self.assertEqual(d.reply(self.store, 'task-1', 'go', event, 'r'), result)
        with self.assertRaisesRegex(d.DispatchError, 'stale'):
            d.reply(self.store, 'task-1', 'go', event, 'different-id')
        self.assertEqual(len(self.fake.sends), 1)
        self.fake.message = 'Follow-up?'
        event = d.status(self.store, 'task-1')['conversation']['event_id']
        original = self.fake
        def crash(*args, **kwargs):
            if args[:2] == ('panels', 'submit'):
                self.assertEqual(self.store.get('task-1')['replies']['crash']['delivery'], 'unknown')
                raise KeyboardInterrupt()
            return original(*args, **kwargs)
        with patch.object(d, 'pane', crash), self.assertRaises(KeyboardInterrupt):
            d.reply(self.store, 'task-1', 'go', event, 'crash')
        self.assertEqual(d.reply(self.store, 'task-1', 'go', event, 'crash')['delivery'], 'unknown')
        self.assertEqual(len(self.fake.sends), 1)

    def test_report_question_not_resurrected_after_reply(self):
        self.waiting()
        self.fake.panels[0]['report'] = dict(state='blocked', question='Choose policy?', summary='Waiting')
        before = d.status(self.store, 'task-1')
        self.assertEqual(before['conversation']['question'], 'Choose policy?')
        d.reply(self.store, 'task-1', 'Keep', before['conversation']['event_id'], 'r')
        after = d.status(self.store, 'task-1')
        self.assertFalse(after['conversation']['replyable'])
        self.assertFalse(after['conversation']['new'])
        self.assertEqual(after['state'], 'awaiting_reply_evidence')
        self.fake.message = 'Next policy question?'
        after = d.status(self.store, 'task-1')
        self.assertEqual(after['conversation']['excerpt'], self.fake.message)
        self.assertTrue(after['conversation']['replyable'])

    def test_direct_resume_supersedes_persistent_blocked_report(self):
        self.waiting()
        self.fake.panels[0]['report'] = dict(state='blocked', question='Choose policy?', summary='Waiting')
        blocked = d.status(self.store, 'task-1')
        event = blocked['conversation']['event_id']
        # A human resumes the panel directly: no dispatcher reply/consumption receipt.
        self.fake.activity = 'active'
        self.fake.message = 'Applying the selected policy and running tests.'
        with self.assertRaisesRegex(d.DispatchError, 'stale'):
            d.reply(self.store, 'task-1', 'Keep', event, 'late-reply')
        working = d.status(self.store, 'task-1')
        self.assertEqual(working['state'], 'working')
        self.assertEqual(working['conversation']['excerpt'], self.fake.message)
        self.assertIsNone(working['conversation']['question'])
        self.assertFalse(working['conversation']['replyable'])
        self.assertTrue(working['evidence']['superseded'])
        self.assertEqual(working['evidence']['question'], 'Choose policy?')
        self.assertFalse(self.fake.sends)
        self.fake.activity = 'idle'
        self.fake.message = 'Next question: red or blue?'
        followup = d.wait(self.store, 'task-1', .1)['task']
        self.assertEqual(followup['state'], 'needs_attention')
        self.assertEqual(followup['conversation']['excerpt'], self.fake.message)
        self.assertTrue(followup['conversation']['replyable'])
        self.assertNotEqual(followup['conversation']['event_id'], event)
        # A genuinely replaced report may supply a new question once idle.
        self.fake.panels[0]['report'] = dict(state='blocked', question='New report question?', summary='Waiting')
        fresh = d.status(self.store, 'task-1')
        self.assertEqual(fresh['state'], 'reported_blocked')
        self.assertEqual(fresh['conversation']['question'], 'New report question?')
        self.assertTrue(fresh['conversation']['replyable'])

    def test_first_observation_of_active_panel_cannot_authorize_old_report(self):
        self.waiting()
        self.fake.panels[0]['report'] = dict(state='blocked', question='Proceed?', summary='Old report')
        self.fake.activity = 'active'
        self.fake.message = 'Already running tests.'
        working = d.wait(self.store, 'task-1', .1)['task']
        self.assertEqual(working['state'], 'working')
        self.assertEqual(working['conversation']['provenance'], 'pane_agent_last_message')
        self.assertFalse(working['conversation']['replyable'])
        self.assertIsNone(working['conversation']['question'])

    def test_menu_bytes_change_selection_before_confirmation(self):
        self.waiting()
        selected = 0
        accepted = []
        def render():
            self.fake.screen = '\n'.join(('❯ ' if i == selected else '  ') + str(i+1) + '. ' + label
                                         for i, label in enumerate(('Draft', 'Ready')))
        render()
        original = self.fake
        def menu(*args, **kwargs):
            nonlocal selected
            if args[:2] == ('panels', 'input'):
                key = args[args.index('--text') + 1]
                if key == '\x1b[B':
                    selected = min(1, selected + 1)
                elif key == '\x1b[A':
                    selected = max(0, selected - 1)
                elif key == '\r':
                    accepted.append(selected)
                    self.fake.screen = 'Selection delivered'
                    self.fake.message = ''
                    self.fake.activity = 'active'
                    return dict(ok=True)
                render()
            return original(*args, **kwargs)
        with patch.object(d, 'pane', menu):
            first = d.status(self.store, 'task-1')['conversation']
            self.assertEqual(first['provenance'], 'pane_terminal_screen')
            with self.assertRaisesRegex(d.DispatchError, 'menu_requires'):
                d.reply(self.store, 'task-1', 'Ready', first['event_id'], 'text')
            d.reply(self.store, 'task-1', 'Select Ready', first['event_id'], 'down', 'down')
            second = d.status(self.store, 'task-1')['conversation']
            self.assertNotEqual(first['event_id'], second['event_id'])
            with self.assertRaisesRegex(d.DispatchError, 'stale'):
                d.reply(self.store, 'task-1', 'Select Ready', first['event_id'], 'stale-enter', 'enter')
            d.reply(self.store, 'task-1', 'Select Ready', second['event_id'], 'enter', 'enter')
            self.assertEqual(accepted, [1])
            self.assertEqual(d.status(self.store, 'task-1')['state'], 'working')
        with self.assertRaisesRegex(d.DispatchError, 'invalid_menu_key'):
            d.reply(self.store, 'task-1', 'go', first['event_id'], 'bad', 'ctrl-c')

    def test_boundary_navigation_keeps_same_event_available_for_enter(self):
        self.waiting()
        self.fake.screen = '> 1. Blue\n  2. Green'
        event = d.status(self.store, 'task-1')['conversation']['event_id']
        nav = d.reply(self.store, 'task-1', 'Choose Blue', event, 'up-at-first', 'up')
        self.assertEqual(d.reply(self.store, 'task-1', 'Choose Blue', event, 'up-at-first', 'up'), nav)
        current = d.status(self.store, 'task-1')['conversation']
        self.assertEqual(current['event_id'], event)
        self.assertTrue(current['replyable'])
        self.assertFalse(current['consumed'])
        submitted = d.reply(self.store, 'task-1', 'Choose Blue', event, 'confirm-blue', 'enter')
        self.assertEqual(nav['action'], 'navigate')
        self.assertEqual(submitted['action'], 'submit')
        self.assertEqual(d.reply(self.store, 'task-1', 'Choose Blue', event, 'confirm-blue', 'enter'), submitted)
        self.assertEqual(len(self.fake.sends), 2)
        self.assertEqual([args[args.index('--text') + 1] for args in self.fake.sends], ['\x1b[A', '\r'])
        with self.assertRaisesRegex(d.DispatchError, 'stale'):
            d.reply(self.store, 'task-1', 'Choose Blue', event, 'second-enter', 'enter')

    def test_ambiguous_navigation_does_not_enable_enter_or_retry(self):
        self.waiting()
        self.fake.screen = '> 1. Blue\n  2. Green'
        event = d.status(self.store, 'task-1')['conversation']['event_id']
        self.fake.send_failure = 'process_timeout'
        nav = d.reply(self.store, 'task-1', 'Choose Blue', event, 'up', 'up')
        self.assertEqual(nav['delivery'], 'unknown')
        self.assertEqual(d.reply(self.store, 'task-1', 'Choose Blue', event, 'up', 'up'), nav)
        with self.assertRaisesRegex(d.DispatchError, 'stale'):
            d.reply(self.store, 'task-1', 'Choose Blue', event, 'enter-after-unknown', 'enter')
        self.assertEqual(len(self.fake.sends), 1)

    def test_shell_held_input_and_wrong_read_target_are_not_replyable(self):
        event = self.waiting()
        original = self.fake
        for change in ('shell', 'held', 'wrong-target'):
            def altered(*args, **kwargs):
                result = original(*args, **kwargs)
                if args[:2] == ('panels', 'screen'):
                    if change == 'shell':
                        result['state']['isCliPanel'] = False
                    elif change == 'held':
                        result['composer'] = dict(hasUndeliveredText=True)
                    else:
                        result['paneId'] = 'other'
                return result
            with patch.object(d, 'pane', altered), self.assertRaises(d.DispatchError):
                d.reply(self.store, 'task-1', 'go', event, change)
        self.assertFalse(self.fake.sends)

    def test_numeric_menu_and_return_to_previous_selection_has_new_event(self):
        self.waiting()
        self.fake.screen = 'Select an option, press its number.\n❯ 1. Draft\n  2. Ready'
        original = self.fake
        def numeric(*args, **kwargs):
            if args[:2] == ('panels', 'input'):
                value = args[args.index('--text') + 1]
                self.fake.screen = ('Select an option, press its number.\n  1. Draft\n❯ 2. Ready' if value == '2'
                                    else 'Select an option, press its number.\n❯ 1. Draft\n  2. Ready')
            return original(*args, **kwargs)
        with patch.object(d, 'pane', numeric):
            first = d.status(self.store, 'task-1')['conversation']['event_id']
            d.reply(self.store, 'task-1', 'Choose Ready', first, 'two', '2')
            second = d.status(self.store, 'task-1')['conversation']['event_id']
            d.reply(self.store, 'task-1', 'Choose Draft', second, 'up', 'up')
            third = d.status(self.store, 'task-1')['conversation']
            self.assertNotEqual(first, third['event_id'])
            self.assertTrue(third['replyable'])
            with self.assertRaisesRegex(d.DispatchError, 'not_present'):
                d.reply(self.store, 'task-1', 'Choose nine', third['event_id'], 'nine', '9')

    def test_wait_timeout_releases_locks_and_journal_wakeup(self):
        self.waiting()
        self.fake.activity = 'active'
        d.status(self.store, 'task-1')
        original = self.fake
        def wake(*args, **kwargs):
            if args[0] == 'watch' and args[args.index('--timeout-ms') + 1] != '0':
                # Would deadlock if wait held either task/global state lock.
                with self.store.locked(), self.store.locked('task-1'):
                    self.fake.activity = 'idle'
                    self.fake.message = 'Already waiting now?'
                return [dict(gen=7, kind='agent.ready', paneId='pane-1', panelId='panel-1')]
            return original(*args, **kwargs)
        with patch.object(d, 'pane', wake):
            self.assertEqual(d.wait(self.store, 'task-1', 1)['task']['state'], 'needs_attention')
        self.fake.activity = 'active'
        d.status(self.store, 'task-1')
        def silent(*args, **kwargs):
            if args[0] == 'watch' and args[args.index('--timeout-ms') + 1] != '0':
                time.sleep(.03)
                raise d.DispatchError('process_timeout')
            return original(*args, **kwargs)
        with patch.object(d, 'pane', silent):
            self.assertEqual(d.wait(self.store, 'task-1', .03)['outcome'], 'timeout')
        for timeout in (0, -1, 46, float('nan')):
            with self.assertRaises(d.DispatchError):
                d.wait(self.store, 'task-1', timeout)

    def test_claude_argument_launch_and_readiness_precedence(self):
        result = d.start(self.store, 'task-1', 'Repo', 'claude', 'Open non-draft PR; mark ready', pr_mode='draft')
        import shlex
        args = shlex.split(result['launch_command'])
        self.assertTrue(args[-1].startswith('Please carry out the user-authorized task'))
        self.assertNotIn('--initial-input-file', self.fake.creates[0])
        prompt = (self.store.root / (d.digest('task-1') + '.prompt')).read_text()
        self.assertIn('source user task controls draft/ready', prompt)
        self.assertNotIn('open a DRAFT PR', prompt)
        self.assertIn('Open non-draft PR; mark ready', prompt)
        self.assertEqual(result['pr_mode'], 'draft')
        self.assertEqual(d.active(self.store)[0]['task_id'], 'task-1')


def concurrent_reply(directory, event):
    store = d.Store(directory)
    fake = FakePane()
    record = store.get('task-1')
    fake.panes = [dict(id='pane-1', name=record['pane_name'], ownership='pane', worktreePath=record['worktree'])]
    fake.panels = [dict(id='panel-1')]
    fake.message, fake.activity = 'Question?', 'idle'
    def call(*args, **kwargs):
        if args[:2] == ('panels', 'submit'):
            with (Path(directory) / 'send-count').open('a') as stream:
                stream.write('send\n')
            time.sleep(.05)
        return fake(*args, **kwargs)
    with patch.object(d, 'pane', call):
        d.reply(store, 'task-1', 'Go', event, 'same-reply')


class RelayConcurrencyTests(unittest.TestCase):
    def test_colon_pairs_cannot_overwrite_another_tasks_reply_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = d.Store(directory)
            for task, panel in [('a:b', 'first-panel'), ('a', 'second-panel')]:
                store.save(dict(task_id=task, state='needs_attention', pane_id=panel + '-pane',
                                panel_id=panel, conversation=dict(event_id=panel, replyable=True,
                                options=[], report_key=None, content_signature=panel)))
            delivered = {}
            paths = {}
            def submit(*args, **kwargs):
                self.assertEqual(args[:2], ('panels', 'submit'))
                panel = args[args.index('--panel') + 1]
                path = Path(args[args.index('--input-file') + 1])
                paths[panel] = path
                if panel == 'first-panel':
                    # Interleave while the first task lock is held, before Pane reads
                    # its file. A different task lock allows the second full send.
                    d.reply(store, 'a', 'Second user instruction', 'second-panel', 'b:c')
                delivered[panel] = path.read_text()
                return dict(ok=True, verifiedSubmitted=True)
            # Identity validation is covered separately; freeze both validated prompts
            # to isolate the cross-task file lifecycle under real Store locks.
            with patch.object(d, 'observe'), patch.object(d, 'pane', submit):
                first = d.reply(store, 'a:b', 'First user instruction', 'first-panel', 'c')
                self.assertEqual(d.reply(store, 'a:b', 'First user instruction', 'first-panel', 'c'), first)
            self.assertEqual(delivered, {'first-panel': 'First user instruction',
                                         'second-panel': 'Second user instruction'})
            self.assertNotEqual(paths['first-panel'], paths['second-panel'])
            self.assertEqual(len(list(store.root.glob('*.reply'))), 2)

    def test_four_processes_deliver_same_reply_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = d.Store(directory)
            fake = FakePane()
            with patch.object(d, 'pane', fake), patch.object(d, 'cli_info', return_value=dict(available=True, path='/codex')):
                d.start(store, 'task-1', 'Repo', 'codex', 'Task')
                fake.message, fake.activity = 'Question?', 'idle'
                event = d.status(store, 'task-1')['conversation']['event_id']
            ctx = multiprocessing.get_context('spawn')
            processes = [ctx.Process(target=concurrent_reply, args=(directory, event)) for _ in range(4)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual((Path(directory) / 'send-count').read_text(), 'send\n')

def hold_task_lock(directory, ready, release):
    with d.Store(directory).locked('contended'):
        ready.set()
        release.wait(3)


class DeadlineTests(unittest.TestCase):
    def test_contended_task_lock_obeys_total_wait_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            store = d.Store(directory)
            store.save(dict(task_id='contended', state='submitted'))
            ctx = multiprocessing.get_context('spawn')
            ready, release = ctx.Event(), ctx.Event()
            process = ctx.Process(target=hold_task_lock, args=(directory, ready, release))
            process.start()
            try:
                self.assertTrue(ready.wait(3))
                started = time.monotonic()
                with patch.object(d, 'pane', side_effect=AssertionError('must not reach Pane while lock is held')):
                    result = d.wait(store, 'contended', .15)
                elapsed = time.monotonic() - started
                self.assertEqual(result['outcome'], 'timeout')
                self.assertIsNone(result['task'])
                self.assertGreaterEqual(elapsed, .1)
                self.assertLess(elapsed, 1)
                self.assertTrue(process.is_alive())
                self.assertIsNone(d.DEADLINE.get())
            finally:
                release.set()
                process.join(4)
                if process.is_alive():
                    process.kill()
                    process.join()
            self.assertEqual(process.exitcode, 0)

    def test_real_subprocess_is_killed_at_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            store = d.Store(directory)
            store.save(dict(task_id='slow', pane_name='slow', repo_id=3, state='submitted'))
            sleeper = Path(directory) / 'slow-runpane'
            sleeper.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n')
            sleeper.chmod(0o700)
            started = time.monotonic()
            with patch.object(d, 'executable', return_value=str(sleeper)):
                result = d.wait(store, 'slow', .1)
            self.assertEqual(result['outcome'], 'timeout')
            self.assertLess(time.monotonic() - started, 1)

class InertMenuFixtureTests(unittest.TestCase):
    def test_real_pty_arrow_numeric_and_enter_transitions(self):
        import os
        import pty
        import select
        import sys
        master, slave = pty.openpty()
        fixture = Path(__file__).parent / 'fixtures' / 'relay_menu.py'
        process = subprocess.Popen([sys.executable, '-u', str(fixture)], stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        self.addCleanup(os.close, master)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
        self.addCleanup(cleanup)
        def expect(text):
            output = b''
            deadline = time.monotonic() + 3
            while text.encode() not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        output += os.read(master, 8192)
                    except OSError:
                        break
            self.assertIn(text.encode(), output)
        expect('Selected: Blue | Keys received: 0')
        for key, text in [(b'\x1b[A', 'Selected: Blue | Keys received: 1'),
                          (b'\x1b[B', 'Selected: Green | Keys received: 2'),
                          (b'\x1b[A', 'Selected: Blue | Keys received: 3'),
                          (b'2', 'Selected: Green | Keys received: 4'),
                          (b'\r', 'FIXTURE CONFIRMED: Green | Keys received: 5')]:
            os.write(master, key)
            expect(text)
        self.assertEqual(process.wait(timeout=2), 0)
