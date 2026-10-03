import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import pane_dispatch as d

SHA = 'a' * 40


class FakePane:
    def __init__(self):
        self.creates = []
        self.panes = []
        self.panels = []
        self.events = []
        self.failure = None
        self.cost = {'ok': True, 'panes': []}

    def __call__(self, *args, **kwargs):
        cmd = args[:2]
        if cmd == ('repos', 'list'):
            return {'repos': [dict(id=3, name='Repo', path='/repo', environment='macos')]}
        if args[0] == 'doctor':
            return {'daemon': {'reachable': True}}
        if args[0] == 'agent-context':
            return {'command': {'arguments': [{'name': n} for n in
                    ('--agent', '--tool-command', '--initial-input-file', '--branch', '--base-branch')]}}
        if cmd == ('agents', 'doctor'):
            return {'available': True}
        if cmd == ('panes', 'list'):
            return {'panes': self.panes}
        if cmd == ('panes', 'create'):
            self.creates.append(args)
            name = args[args.index('--name') + 1]
            self.panes = [dict(id='pane-1', name=name, ownership='pane', worktreePath='/repo/worktrees/task')]
            self.panels = [dict(id='panel-1', isCliPanel=True)]
            if self.failure:
                raise d.DispatchError(self.failure)
            return {'items': [dict(ok=True, paneId='pane-1', panelId='panel-1', worktreePath='/repo/worktrees/task',
                                   initialInput={'verifiedSubmitted': True, 'delivery': 'taken'})]}
        if cmd == ('panels', 'list'):
            return {'panels': self.panels}
        if args[0] == 'watch':
            return self.events
        if cmd == ('panes', 'cost'):
            return self.cost
        raise AssertionError(args)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = d.Store(Path(self.tmp.name) / 'private')
        self.fake = FakePane()
        for target, replacement in [('pane', self.fake),
                ('fresh_base', lambda repo: dict(remote='origin', ref='refs/heads/main', sha=SHA)),
                ('cli_info', lambda name: dict(cli=name, path='/safe bin/' + name, available=True)),
                ('executable', lambda name: '/safe/' + name)]:
            mock = patch.object(d, target, replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def start(self, prompt='Do task', cli='codex', auto=False):
        return d.start(self.store, 'task-1', 'Repo', cli, prompt, auto)

    def test_exact_duplicate_and_mismatch(self):
        first = self.start()
        self.assertEqual(self.start()['pane_id'], first['pane_id'])
        for prompt, cli in [('different', 'codex'), ('Do task', 'claude')]:
            with self.assertRaisesRegex(d.DispatchError, 'mismatch'):
                self.start(prompt, cli)
        self.assertEqual(len(self.fake.creates), 1)

    def test_pins_fetched_base_and_duplicate_never_refetches(self):
        first = self.start()
        args = self.fake.creates[0]
        self.assertEqual(args[args.index('--base-branch') + 1], SHA)
        with patch.object(d, 'fresh_base', side_effect=AssertionError('must not refetch')):
            self.assertEqual(self.start()['base'], first['base'])

    def test_fetch_failure_stops_before_intent_or_worker(self):
        with patch.object(d, 'fresh_base', side_effect=d.DispatchError('process_exit_128')):
            with self.assertRaisesRegex(d.DispatchError, 'process_exit_128'):
                self.start()
        self.assertIsNone(self.store.get('task-1'))
        self.assertFalse(self.fake.creates)

    def test_timeout_and_process_failure_reconcile_never_relaunch(self):
        for failure in ('process_timeout', 'process_exit_1'):
            with self.subTest(failure=failure):
                self.fake.failure = failure
                record = d.start(self.store, failure, 'Repo', 'grok', 'Task')
                self.assertEqual(record['state'], 'needs_inspection')
                self.assertEqual(record['pane_id'], 'pane-1')
                count = len(self.fake.creates)
                d.start(self.store, failure, 'Repo', 'grok', 'Task')
                self.assertEqual(len(self.fake.creates), count)

    def test_crash_before_creation_retains_intent(self):
        record = self.start()
        record.update(state='creation_unknown', pane_id=None, panel_id=None)
        self.store.save(record)
        self.fake.panes = []
        again = self.start()
        self.assertEqual(again['state'], 'creation_unknown')
        self.assertEqual(again['reconcile_error'], 'pane_not_found')
        self.assertEqual(len(self.fake.creates), 1)

    def test_no_selection_or_both_rejected(self):
        for cli, auto in [(None, False), ('codex', True)]:
            with self.assertRaisesRegex(d.DispatchError, 'select_explicit'):
                self.start(cli=cli, auto=auto)
        self.assertFalse(self.fake.creates)

    def test_manual_does_not_depend_on_quota(self):
        with patch.object(d, 'quota', side_effect=AssertionError('must not fetch')):
            self.assertEqual(self.start()['state'], 'submitted')

    def test_auto_fails_without_evidence(self):
        snapshot = {'clis': [dict(cli='codex', available=True, active_tasks=0,
                                quota=dict(usable=False, eligible=False))]}
        with patch.object(d, 'inventory', return_value=snapshot):
            with self.assertRaisesRegex(d.DispatchError, 'auto_requires'):
                self.start(cli=None, auto=True)
        self.assertFalse(self.fake.creates)

    def test_auto_records_selection_evidence_and_duplicate_does_not_select_again(self):
        snapshot = {'clis': [dict(cli='codex', available=True, active_tasks=0,
                                quota=dict(usable=True, eligible=True))]}
        with patch.object(d, 'inventory', return_value=snapshot) as inventory:
            first = self.start(cli=None, auto=True)
            again = self.start(cli=None, auto=True)
        self.assertEqual(inventory.call_count, 1)
        self.assertEqual(again['cli'], first['cli'])
        self.assertEqual(first['selection_evidence']['recommended_cli'], 'codex')

    def test_reconcile_ambiguous_names_never_launches(self):
        self.start()
        self.fake.panes.append(dict(self.fake.panes[0], id='pane-2'))
        result = self.start()
        self.assertEqual(result['reconcile_error'], 'multiple_matching_panes')
        self.assertEqual(len(self.fake.creates), 1)

    def test_unavailable_cli_does_not_create(self):
        with patch.object(d, 'cli_info', return_value={'available': False}):
            with self.assertRaisesRegex(d.DispatchError, 'unavailable'):
                self.start()
        self.assertFalse(self.fake.creates)

    def test_prompt_is_file_data_and_commands_have_no_bypass(self):
        prompt = '! touch /tmp/PWN\n$(touch /tmp/PWN) `echo bad` ; \' " --yolo\n你好'
        self.start(prompt)
        args = self.fake.creates[0]
        self.assertNotIn(prompt, args)
        self.assertEqual(shlex.split(args[args.index('--tool-command') + 1]), ['/safe bin/codex'])
        text = Path(args[args.index('--initial-input-file') + 1]).read_text()
        data = json.loads(text.split('as data, not shell syntax:\n')[1])
        self.assertEqual(data['source_user_task'], prompt)
        self.assertTrue(text.startswith('You are'))
        for cli in d.CLIS:
            path = "/tmp/bin '$() " + cli
            args = d.launch_args(cli, path, Path("/tmp/task ' $(evil).prompt"), 'session')
            command = shlex.split(args[args.index('--tool-command') + 1])
            self.assertEqual(command[0], path)
            self.assertFalse(set(command) & {'--yolo', '--force', '--trust', '--always-approve', '--auto',
                                              '--dangerously-skip-permissions'})

    def test_state_private_and_atomic(self):
        self.start()
        self.assertEqual(stat.S_IMODE(self.store.root.stat().st_mode), 0o700)
        for path in self.store.root.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(list(self.store.root.glob('.write-*')))

    def test_idle_exit_and_plain_report_are_not_success(self):
        self.start()
        for kind in ('agent.idle', 'agent.ready', 'panel.exited'):
            self.fake.events = [dict(paneId='pane-1', panelId='panel-1', kind=kind, exitCode=0)]
            record = d.status(self.store, 'task-1')
            self.assertNotEqual(record['state'], 'reported_ready')
            self.assertIsNone(record['evidence'])
        self.fake.panels[0]['report'] = dict(state='ready', head=SHA, pr=9, summary='Tests passed!')
        self.assertEqual(d.status(self.store, 'task-1')['state'], 'incomplete_report')
        self.fake.panels[0]['report']['state'] = 'done'
        self.assertEqual(d.status(self.store, 'task-1')['state'], 'incomplete_report')

    def test_watch_failure_does_not_hide_persistent_report(self):
        self.start()
        self.fake.panels[0]['report'] = dict(state='failed', summary='failure')
        original = self.fake

        def pane(*args, **kwargs):
            if args[0] == 'watch':
                raise d.DispatchError('watch_unavailable')
            return original(*args, **kwargs)

        with patch.object(d, 'pane', pane):
            result = d.status(self.store, 'task-1')
        self.assertEqual(result['state'], 'reported_failed')
        self.assertEqual(result['watch_error'], 'watch_unavailable')

    def test_journal_cursor_progresses_without_losing_previous_events(self):
        self.start()
        self.fake.events = [dict(gen=42, paneId='pane-1', panelId='panel-1', kind='agent.idle')]
        d.status(self.store, 'task-1')
        self.fake.events = []
        result = d.status(self.store, 'task-1')
        self.assertEqual(result['watch_cursor'], 42)
        self.assertEqual(result['events'][0]['gen'], 42)

    def test_result_evidence_and_session_pr_are_durable(self):
        self.start()
        summary = dict(task_id='task-1', head=SHA, pr_url='https://github.com/o/r/pull/9',
                       cli_session_id='cli-session', tests=[dict(command='python -m unittest', outcome='passed')])
        self.fake.panels[0]['report'] = dict(state='ready', head=SHA, pr=9, summary=json.dumps(summary))
        result = d.status(self.store, 'task-1')
        self.assertEqual(result['state'], 'reported_ready')
        self.assertFalse(result['evidence']['independently_verified'])
        self.assertEqual(self.store.get('task-1')['cli_session_id'], 'cli-session')
        self.assertEqual(result['pr']['head'], SHA)
        summary['tests'][0]['outcome'] = 'failed'
        self.fake.panels[0]['report']['summary'] = json.dumps(summary)
        self.assertEqual(d.status(self.store, 'task-1')['state'], 'incomplete_report')

    def test_other_panels_cannot_complete_task(self):
        self.start()
        self.fake.panels.append(dict(id='other', report=dict(state='ready', summary='unrelated')))
        self.fake.events = [dict(paneId='pane-1', panelId='other', kind='panel.exited')]
        self.assertEqual(d.status(self.store, 'task-1')['state'], 'submitted')

    def test_usage_missing_is_unknown_and_cost_is_estimate(self):
        self.start()
        with patch.object(d, 'quota', return_value={'error': 'unavailable'}):
            self.assertIsNone(d.usage(self.store, 'task-1')['estimated_cost_usd'])
            self.fake.cost = dict(panes=[dict(paneId='pane-1', messageCount=3,
                                 estimatedCostUsd=0, totalTokens=20, costIncomplete=True)])
            usage = d.usage(self.store, 'task-1')
            self.assertEqual(usage['estimated_cost_usd'], 0)
            self.assertTrue(usage['incomplete'])
            self.assertNotIn('budget', usage)


class QuotaTests(unittest.TestCase):
    def payload(self, used=20):
        return dict(provider='cursor', source='local', usage=dict(updatedAt=d.now(),
                    identity={'accountEmail': 'one@example.com'},
                    primary=dict(usedPercent=used, windowMinutes=300, resetsAt='2030-01-01T00:00:00Z')))

    def test_quota_parse_preserves_source_time_reset_and_no_credentials(self):
        row = self.payload()
        row['token'] = 'credential-must-not-appear'
        row['usage']['details'] = [{'secret': 'credential-must-not-appear'}]
        result = d.parse_quota('cursor', [row, row], d.now())
        self.assertEqual(len(result['accounts']), 1)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['accounts'][0]['source'], 'local')
        self.assertEqual(result['accounts'][0]['windows'][0]['resets_at'], '2030-01-01T00:00:00Z')
        self.assertNotIn('credential-must-not-appear', json.dumps(result))
        self.assertNotIn('one@example.com', json.dumps(result))

    def test_missing_invalid_stale_exhausted_and_multiple_accounts(self):
        for value in (None, -1, 101, float('nan'), '20', True):
            self.assertFalse(d.parse_quota('cursor', [self.payload(value)], d.now())['usable'])
        row = self.payload(100)
        self.assertFalse(d.parse_quota('cursor', [row], d.now())['eligible'])
        row['usage']['updatedAt'] = '2000-01-01T00:00:00Z'
        self.assertFalse(d.parse_quota('cursor', [row], d.now())['usable'])
        row = self.payload()
        row['account'] = 'second'
        row['usage']['identity']['accountEmail'] = 'second@example.com'
        self.assertFalse(d.parse_quota('cursor', [self.payload(), row], d.now())['usable'])

    def test_provider_errors_are_sanitized(self):
        row = self.payload()
        row['error'] = {'message': 'secret-credential'}
        result = d.parse_quota('cursor', [row], d.now())
        self.assertFalse(result['usable'])
        self.assertEqual(result['accounts'][0]['error'], 'provider_error')
        self.assertNotIn('secret-credential', json.dumps(result))

    def test_nonzero_quota_preserves_safe_snapshot_metadata(self):
        with patch.object(d, 'executable', return_value='/codexbar'), patch.object(d, 'run', return_value=([self.payload()], 1)):
            result = d.quota('cursor')
        self.assertFalse(result['eligible'])
        self.assertEqual(result['error'], 'process_exit_1')
        self.assertEqual(result['accounts'][0]['source'], 'local')

    def test_recommendation_waits_and_does_not_rank_quota_percentages(self):
        entries = [dict(cli=c, available=True, active_tasks=n, quota={'usable': True, 'eligible': True})
                   for c, n in [('codex', 2), ('claude', 0), ('grok', 1)]]
        result = d.recommendation({'clis': entries})
        self.assertEqual(result['recommended_cli'], 'claude')
        self.assertTrue(result['selection_required'])
        self.assertEqual(d.PROVIDERS['cursor'], 'cursor')
        self.assertEqual(d.PROVIDERS['grok'], 'grok')

    def test_grok_inventory_uses_xai_quota_not_cursor_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            def quota(provider):
                return dict(provider=provider, usable=True, eligible=provider != 'grok')

            with patch.object(d, 'pane', return_value={'repos': []}), \
                    patch.object(d, 'cli_info', side_effect=lambda cli: dict(cli=cli, available=cli == 'grok',
                                                                           quota_group=d.PROVIDERS[cli])), \
                    patch.object(d, 'quota', side_effect=quota) as fetch:
                snapshot = d.inventory(d.Store(directory))
        self.assertEqual(fetch.call_count, len(set(d.PROVIDERS.values())))
        self.assertEqual({call.args[0] for call in fetch.call_args_list}, set(d.PROVIDERS.values()))
        grok = next(row for row in snapshot['clis'] if row['cli'] == 'grok')
        self.assertEqual(grok['quota']['provider'], 'grok')
        self.assertFalse(grok['quota']['eligible'])
        self.assertIsNone(d.recommendation(snapshot, auto=True)['recommended_cli'])


class ProcessTests(unittest.TestCase):
    def test_custom_command_shell_quoting_prevents_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / "cli ' with spaces"
            cli.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            cli.chmod(0o700)
            marker = root / 'PWN'
            prompt = root / ("prompt ' $(touch " + str(marker).replace('/', '_') + ')')
            # Executing the shell form exercises the boundary Pane uses for custom commands.
            for name in ('agy', 'grok', 'opencode'):
                args = d.launch_args(name, str(cli), prompt, 'session')
                proc = subprocess.run(['/bin/sh', '-c', args[1]], cwd=root, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(str(prompt), json.loads(proc.stdout)[-1])
            self.assertEqual(list(root.iterdir()), [cli])

    def test_symlink_wrapper_loads_versioned_module(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / 'pane-dispatch'
            wrapper.symlink_to(Path(d.__file__).parent / 'bin/pane-dispatch')
            proc = subprocess.run([str(wrapper), '--help'], cwd=directory, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('inventory', proc.stdout)

    def test_subprocess_failure_suppresses_secrets(self):
        with patch.object(d.subprocess, 'run', return_value=subprocess.CompletedProcess([], 9, 'secret', 'secret')) as run:
            with self.assertRaisesRegex(d.DispatchError, '^process_exit_9$'):
                d.run(['/cli', '$(touch /tmp/bad)'])
            self.assertFalse(run.call_args.kwargs['shell'])
        with patch.object(d.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['secret'], 1)):
            with self.assertRaisesRegex(d.DispatchError, '^process_timeout$'):
                d.run(['/cli'])

    def test_custom_cli_help_required(self):
        with patch.object(d, 'executable', return_value='/bin/cli'), patch.object(d.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, 'unrecognized interface', '')
            self.assertFalse(d.cli_info('agy')['available'])
            run.return_value = subprocess.CompletedProcess([], 0, '--prompt-interactive', '')
            self.assertTrue(d.cli_info('agy')['available'])

    def test_json_argument_error(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = d.main(['start', '--task-id', 'one', '--repo', 'Repo', '--prompt-file', 'file'])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(stdout.getvalue())['ok'])


def concurrent_start(directory):
    store = d.Store(directory)
    fake = FakePane()
    original = fake.__call__

    def pane(*args, **kwargs):
        if args[:2] == ('panes', 'create'):
            with (Path(directory) / 'create-count').open('a') as stream:
                stream.write('create\n')
            time.sleep(0.1)
        return original(*args, **kwargs)

    with patch.object(d, 'pane', pane), patch.object(d, 'cli_info', return_value={'available': True, 'path': '/codex'}), \
            patch.object(d, 'fresh_base', return_value=dict(remote='origin', ref='refs/heads/main', sha=SHA)):
        d.start(store, 'same-task', 'Repo', 'codex', 'Prompt')


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_submission_creates_once(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = multiprocessing.get_context('spawn')
            processes = [ctx.Process(target=concurrent_start, args=(directory,)) for _ in range(4)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual((Path(directory) / 'create-count').read_text(), 'create\n')


class BaseTests(unittest.TestCase):
    def test_fetches_remote_default_even_when_local_default_is_stale_or_renamed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, seed, checkout = (root / name for name in ('remote.git', 'seed', 'checkout'))

            def git(path, *args):
                return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.PIPE, text=True).strip()

            git(root, 'init', '--bare', str(remote))
            git(remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
            git(root, 'init', '-b', 'main', str(seed))
            git(seed, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-m', 'initial')
            git(seed, 'remote', 'add', 'origin', str(remote))
            git(seed, 'push', 'origin', 'main')
            git(root, 'clone', str(remote), str(checkout))
            original = git(checkout, 'rev-parse', 'HEAD')
            (checkout / 'local.txt').write_text('keep this uncommitted file')
            git(seed, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-m', 'new upstream')
            git(seed, 'push', 'origin', 'main')
            new_head = git(seed, 'rev-parse', 'HEAD')
            self.assertNotEqual(original, new_head)
            result = d.fresh_base(str(checkout))
            self.assertEqual(result['sha'], new_head)
            self.assertEqual(result['ref'], 'refs/heads/main')

            git(seed, 'switch', '-c', 'trunk')
            git(seed, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-m', 'new default')
            git(seed, 'push', 'origin', 'trunk')
            git(remote, 'symbolic-ref', 'HEAD', 'refs/heads/trunk')
            result = d.fresh_base(str(checkout))
            self.assertEqual(result['sha'], git(seed, 'rev-parse', 'HEAD'))
            self.assertEqual(result['ref'], 'refs/heads/trunk')
            self.assertEqual(git(checkout, 'rev-parse', 'HEAD'), original)
            self.assertEqual(git(checkout, 'symbolic-ref', 'refs/remotes/origin/HEAD'), 'refs/remotes/origin/main')
            self.assertEqual((checkout / 'local.txt').read_text(), 'keep this uncommitted file')
            self.assertEqual(git(checkout, 'cat-file', '-t', result['sha']), 'commit')

    def test_unknown_remote_default_has_no_local_fallback(self):
        with patch.object(d, 'run', return_value=SHA + '\tHEAD\n') as run:
            with self.assertRaisesRegex(d.DispatchError, 'remote_default_unavailable'):
                d.fresh_base('/repo')
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
