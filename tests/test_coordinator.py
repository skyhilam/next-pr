from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from next_pr import engine, github, providers, runner
from next_pr.cli import activation_gate, main, recover
from next_pr.common import Blocked, atomic_json, lock, lock_held, now, read_json
from next_pr.state import Store, initial_config, overlaps, repo_config
from next_pr.ui import Handler, PAGE, snapshot, transcript

SHA = 'a' * 40
OTHER = 'b' * 40
REPO = 'skyhilam/dkdm-monorepo'


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.cfg = initial_config(str(self.home / 'repo'))
        self.cfg['repos'][REPO]['validation'] = [sys.executable, '-c', 'pass']
        for value in self.cfg['providers'].values():
            value.update(enabled=True, billing_confirmed=True, config_audited=True)
        atomic_json(self.home / 'config.json', self.cfg)

    def task(self, scope='api', key='one'):
        task = self.store.submit(REPO, 'Test', 'Do the work', key, [scope], [])
        task.update(stage='coding', branch='next-pr/task-' + task['id'], base='main',
                    worktree=str(self.home / 'worktree'), pr_number=1)
        self.store.save(task)
        return task

    def record_run(self, task, role='writer', result=None, receipt=None):
        run_id = str(len(self.store.runs()))
        directory = self.home / 'runs' / run_id
        directory.mkdir(parents=True)
        record = dict(id=run_id, task_id=task['id'], role=role, head_sha=SHA,
                      provider='claude' if role == 'review' else task['writer'], consumed=False,
                      directory=str(directory), created_at=now() - 20)
        if result is not None:
            (directory / 'stdout.log').write_text(json.dumps({'type': 'result',
                'session_id': 'session-123', 'usage': {'input_tokens': 12}, 'result': json.dumps(result)}))
        if receipt is not None:
            atomic_json(directory / 'receipt.json', dict(run_id=run_id, exit_code=0,
                         safe_to_retry=True, cancelled=False, **receipt))
        self.store.save_run(record)
        task['run_id'] = run_id
        self.store.save(task)
        return record

    def result(self, status='completed', head=SHA, blockers=None):
        return dict(status=status, head_sha=head, summary='Done', tests=['unit tests passed'],
                    blockers=blockers or [])


class StateTests(Fixture):
    def test_idempotent_request_and_conflicting_reuse(self):
        one = self.task()
        again = self.store.submit(REPO, 'Test', 'Do the work', 'one', ['api'], [])
        self.assertEqual(one['id'], again['id'])
        with self.assertRaises(Blocked):
            self.store.submit(REPO, 'Test', 'different prompt', 'one', ['api'], [])
        self.assertEqual(len(self.store.tasks()), 1)

    def test_overlap_requires_explicit_dependency_even_if_mergeable(self):
        first = self.task()
        first['stage'] = 'mergeable'
        self.store.save(first)
        with self.assertRaises(Blocked):
            self.store.submit(REPO, 'Test', 'Do work', 'two', ['api/app'], [])
        second = self.store.submit(REPO, 'Test', 'Do work', 'two', ['api/app'], [first['id']])
        self.assertEqual(second['writer'], 'claude')
        self.assertTrue(overlaps(['*'], ['web']))
        self.assertFalse(overlaps(['api'], ['api-other']))

    def test_real_git_origin_allowlist(self):
        path = self.home / 'repo'
        subprocess.run(['git', 'init', str(path)], capture_output=True, check=True)
        subprocess.run(['git', '-C', str(path), 'remote', 'add', 'origin',
                        'https://github.com/evil/repo.git'], check=True)
        with self.assertRaisesRegex(Blocked, 'origin'):
            repo_config(self.cfg, REPO)
        with self.assertRaisesRegex(Blocked, 'allowlist'):
            repo_config(self.cfg, 'someone/else')


class LifecycleTests(Fixture):
    @patch('next_pr.github.reconciled_head', return_value=SHA)
    def test_writer_validation_review_consumption(self, _head):
        task = self.task()
        record = self.record_run(task, result=self.result(), receipt={})
        engine.consume(self.store, task, record)
        task = self.store.task(task['id'])
        self.assertEqual(task['stage'], 'validation')
        self.assertEqual(self.store.runs()[0]['session_id'], 'session-123')
        record = self.record_run(task, role='validation', receipt={})
        engine.consume(self.store, task, record)
        task = self.store.task(task['id'])
        self.assertEqual(task['stage'], 'review')
        record = self.record_run(task, role='review', result=self.result('no_blockers'), receipt={})
        engine.consume(self.store, task, record)
        task = self.store.task(task['id'])
        self.assertEqual(task['stage'], 'ready')
        self.assertEqual(task['review_sha'], SHA)
        with patch('next_pr.engine.repo_config', return_value=self.cfg['repos'][REPO]), \
             patch('next_pr.github.ready') as ready:
            engine.tick(self.store)
            ready.assert_called_once()
        self.assertEqual(len(self.store.task(task['id'])['evidence']), 3)
        self.assertTrue(all(run['consumed'] for run in self.store.runs()))

    def test_missing_receipt_blocks_restart_and_handoff(self):
        task = self.task()
        record = self.record_run(task)
        engine.consume(self.store, task, record)
        task = self.store.task(task['id'])
        self.assertEqual(task['stage'], 'blocked')
        self.assertEqual(task['run_id'], record['id'])
        with self.assertRaisesRegex(Blocked, 'possibly live'):
            engine.resume_task(self.store, self.cfg, task, 'claude')

    @patch('next_pr.github.reconciled_head', return_value=SHA)
    @patch('next_pr.providers.gate')
    def test_missing_receipt_recovery_preserves_resume_and_handoff_stage(self, _gate, _head):
        for handoff in (None, 'claude'):
            with self.subTest(handoff=handoff):
                task = self.task(scope=str(handoff), key=str(handoff))
                task['writer'] = 'codex'
                record = self.record_run(task)
                engine.consume(self.store, task, record)
                task = self.store.task(task['id'])
                self.assertEqual(task['resume_stage'], 'coding')
                task = recover(self.store, task)
                self.assertEqual(task['stage'], 'blocked')
                self.assertEqual(task['resume_stage'], 'coding')
                self.assertIsNone(task['run_id'])
                self.assertTrue(self.store.runs(task['id'])[0]['consumed'])
                engine.resume_task(self.store, self.cfg, task, handoff)
                task = self.store.task(task['id'])
                self.assertEqual(task['stage'], 'coding')
                if handoff:
                    self.assertEqual(task['writer'], handoff)
                self.assertFalse(task['paused'])

    def test_live_lock_survives_manager_restart(self):
        task = self.task()
        record = self.record_run(task)
        with lock(Path(record['directory']) / 'run.lock'):
            engine.consume(self.store, task, record)
            self.assertEqual(self.store.task(task['id'])['stage'], 'coding')

    @patch('next_pr.github.reconciled_head', return_value=SHA)
    def test_rate_limit_pauses_provider_and_checkpoints(self, _head):
        task = self.task()
        record = self.record_run(task, result=self.result('rate_limited'), receipt={})
        engine.consume(self.store, task, record)
        task = self.store.task(task['id'])
        self.assertIsNone(task['run_id'])
        self.assertEqual(task['stage'], 'blocked')
        self.assertIn('codex', self.store.meta('provider_pauses'))
        with patch('next_pr.providers.gate'):
            engine.resume_task(self.store, self.cfg, task, 'claude')
        self.assertEqual(self.store.task(task['id'])['writer'], 'claude')

    @patch('next_pr.github.reconciled_head', return_value=OTHER)
    def test_stale_review_is_never_ready(self, _head):
        task = self.task()
        task.update(stage='review', validated_sha=SHA)
        record = self.record_run(task, role='review', result=self.result('no_blockers', OTHER), receipt={})
        engine.consume(self.store, task, record)
        self.assertEqual(self.store.task(task['id'])['stage'], 'validation')
        self.assertIsNone(self.store.task(task['id'])['review_sha'])

    @patch('next_pr.github.reconciled_head', return_value=SHA)
    def test_three_fix_round_cap(self, _head):
        task = self.task()
        for count in range(3):
            engine.request_fix(task, 'Fix blocker')
            self.assertEqual(task['fix_round'], count + 1)
        with self.assertRaisesRegex(Blocked, 'three fix'):
            engine.request_fix(task, 'Still broken')

    def test_cancel_only_requests_supervisor_stop_and_retains_slot(self):
        task = self.task()
        record = self.record_run(task)
        with patch('os.kill') as kill:
            engine.stop_task(self.store, task, cancel=True)
            kill.assert_not_called()
        self.assertTrue((Path(record['directory']) / 'stop.json').exists())
        self.assertEqual(self.store.task(task['id'])['run_id'], record['id'])
        atomic_json(Path(record['directory']) / 'receipt.json', dict(run_id=record['id'],
                    exit_code=-15, safe_to_retry=True, cancelled=True))
        engine.consume(self.store, task, record)
        self.assertEqual(self.store.task(task['id'])['stage'], 'cancelled')

    def test_pause_without_active_run(self):
        task = self.task()
        engine.stop_task(self.store, task)
        self.assertTrue(self.store.task(task['id'])['paused'])
        with patch('next_pr.github.reconciled_head', return_value=SHA):
            engine.resume_task(self.store, self.cfg, task)
        self.assertFalse(self.store.task(task['id'])['paused'])

    def test_surviving_descendants_block_handoff(self):
        task = self.task()
        record = self.record_run(task)
        atomic_json(Path(record['directory']) / 'receipt.json', dict(run_id=record['id'],
                    safe_to_retry=False, exit_code=0))
        engine.consume(self.store, task, record)
        self.assertEqual(self.store.task(task['id'])['run_id'], record['id'])

    def test_developer_concurrency_and_dependency_wait(self):
        tasks = [self.task(scope=f'part{i}', key=str(i)) for i in range(3)]
        dispatched = []
        def start(store, cfg, task, role):
            dispatched.append((task['id'], role))
        with patch('next_pr.engine.repo_config', return_value=self.cfg['repos'][REPO]), \
             patch('next_pr.engine.start_run', side_effect=start):
            engine.tick(self.store)
        self.assertEqual(dispatched, [(tasks[0]['id'], 'writer'), (tasks[1]['id'], 'writer')])
        tasks[0]['stage'] = 'blocked'
        self.store.save(tasks[0])
        tasks[1]['stage'] = 'blocked'
        self.store.save(tasks[1])
        tasks[2]['dependencies'] = [tasks[0]['id']]
        self.store.save(tasks[2])
        with patch('next_pr.engine.repo_config', return_value=self.cfg['repos'][REPO]), \
             patch('next_pr.engine.start_run') as start:
            engine.tick(self.store)
            start.assert_not_called()

    def test_one_reviewer(self):
        for i in range(2):
            task = self.task(scope=f'part{i}', key=str(i))
            task['stage'] = 'review'
            self.store.save(task)
        with patch('next_pr.engine.repo_config', return_value=self.cfg['repos'][REPO]), \
             patch('next_pr.engine.start_run') as start:
            engine.tick(self.store)
            self.assertEqual(start.call_count, 1)

    def test_duplicate_tick_does_not_consume_again(self):
        task = self.task()
        record = self.record_run(task, result=self.result('blocked'), receipt={})
        engine.consume(self.store, task, record)
        engine.tick(self.store)
        engine.tick(self.store)
        self.assertEqual(len(self.store.task(task['id'])['evidence']), 1)

    def test_manual_recovery_refuses_existing_pid(self):
        task = self.task()
        record = self.record_run(task)
        atomic_json(Path(record['directory']) / 'child.json', {'pid': os.getpid()})
        with self.assertRaisesRegex(Blocked, 'exists'):
            recover(self.store, task)


class ProviderTests(Fixture):
    def test_api_environment_and_billing_gate(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'never-print-this'}):
            with self.assertRaisesRegex(Blocked, 'OPENAI_API_KEY') as error:
                providers.safe_environment()
            self.assertNotIn('never-print-this', str(error.exception))
        self.cfg['providers']['codex']['billing_confirmed'] = False
        with self.assertRaisesRegex(Blocked, 'unconfirmed'):
            providers.gate('codex', self.cfg, {})
        with self.assertRaises(Blocked):
            activation_gate(self.cfg)
        with self.assertRaises(Blocked):
            providers.gate('opencode', self.cfg, {})

    def test_optional_provider_requires_subscription_evidence(self):
        with patch('next_pr.providers.safe_environment', return_value={}):
            with self.assertRaisesRegex(Blocked, 'subscription_probe'):
                providers.verify_auth('grok', self.cfg['providers']['grok'])
            item = self.cfg['providers']['cursor'] | {'subscription_probe': ['probe']}
            with patch('next_pr.providers.command', return_value='{"subscription":true,"api_credentials":false}'):
                self.assertEqual(providers.verify_auth('cursor', item)['quota'], 'unknown')
            with patch('next_pr.providers.command', return_value='{"loggedIn":true}'):
                with self.assertRaises(Blocked):
                    providers.verify_auth('cursor', item)

    def test_read_only_adapters_and_no_model_override_or_bypass(self):
        prompt = self.home / 'prompt.txt'
        prompt.write_text('Untrusted text $(touch /tmp/never)')
        for name in ('codex', 'claude', 'cursor'):
            args = providers.argv(name, self.cfg['providers'][name], 'review', '/work', prompt)
            self.assertNotIn('--model', args)
            self.assertNotIn('--force', args)
            self.assertNotIn('--dangerously-skip-permissions', args)
            self.assertTrue(any(item in args for item in ('read-only', 'Read,Glob,Grep', 'ask')))
        with self.assertRaises(Blocked):
            providers.argv('grok', self.cfg['providers']['grok'], 'review', '/work', prompt)

    def test_contract_permission_and_native_rate_limit(self):
        log = self.home / 'log'
        log.write_text(json.dumps({'type': 'result', 'permission_denials': ['Bash denied']}))
        self.assertEqual(providers.parse_output(log)['status'], 'blocked')
        log.write_text(json.dumps({'type': 'error', 'message': 'You have hit your usage limit'}))
        self.assertEqual(providers.parse_output(log)['status'], 'rate_limited')
        log.write_text('tool output: {"status":"completed"}')
        with self.assertRaises(Blocked):
            providers.parse_output(log)
        log.write_text(json.dumps({'type': 'result', 'session_id': 'x',
                                   'result': json.dumps(self.result())}, indent=2))
        self.assertEqual(providers.parse_output(log)['head_sha'], SHA)


class GitHubTests(Fixture):
    def check(self, name='CI summary', conclusion='success', sha=SHA):
        return dict(id=1, name=name, status='completed', conclusion=conclusion, head_sha=sha,
                    app={'id': 1, 'slug': 'github-actions'})

    def facts(self):
        return dict(headRefOid=SHA, ci_sha=SHA, ci_conclusion='success', state='OPEN',
                    isDraft=False, mergeable='MERGEABLE', mergeStateStatus='CLEAN')

    def test_checks_fail_closed_and_summary_must_succeed(self):
        cases = [([], [], 'unknown'), ([self.check(conclusion='skipped')], [], 'unknown'),
                 ([self.check(conclusion='invented')], [], 'pending'),
                 ([self.check(conclusion='failure')], [], 'failure'),
                 ([self.check(sha=OTHER)], [], 'pending'),
                 ([self.check()], [{'id': 1, 'context': 'external', 'state': 'pending'}], 'pending'),
                 ([self.check()], [], 'success')]
        for runs, statuses, expected in cases:
            with self.subTest(expected=expected, runs=runs):
                self.assertEqual(github.check_facts(runs, statuses, SHA, True)['ci_conclusion'], expected)
        with self.assertRaisesRegex(Blocked, 'invalid'):
            github.check_facts([{'app': None}], [], SHA, True)
        old = self.check()
        new = self.check(conclusion='failure') | {'id': 2}
        self.assertEqual(github.check_facts([old, new], [], SHA, True)['ci_conclusion'], 'failure')

    def test_merge_requires_exact_review_head_and_clean_terminal_state(self):
        task = self.task()
        task['review_sha'] = SHA
        self.assertTrue(github.can_merge(task, self.facts()))
        for key, value in [('headRefOid', OTHER), ('ci_sha', OTHER), ('ci_conclusion', 'unknown'),
                           ('isDraft', True), ('mergeable', 'UNKNOWN'), ('mergeStateStatus', 'BLOCKED'),
                           ('state', 'CLOSED'), ('reviewDecision', 'CHANGES_REQUESTED')]:
            self.assertFalse(github.can_merge(task, self.facts() | {key: value}), key)

    def test_merge_uses_expected_head_and_leased_deletion(self):
        task = self.task()
        task['review_sha'] = SHA
        with patch('next_pr.github.observe', return_value=self.facts()), \
             patch('next_pr.github.reconciled_head', return_value=SHA), \
             patch('next_pr.github.view', return_value={'state': 'MERGED'}), \
             patch('next_pr.github.gh') as gh, patch('next_pr.github.git') as git:
            github.merge(task)
        self.assertIn('--match-head-commit', gh.call_args.args)
        self.assertIn(SHA, gh.call_args.args)
        self.assertIn(f'--force-with-lease=refs/heads/{task["branch"]}:{SHA}', git.call_args.args)

    def test_ci_failure_and_conflict_route_to_bounded_fixer(self):
        for facts in (self.facts() | {'ci_conclusion': 'failure'},
                      self.facts() | {'mergeable': 'CONFLICTING'}):
            task = self.task(scope=str(len(self.store.tasks())), key=str(len(self.store.tasks())))
            task.update(stage='ci', review_sha=SHA)
            with patch('next_pr.github.observe', return_value=facts), \
                 patch('next_pr.github.reconciled_head', return_value=SHA), patch('next_pr.github.gh'):
                engine.advance_ci(self.store, task, self.cfg['repos'][REPO])
            self.assertEqual(task['stage'], 'fixing')
            self.assertEqual(task['fix_round'], 1)
            self.assertIsNone(task['review_sha'])

    def test_non_dkdm_never_auto_merges(self):
        task = self.task()
        task.update(repo='other/repository', stage='ci', review_sha=SHA)
        with patch('next_pr.github.observe', return_value=self.facts()), \
             patch('next_pr.github.reconciled_head', return_value=SHA), patch('next_pr.github.merge') as merge:
            engine.advance_ci(self.store, task, {'auto_merge': True})
            merge.assert_not_called()
        self.assertEqual(task['stage'], 'mergeable')

    def test_fake_gh_exact_sha_api_and_stale_head(self):
        fake = self.home / 'gh'
        data = self.home / 'github.json'
        pr = self.facts() | {'number': 1, 'url': 'https://github.com/a/b/pull/1',
                             'headRefName': 'test', 'baseRefName': 'main'}
        data.write_text(json.dumps({'pr': pr, 'runs': [self.check()]}))
        fake.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
value=json.loads(Path(os.environ['FAKE_GH_DATA']).read_text())
with Path(os.environ['FAKE_GH_LOG']).open('a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1] == 'pr': print(json.dumps(value['pr']))
elif 'check-runs?' in sys.argv[-1]: print(json.dumps([{'check_runs': value['runs']}]))
else: print('[[]]')
''')
        fake.chmod(0o755)
        log = self.home / 'gh.log'
        with patch.dict(os.environ, {'PATH': str(self.home) + os.pathsep + os.environ['PATH'],
                                    'FAKE_GH_DATA': str(data), 'FAKE_GH_LOG': str(log)}):
            facts = github.observe(REPO, 1)
        self.assertEqual(facts['ci_conclusion'], 'success')
        self.assertIn(f'commits/{SHA}/check-runs', log.read_text())
        with patch('next_pr.github.view', side_effect=[pr, pr | {'headRefOid': OTHER}]), \
             patch('next_pr.github.command', side_effect=['[{"check_runs": []}]', '[[]]']):
            with self.assertRaisesRegex(Blocked, 'changed'):
                github.observe(REPO, 1)


class RunnerTests(Fixture):
    def manifest(self, code, role='writer', directory='supervisor'):
        directory = self.home / directory
        directory.mkdir()
        value = dict(id=directory.name, task_id='task', role=role, home=str(self.home),
                     argv=[sys.executable, '-c', code], worktree=str(self.home))
        path = directory / 'manifest.json'
        atomic_json(path, value)
        return path

    def test_actual_process_completion_and_duplicate_runner(self):
        path = self.manifest('print("done")')
        with patch('next_pr.runner.safe_environment', return_value=dict(os.environ)):
            runner.execute(path)
            runner.execute(path)
        receipt = read_json(path.parent / 'receipt.json')
        self.assertEqual(receipt['exit_code'], 0)
        self.assertTrue(receipt['safe_to_retry'])
        self.assertEqual((path.parent / 'stdout.log').read_text(), 'done\n')

    def test_prestart_cancel_never_launches_child(self):
        path = self.manifest('raise RuntimeError("must not execute")')
        atomic_json(path.parent / 'stop.json', {'cancel': True})
        runner.execute(path)
        receipt = read_json(path.parent / 'receipt.json')
        self.assertTrue(receipt['cancelled'])
        self.assertFalse((path.parent / 'child.json').exists())

    def test_runner_crash_marker_refuses_duplicate_start(self):
        path = self.manifest('raise RuntimeError("must not execute")')
        atomic_json(path.parent / 'started.json', {'pid': 99999})
        with self.assertRaisesRegex(Blocked, 'already started'):
            runner.execute(path)

    def test_setsid_descendant_retains_run_and_validation_locks(self):
        ready, release = self.home / 'descendant-ready', self.home / 'release-descendant'
        code = f'''import os, time
from pathlib import Path
ready, release = Path({str(ready)!r}), Path({str(release)!r})
if os.fork() == 0:
    os.setsid()
    ready.write_text(str(os.getpid()))
    while not release.exists():
        time.sleep(0.02)
    os._exit(0)
while not ready.exists():
    time.sleep(0.02)
'''
        path = self.manifest(code, role='validation')
        try:
            subprocess.run([sys.executable, '-m', 'next_pr.runner', str(path)],
                           check=True, capture_output=True, timeout=5)
            self.assertTrue(ready.exists())
            child_pid = read_json(path.parent / 'child.json')['pid']
            self.assertFalse(runner.group_alive(child_pid))
            self.assertEqual(read_json(path.parent / 'receipt.json')['exit_code'], 0)
            self.assertTrue(lock_held(path.parent / 'run.lock'))
            self.assertTrue(lock_held(self.home / 'validation.lock'))
            with self.assertRaisesRegex(Blocked, 'lock held'):
                runner.execute(path)
            # A second actual validation supervisor must wait despite the first
            # supervisor having exited and its original process group being gone.
            second = self.manifest('raise RuntimeError("must not launch")',
                                   role='validation', directory='second')
            process = subprocess.Popen([sys.executable, '-m', 'next_pr.runner', str(second)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.time() + 5
                while not (second.parent / 'started.json').exists() and time.time() < deadline:
                    time.sleep(0.02)
                self.assertTrue((second.parent / 'started.json').exists())
                atomic_json(second.parent / 'stop.json', {'cancel': True})
                process.communicate(timeout=5)
                self.assertFalse((second.parent / 'child.json').exists())
                self.assertTrue(read_json(second.parent / 'receipt.json')['cancelled'])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        finally:
            release.touch()
            deadline = time.time() + 5
            while lock_held(path.parent / 'run.lock') and time.time() < deadline:
                time.sleep(0.02)
        self.assertFalse(lock_held(path.parent / 'run.lock'))
        self.assertFalse(lock_held(self.home / 'validation.lock'))

    def test_validation_serialization_and_supervisor_cancel(self):
        path = self.manifest('print("validation")', role='validation')
        process = None
        with lock(self.home / 'validation.lock'):
            process = subprocess.Popen([sys.executable, '-m', 'next_pr.runner', str(path)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.time() + 5
                while not (path.parent / 'started.json').exists() and time.time() < deadline:
                    time.sleep(0.05)
                self.assertTrue((path.parent / 'started.json').exists())
                self.assertFalse((path.parent / 'child.json').exists())
                atomic_json(path.parent / 'stop.json', {'cancel': True})
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertTrue(read_json(path.parent / 'receipt.json')['cancelled'])


if __name__ == '__main__':
    unittest.main()


class CrashTests(Fixture):
    def test_validation_locks_survive_supervisor_death_until_child_exits(self):
        directory = self.home / 'crash'
        directory.mkdir()
        manifest = directory / 'manifest.json'
        atomic_json(manifest, dict(id='crash', role='validation', home=str(self.home),
                    worktree=str(self.home), argv=[sys.executable, '-c', 'import time; time.sleep(1.5)']))
        process = subprocess.Popen([sys.executable, '-m', 'next_pr.runner', str(manifest)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.time() + 5
            while not (directory / 'child.json').exists() and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue((directory / 'child.json').exists())
            process.kill()
            process.communicate(timeout=5)
            self.assertTrue(lock_held(directory / 'run.lock'))
            self.assertTrue(lock_held(self.home / 'validation.lock'))
            self.assertFalse((directory / 'receipt.json').exists())
            deadline = time.time() + 5
            while lock_held(directory / 'run.lock') and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(lock_held(directory / 'run.lock'))
            self.assertFalse((directory / 'receipt.json').exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_state_inside_git_checkout_is_rejected_before_write(self):
        path = self.home / 'repo'
        subprocess.run(['git', 'init', str(path)], capture_output=True, check=True)
        with self.assertRaisesRegex(Blocked, 'outside'):
            Store(path / 'state')
        self.assertFalse((path / 'state').exists())


class DashboardTests(Fixture):
    @patch('next_pr.cli.ui.serve')
    def test_cli_ui_dispatches_without_holding_state_lock(self, serve):
        with patch('next_pr.cli.Store') as store:
            for options, port in (([], 8765), (['--port', '9001'], 9001)):
                self.assertEqual(main(['--home', str(self.home), 'ui', *options]), 0)
                serve.assert_called_with(self.home, '127.0.0.1', port)
            store.assert_not_called()

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for browser JavaScript checks')
    def test_real_store_snapshot_opens_every_run_without_task_items(self):
        task = self.task()
        self.assertNotIn('items', task)
        logs = {}
        for index, run_id in enumerate(('ab' * 16, 'cd' * 16)):
            directory = self.home / 'runs' / run_id
            directory.mkdir(parents=True)
            (directory / 'prompt.txt').write_text('# Request\n- Keep `code`')
            (directory / 'stdout.log').write_text(json.dumps({'type': 'text', 'data': '## Reply\nDone'}))
            self.store.save_run(dict(id=run_id, task_id=task['id'], role='writer',
                                     provider='codex', created_at=now() + index,
                                     directory=str(directory),
                                     receipt={'exit_code': 0} if index == 0 else None))
            logs[run_id] = transcript(self.home, run_id)
        other = self.task(scope='web', key='two')
        page_state = snapshot(self.store)
        self.assertEqual(len(page_state['runs']), 2)
        self.assertNotIn('items', page_state['tasks'][0])
        self.assertEqual(page_state['tasks'][1]['id'], other['id'])
        check = Path(__file__).with_name('ui_page.cjs')
        result = subprocess.run(['node', str(check)], input=json.dumps({
            'page': PAGE, 'state': page_state, 'logs': logs}), text=True,
            capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class DashboardHTTPTests(Fixture):
    def request(self, path, method='GET', headers=None):
        if headers is None:
            headers = [('Host', '127.0.0.1:8765')]
        raw = f'{method} {path} HTTP/1.1\r\n'
        raw += ''.join(f'{key}: {value}\r\n' for key, value in headers) + '\r\n'
        response = bytearray()
        connection = SimpleNamespace(makefile=lambda *_: BytesIO(raw.encode()),
                                     sendall=response.extend)
        server = SimpleNamespace(server_port=8765)
        with patch.object(Handler, 'home', self.home):
            Handler(connection, ('127.0.0.1', 12345), server)
        head, body = bytes(response).split(b'\r\n\r\n', 1)
        status = int(head.split()[1])
        if b'application/json' in head:
            return status, json.loads(body)
        return status, body.decode()

    def test_valid_hosts_can_read_page_state_and_transcript(self):
        task = self.task()
        run_id = 'ab' * 16
        directory = self.home / 'runs' / run_id
        directory.mkdir(parents=True)
        (directory / 'prompt.txt').write_text('Private prompt')
        for host in ('127.0.0.1:8765', 'localhost:8765'):
            with self.subTest(host=host):
                headers = [('Host', host)]
                self.assertEqual(self.request('/', headers=headers), (200, PAGE))
                status, body = self.request('/api/state', headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(body['tasks'][0]['id'], task['id'])
                status, body = self.request(f'/runs/{run_id}/transcript', headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(body['messages'][0]['text'], 'Private prompt')

    def test_invalid_hosts_rejected_before_reading_or_mutating(self):
        for hosts in ([], ['attacker.example:8765'], ['127.0.0.1:9001'], ['localhost'],
                      ['127.0.0.1:8765', 'attacker.example:8765']):
            for method, path in (('GET', '/'), ('GET', '/api/state'),
                                 ('GET', '/runs/' + 'ab' * 16 + '/transcript'),
                                 ('POST', '/tasks/id/pause')):
                with self.subTest(hosts=hosts, method=method, path=path), \
                        patch('next_pr.ui.Store') as store:
                    headers = [('Host', host) for host in hosts]
                    headers.append(('Origin', 'http://127.0.0.1:8765'))
                    self.assertEqual(self.request(path, method, headers),
                                     (403, {'blocked': 'invalid host'}))
                    store.assert_not_called()

    def test_post_requires_exact_same_origin_and_preserves_controls(self):
        for host in ('127.0.0.1:8765', 'localhost:8765'):
            origins = ([], ['null'], ['https://' + host], ['http://attacker.example:8765'],
                       ['http://localhost:9001'], ['http://' + host, 'http://' + host],
                       ['http://' + ('localhost:8765' if host.startswith('127') else '127.0.0.1:8765')])
            for origin in origins:
                with self.subTest(host=host, origin=origin), patch('next_pr.ui.Store') as store:
                    headers = [('Host', host)] + [('Origin', value) for value in origin]
                    self.assertEqual(self.request('/tasks/id/pause', 'POST', headers),
                                     (403, {'blocked': 'same-origin request required'}))
                    store.assert_not_called()
            for action in ('pause', 'resume', 'cancel'):
                with self.subTest(host=host, action=action), \
                        patch('next_pr.ui.act', return_value={'ok': True}) as act:
                    headers = [('Host', host), ('Origin', 'http://' + host)]
                    self.assertEqual(self.request('/tasks/id/' + action, 'POST', headers),
                                     (200, {'ok': True}))
                    self.assertEqual(act.call_args.args[1:], ('id', action))

    def test_malformed_run_routes_return_json_404(self):
        run = 'ab' * 16
        for path in ('/runs/', f'/runs/{run}', f'/runs/{run}/', '/runs//transcript',
                     f'/runs/{run}/transcript/extra', f'/runs/{run}/unknown'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path), (404, {'blocked': 'not found'}))


class TranscriptTests(Fixture):
    def test_page_keeps_conversation_primary_and_task_controls_available(self):
        self.assertIn('<html lang="zh-Hant">', PAGE)
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', PAGE)
        self.assertIn('<section id="viewer" aria-labelledby="viewer-title">', PAGE)
        self.assertIn('<aside class="sidebar" aria-labelledby="tasks-title">', PAGE)
        self.assertLess(PAGE.index('id="viewer"'), PAGE.index('class="sidebar"'))
        for identifier in ('tasks', 'now', 'flash', 'daemon', 'providers', 'activity', 'viewer-status'):
            self.assertIn('id="' + identifier + '"', PAGE)
        self.assertIn('<article class="task-row', PAGE)
        self.assertIn('grid-template-columns: minmax(0, 1fr) minmax(260px, 30%)', PAGE)
        self.assertIn('@media (max-width: 640px)', PAGE)
        self.assertIn('.workspace { display: flex; flex-direction: column; }', PAGE)
        self.assertNotIn("document.getElementById('viewer').hidden = true", PAGE)
        self.assertIn("action === 'cancel' && !confirm(", PAGE)
        for label in ('暫停', '繼續', '取消', '協調器紀錄', '工具就緒'):
            self.assertIn(label, PAGE)

    def test_transcript_shows_speech_tools_and_refuses_other_runs(self):
        run = self.home / 'runs' / ('ab' * 16)
        run.mkdir(parents=True)
        (run / 'prompt.txt').write_text('Please change the label')
        (run / 'manifest.json').write_text(json.dumps({'provider': 'grok', 'role': 'code'}))
        contract = json.dumps({'status': 'completed', 'summary': 'Label updated', 'tests': [], 'blockers': []})
        (run / 'stdout.log').write_text('\n'.join([
            json.dumps({'type': 'thought', 'data': 'Looking'}),
            json.dumps({'type': 'thought', 'data': ' at the file'}),
            json.dumps({'type': 'tool_call', 'toolCallId': 'c1', 'toolName': 'read_file',
                        'rawInput': {'target_file': 'app/Label.kt'}}),
            json.dumps({'type': 'tool_call_update', 'toolCallId': 'c1', 'status': 'completed',
                        'content': [{'type': 'content', 'content': 'class Label'}]}),
            json.dumps({'type': 'text', 'data': contract[:8]}),
            json.dumps({'type': 'text', 'data': contract[8:]}),
        ]) + '\n')
        page = transcript(self.home, 'ab' * 16)
        self.assertEqual(page['provider'], 'grok')
        self.assertEqual([item['kind'] for item in page['messages']], ['prompt', 'thought', 'tool', 'say'])
        self.assertEqual(page['messages'][0]['text'], 'Please change the label')
        self.assertEqual(page['messages'][1]['text'], 'Looking at the file')
        self.assertIn('Label.kt', page['messages'][2]['text'])
        self.assertIn('class Label', page['messages'][2]['text'])
        self.assertEqual(page['messages'][2]['tool'], 'read_file')
        self.assertEqual(page['messages'][2]['command'], 'app/Label.kt')
        self.assertEqual(page['messages'][2]['output'], 'class Label')
        self.assertEqual(page['messages'][2]['calls'], [
            {'tool': 'read_file', 'command': 'app/Label.kt', 'output': 'class Label'}])
        self.assertIn('Label updated', page['messages'][3]['text'])
        run = self.home / 'runs' / ('cd' * 16)
        run.mkdir()
        (run / 'prompt.txt').write_text('Look')
        calls = []
        for index, name in enumerate(('read_file', 'grep', 'grep')):
            calls.append(json.dumps({'type': 'tool_call', 'toolCallId': str(index), 'toolName': name,
                                     'rawInput': {'target_file': 'a.kt'}}))
        calls.append(json.dumps({'type': 'text', 'data': json.dumps(self.result())}))
        (run / 'stdout.log').write_text('\n'.join(calls) + '\n')
        grouped = transcript(self.home, 'cd' * 16)
        self.assertEqual([item['kind'] for item in grouped['messages']], ['prompt', 'tool', 'say'])
        self.assertIn('3 次', grouped['messages'][1]['title'])
        self.assertIn('read_file 1', grouped['messages'][1]['title'])
        grouped_calls = grouped['messages'][1]['calls']
        self.assertEqual(len(grouped_calls), 3)
        self.assertEqual([item['tool'] for item in grouped_calls], ['read_file', 'grep', 'grep'])
        self.assertEqual([item['command'] for item in grouped_calls], ['a.kt', 'a.kt', 'a.kt'])
        self.assertTrue(all(item['output'] == '' for item in grouped_calls))
        self.assertIn('id="viewer"', PAGE)
        self.assertIn('id="now"', PAGE)
        self.assertIn('協調器紀錄', PAGE)
        self.assertNotIn('運行同對話', PAGE)
        self.assertNotIn('if (openRun) loadTalk()', PAGE)
        self.assertIn('function renderMarkdown', PAGE)
        self.assertIn('class="md"', PAGE)
        self.assertIn('class="md-code"', PAGE)
        self.assertIn('class="md-inline"', PAGE)
        self.assertIn('class="term"', PAGE)
        self.assertIn('class="term-cmd"', PAGE)
        self.assertIn('class="term-prompt"', PAGE)
        self.assertIn('class="term-out"', PAGE)
        self.assertIn('class="term-err"', PAGE)
        self.assertNotIn('foldTalk', PAGE)
        self.assertNotIn("'</summary><pre>'", PAGE)
        with self.assertRaisesRegex(Blocked, 'unknown log'):
            transcript(self.home, '../config')

    def test_transcript_keeps_order_clips_and_splits_stderr(self):
        run = self.home / 'runs' / ('ef' * 16)
        run.mkdir(parents=True)
        prompt = 'P' * 5000
        (run / 'prompt.txt').write_text(prompt)
        (run / 'stdout.log').write_text('\n'.join([
            json.dumps({'type': 'thought', 'data': 'Hmm'}),
            json.dumps({'type': 'error', 'message': 'model blew up'}),
            json.dumps({'type': 'tool_call', 'toolCallId': 'a', 'toolName': 'read_file',
                        'rawInput': {'target_file': 'left.kt'}}),
            json.dumps({'type': 'text', 'data': 'between'}),
            json.dumps({'type': 'tool_call', 'toolCallId': 'b', 'toolName': 'grep',
                        'rawInput': {'command': 'rg label'}}),
            json.dumps({'type': 'tool_call_update', 'toolCallId': 'b', 'status': 'completed',
                        'rawOutput': 'Y' * 2500}),
        ]) + '\n')
        (run / 'stderr.log').write_text('E' * 5000)
        page = transcript(self.home, 'ef' * 16)
        self.assertEqual([item['kind'] for item in page['messages']],
                         ['prompt', 'thought', 'error', 'tool', 'say', 'tool', 'error'])
        self.assertEqual(page['messages'][0]['text'], prompt)
        self.assertEqual(page['messages'][1]['text'], 'Hmm')
        model, stderr = page['messages'][2], page['messages'][6]
        self.assertEqual(model['source'], 'model')
        self.assertEqual(model['title'], '錯誤')
        self.assertEqual(model['text'], 'model blew up')
        self.assertEqual(stderr['source'], 'stderr')
        self.assertEqual(stderr['title'], 'stderr')
        self.assertNotEqual(model['source'], stderr['source'])
        self.assertEqual(stderr['text'], ('E' * 4000) + '\n…已截短')
        self.assertEqual(page['messages'][3]['command'], 'left.kt')
        self.assertEqual(page['messages'][3]['output'], '')
        self.assertEqual(page['messages'][4]['text'], 'between')
        trailed = page['messages'][5]
        self.assertEqual(trailed['tool'], 'grep')
        self.assertEqual(trailed['command'], 'rg label')
        self.assertEqual(trailed['output'], ('Y' * 2000) + '\n…已截短')
        self.assertNotIn('Y' * 2500, trailed['text'])
        self.assertIn('rg label', trailed['text'])
        self.assertEqual(trailed['calls'][0]['output'], trailed['output'])
