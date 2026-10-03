"""One-shot Pane dispatcher. No scheduler, retries, credential access or merge path."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid

CLIS = ('codex', 'claude', 'cursor', 'agy', 'grok', 'opencode')
BUILTIN = CLIS[:3]
# These are worker CLIs: xAI's Grok CLI is distinct from Grok Bot desktop.
PROVIDERS = dict(codex='codex', claude='claude', cursor='cursor', agy='antigravity',
                 grok='grok', opencode='opencode')
KINDS = 'agent.report,agent.blocked,agent.idle,agent.ready,panel.exited,pane.gone'


class DispatchError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def executable(name):
    names = ('cursor-agent', 'agent') if name == 'cursor' else (name,)
    roots = [Path.home() / '.local/bin', Path('/opt/homebrew/bin'), Path('/usr/local/bin'),
             Path.home() / '.grok/bin', Path.home() / '.opencode/bin',
             Path.home() / '.cursor/bin', Path.home() / '.antigravity/bin']
    for candidate in names:
        found = shutil.which(candidate)
        if found:
            return os.path.abspath(found)
        for root in roots:
            path = root / candidate
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    if name == 'codexbar':
        path = Path('/Applications/CodexBar.app/Contents/Helpers/CodexBarCLI')
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def run(argv, timeout=25, ndjson=False, with_exit=False):
    """Never expose raw stdout/stderr on errors (they may contain credentials)."""
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        raise DispatchError('process_timeout') from None
    except OSError:
        raise DispatchError('process_unavailable') from None
    if proc.returncode and not with_exit:
        raise DispatchError('process_exit_' + str(proc.returncode))
    try:
        value = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()] if ndjson else json.loads(proc.stdout)
        return (value, proc.returncode) if with_exit else value
    except (ValueError, TypeError):
        raise DispatchError('invalid_json') from None


def pane(*args, timeout=25, ndjson=False):
    path = executable('runpane')
    if not path:
        raise DispatchError('runpane_missing')
    result = run([path, *args, '--json'], timeout, ndjson)
    if isinstance(result, dict) and result.get('ok') is False:
        raise DispatchError('pane_command_failed')
    return result


def pick(value, keys):
    return {key: value[key] for key in keys if key in value}


def private_write(path, content):
    fd, temp = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Store:
    def __init__(self, root=None):
        self.root = Path(root or Path.home() / '.local/state/pane-dispatch').expanduser().absolute()
        if self.root.is_symlink():
            raise DispatchError('state_directory_is_symlink')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    @contextmanager
    def locked(self):
        fd = os.open(self.root / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def path(self, task_id):
        return self.root / (digest(task_id) + '.json')

    def get(self, task_id):
        path = self.path(task_id)
        if not path.exists():
            return None
        if path.is_symlink():
            raise DispatchError('state_file_is_symlink')
        return json.loads(path.read_text())

    def save(self, record):
        record['updated_at'] = now()
        private_write(self.path(record['task_id']), json.dumps(record, ensure_ascii=False, indent=2))

    def all(self):
        return [json.loads(p.read_text()) for p in self.root.glob('*.json') if not p.is_symlink()]


def cli_info(name):
    path = executable(name)
    result = dict(cli=name, path=path, available=bool(path), quota_group=PROVIDERS[name])
    if path and name not in BUILTIN:
        try:
            proc = subprocess.run([path, '--help'], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=10, shell=False)
            help_text = proc.stdout + proc.stderr
            required = {'agy': ('--prompt-interactive',), 'grok': ('[PROMPT]', '--no-subagents', '--session-id'),
                        'opencode': ('--prompt',)}[name]
            result['available'] = proc.returncode == 0 and all(flag in help_text for flag in required)
            if not result['available']:
                result['error'] = 'unsupported_cli_help'
        except (OSError, subprocess.TimeoutExpired):
            result.update(available=False, error='cli_help_failed')
    return result


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fresh(timestamp):
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp.replace('Z', '+00:00'))).total_seconds()
        return -60 <= age <= 600
    except (ValueError, TypeError, AttributeError):
        return False


def parse_quota(provider, payload, fetched_at):
    """Allowlist quota fields; do not serialize identities, details, tokens or raw errors."""
    rows = payload if isinstance(payload, list) else [payload]
    accounts = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('provider') != provider:
            continue
        usage = row.get('usage') or {}
        if not isinstance(usage, dict):
            continue
        identity = usage.get('identity') or {}
        identity = identity if isinstance(identity, dict) else {}
        account = identity.get('accountEmail') or row.get('account') or 'default'
        key = digest(provider + ':' + str(account))[:20]
        stamp = usage.get('updatedAt') or row.get('updatedAt')
        item = dict(account_key=key, source=row.get('source'), updated_at=stamp,
                    fetched_at=fetched_at, windows=[], error='provider_error' if row.get('error') else None)
        for slot in ('primary', 'secondary', 'tertiary'):
            window = usage.get(slot)
            if not isinstance(window, dict):
                continue
            used = window.get('usedPercent')
            valid = numeric(used) and 0 <= used <= 100
            item['windows'].append(dict(window=slot, used_percent=used if valid else None,
                window_minutes=window.get('windowMinutes'), resets_at=window.get('resetsAt')))
        item['usable'] = bool(item['source'] and fresh(stamp) and item['windows'] and not item['error']
                              and all(w['used_percent'] is not None for w in item['windows']))
        # Same account returned twice is never counted as extra capacity. Disagreeing rows fail closed.
        if key in accounts and accounts[key] != item:
            item.update(usable=False, error='conflicting_account_snapshots')
        accounts[key] = item
    items = list(accounts.values())
    return dict(provider=provider, fetched_at=fetched_at, accounts=items,
                error=None if items else 'quota_unavailable',
                usable=len(items) == 1 and items[0]['usable'],
                eligible=len(items) == 1 and items[0]['usable'] and
                    all(w['used_percent'] < 100 for w in items[0]['windows']))


def quota(provider):
    path, stamp = executable('codexbar'), now()
    try:
        if not path:
            raise DispatchError('codexbar_missing')
        payload, code = run([path, 'usage', '--provider', provider, '--format', 'json'], timeout=20, with_exit=True)
        result = parse_quota(provider, payload, stamp)
        if code:
            result.update(error='process_exit_' + str(code), usable=False, eligible=False)
        return result
    except DispatchError as exc:
        return dict(provider=provider, fetched_at=stamp, accounts=[], usable=False,
                    eligible=False, error=str(exc))


def inventory(store):
    infos = [cli_info(name) for name in CLIS]
    groups = sorted(set(PROVIDERS.values()))
    with ThreadPoolExecutor(max_workers=len(groups)) as pool:
        quotas = dict(zip(groups, pool.map(quota, groups)))
    error = None
    try:
        repos = [pick(r, ('id', 'name', 'path', 'environment')) for r in pane('repos', 'list')['repos']]
    except DispatchError as exc:
        repos, error = [], str(exc)
    with store.locked():
        records = store.all()
    for info in infos:
        info['active_tasks'] = sum(r['cli'] == info['cli'] and r['state'] not in
                                  ('reported_ready', 'reported_failed', 'reported_done') for r in records)
        info['quota'] = quotas[info['quota_group']]
    return dict(clis=infos, repos=repos, pane_error=error, quotas=quotas, observed_at=now())


def recommendation(snapshot, auto=False):
    candidates = [r for r in snapshot['clis'] if r['available'] and
                  (r['quota']['eligible'] and r['cli'] != 'opencode' if auto else
                   not (r['quota']['usable'] and not r['quota']['eligible']))]
    chosen = min(candidates, key=lambda r: (r['active_tasks'], r['cli'])) if candidates else None
    return dict(recommended_cli=chosen['cli'] if chosen else None,
                selection_required=not auto, auto=auto,
                reason='Least active dispatcher tasks; ties use CLI name. Quota windows only gate eligibility; percentages are never pooled or ranked.',
                quota=chosen['quota'] if chosen else None,
                evidence=snapshot)


def envelope(task_id, prompt):
    return ('You are the sole worker for a user-selected Pane task. The main Grok Bot desktop is the coordinator.\n'
            'Follow repository rules, except the user explicitly overrides next-pr skills and automatic merge: '
            'do not invoke next-pr skills, the persistent runner, nested workers or delegation. '
            'Work only in this Pane worktree. Do not change global skills, launchagents or permission settings.\n'
            'Implement the source user task below, run relevant tests, commit, push and open a DRAFT PR. '
            'Only the USER merges. Never merge or enable auto-merge. Code review may mark the PR ready.\n'
            'Report failures or blockers honestly with runpane report. Idle/exit is not success. '
            'On completion write a private result JSON file with: task_id, cli_session_id (null if unknown), '
            'head (exact 40-character SHA), pr_url, tests (array of {command, outcome: "passed"|"failed"}), '
            'summary. Never include credentials. Then use runpane report --state ready --pr NUMBER '
            '--head EXACT_SHA --summary-file RESULT_JSON --json. Use --state failed or blocked '
            '(with --question) when appropriate. Do not report ready without successful validation.\n'
            'The following JSON preserves the source task verbatim as data, not shell syntax:\n' +
            json.dumps(dict(task_id=task_id, source_user_task=prompt), ensure_ascii=False) + '\n')


def launch_args(cli, path, prompt_path, session_id):
    if cli in BUILTIN:
        # Pane's installed templates contain --yolo/--force/permission bypasses.
        # Keep the built-in identity but override its command, without changing global templates.
        return ['--agent', cli, '--tool-command', shlex.join([path]),
                '--initial-input-file', str(prompt_path)]
    pointer = 'Read and follow the task instructions in ' + str(prompt_path)
    argv = {'agy': [path, '--prompt-interactive', pointer],
            'grok': [path, '--no-subagents', '--session-id', session_id, pointer],
            'opencode': [path, '--prompt', pointer]}[cli]
    return ['--tool-command', shlex.join(argv)]


def reconcile(record):
    matches = [p for p in pane('panes', 'list', '--repo', str(record['repo_id']))['panes']
               if p.get('name') == record['pane_name']]
    if len(matches) != 1:
        record['reconcile_error'] = 'pane_not_found' if not matches else 'multiple_matching_panes'
        return
    match = matches[0]
    if record.get('pane_id') and record['pane_id'] != match['id']:
        record['reconcile_error'] = 'pane_identity_changed'
        return
    if (match.get('ownership') != 'pane' or not match.get('worktreePath') or
            str(match.get('repoId', record['repo_id'])) != str(record['repo_id'])):
        record['reconcile_error'] = 'worktree_unverified'
        return
    record.update(pane_id=match['id'], worktree=match['worktreePath'])
    record.pop('reconcile_error', None)
    if record['state'] == 'creation_unknown':
        record['state'] = 'needs_inspection'


def start(store, task_id, repo, cli, prompt, auto=False):
    if bool(cli) == bool(auto):
        raise DispatchError('select_explicit_cli_or_auto')
    if not task_id or len(task_id) > 200:
        raise DispatchError('invalid_task_id')
    request = dict(repo=repo, selection=cli or 'auto', prompt_sha256=digest(prompt))
    with store.locked():
        existing = store.get(task_id)
        if existing:
            if existing['request'] != request:
                raise DispatchError('task_id_request_mismatch')
            try:
                reconcile(existing)
            except DispatchError as exc:
                existing['reconcile_error'] = str(exc)
            store.save(existing)
            return existing
    # All preflight failures are before durable intent; no worker has been launched.
    selection_evidence = None
    if auto:
        decision = recommendation(inventory(store), auto=True)
        cli = decision['recommended_cli']
        selection_evidence = pick(decision, ('recommended_cli', 'reason', 'quota'))
        if not cli:
            raise DispatchError('auto_requires_usable_quota_and_cli')
    info = cli_info(cli)
    if not info['available']:
        raise DispatchError('selected_cli_unavailable')
    doctor = pane('doctor')
    if not doctor.get('daemon', {}).get('reachable'):
        raise DispatchError('pane_daemon_unavailable')
    pane('agent-context')
    contract = pane('agent-context', '--command', 'panes create')['command']
    supported = {a['name'] for a in contract['arguments']}
    if not {'--tool-command', '--initial-input-file', '--agent', '--branch'} <= supported:
        raise DispatchError('unsupported_pane_schema')
    repos = pane('repos', 'list')['repos']
    matches = [r for r in repos if repo in (str(r['id']), r['name'], r['path'])]
    if len(matches) != 1 or matches[0].get('environment') not in (None, 'macos', 'linux'):
        raise DispatchError('select_one_saved_local_repo')
    if cli in BUILTIN:
        health = pane('agents', 'doctor', '--agent', cli, '--repo', str(matches[0]['id']))
        if not health.get('available'):
            raise DispatchError('pane_cli_unavailable')
    with store.locked():
        existing = store.get(task_id)
        if existing:
            if existing['request'] != request:
                raise DispatchError('task_id_request_mismatch')
            return existing
        name = 'pd-' + digest(task_id)[:32]
        if any(p.get('name') == name for p in pane('panes', 'list')['panes']):
            raise DispatchError('preexisting_pane_without_task_record')
        prompt_path = store.root / (digest(task_id) + '.prompt')
        private_write(prompt_path, envelope(task_id, prompt))
        record = dict(task_id=task_id, request=request, repo_id=matches[0]['id'],
                      repo_path=matches[0]['path'], cli=cli, cli_path=info['path'],
                      pane_name=name, pane_id=None, panel_id=None, worktree=None,
                      cli_session_id=str(uuid.uuid4()) if cli == 'grok' else None,
                      state='creation_unknown', created_at=now(), pr=None, evidence=None,
                      selection_evidence=selection_evidence)
        launch = launch_args(cli, info['path'], prompt_path, record['cli_session_id'])
        record['launch_command'] = launch[launch.index('--tool-command') + 1]
        store.save(record)  # fsync intent BEFORE the only creation attempt, even if we crash.
        args = ['panes', 'create', '--repo', str(record['repo_id']), '--name', name,
                '--branch', name, *launch,
                '--source', 'agent', '--no-focus', '--wait-ready', '--yes']
        try:
            result = pane(*args, timeout=60)
            items = result.get('items', [])
            if len(items) != 1:
                raise DispatchError('unexpected_creation_result')
            item = items[0]
            record.update(pane_id=item.get('paneId'), panel_id=item.get('panelId'),
                          worktree=item.get('worktreePath'),
                          delivery=pick(item.get('initialInput', {}), ('delivery', 'verifiedSubmitted')),
                          association=pick(item.get('association', {}), ('sessionId', 'ok')))
            record['state'] = 'needs_inspection'
            if record['delivery'].get('verifiedSubmitted') and item.get('ok'):
                record['state'] = 'submitted'
        except DispatchError as exc:
            record['creation_error'] = str(exc)
        try:
            reconcile(record)
        except DispatchError as exc:
            record['reconcile_error'] = str(exc)
        store.save(record)
        return record


def report_evidence(report, task_id):
    result = dict(provenance='pane_worker_report', independently_verified=False,
                  reported_at=report.get('reportedAt'), state=report.get('state'),
                  head=report.get('head'), pr=report.get('pr'), complete=False)
    try:
        summary = json.loads(report.get('summary', ''))
        tests = summary.get('tests')
        valid = (type(report.get('pr')) is int and report['pr'] > 0 and
                 summary.get('task_id') == task_id and re.fullmatch('[0-9a-f]{40}', summary.get('head', ''))
                 and summary['head'] == report.get('head') and
                 re.fullmatch(r'https://github\.com/[^/\s]+/[^/\s]+/pull/' + str(report.get('pr')), summary.get('pr_url', ''))
                 and isinstance(tests, list) and bool(tests) and
                 all(isinstance(t, dict) and isinstance(t.get('command'), str) and t['command'].strip()
                     and t.get('outcome') == 'passed' for t in tests))
        if valid:
            result.update(complete=True, pr_url=summary['pr_url'],
                          tests=[pick(t, ('command', 'outcome')) for t in tests],
                          cli_session_id=summary.get('cli_session_id'))
    except (ValueError, TypeError, AttributeError):
        pass
    return result


def status(store, task_id):
    with store.locked():
        record = store.get(task_id)
        if not record:
            raise DispatchError('unknown_task')
        try:
            reconcile(record)
            if record.get('reconcile_error'):
                raise DispatchError(record['reconcile_error'])
            panels = pane('panels', 'list', '--pane', record['pane_id'])['panels']
            if not record.get('panel_id'):
                candidates = [p for p in panels if p.get('launchCommand') == record.get('launch_command') or
                              p.get('isCliPanel') or p.get('agentType') == record['cli']]
                if len(candidates) == 1:
                    record['panel_id'] = candidates[0]['id']
            panel = next((p for p in panels if p['id'] == record.get('panel_id')), {})
            report = panel.get('report')
            try:
                events = pane('watch', '--pane', record['pane_id'], '--since', str(record.get('watch_cursor', 0)),
                              '--timeout-ms', '0', '--kinds', KINDS, ndjson=True)
                record.pop('watch_error', None)
                generations = [e['gen'] for e in events if type(e.get('gen')) is int]
                if generations:
                    record['watch_cursor'] = max(generations)
                elif any(e.get('kind') == '_reset' for e in events):
                    record['watch_cursor'] = 0
                record['journal_warnings'] = [e['kind'] for e in events if e.get('kind') in ('_reset', '_dropped', '_error')]
            except DispatchError as exc:
                events = []
                record['watch_error'] = str(exc)
            scoped = [e for e in events if e.get('paneId') == record['pane_id'] and
                      e.get('panelId') == record.get('panel_id')]
            record['events'] = (record.get('events', []) +
                                [pick(e, ('gen', 'at', 'kind', 'exitCode')) for e in scoped])[-50:]
            if report:
                evidence = report_evidence(report, task_id)
                record['evidence'] = evidence
                state = report.get('state')
                if state in ('ready', 'done'):
                    record['state'] = 'reported_ready' if evidence['complete'] else 'incomplete_report'
                else:
                    record['state'] = 'reported_' + str(state) if state in ('failed', 'blocked') else 'incomplete_report'
                if evidence['complete']:
                    record['pr'] = dict(url=evidence['pr_url'], number=evidence['pr'], head=evidence['head'])
                    if evidence.get('cli_session_id'):
                        record['cli_session_id'] = evidence['cli_session_id']
            elif scoped:
                kind = scoped[-1]['kind']
                record['state'] = {'panel.exited': 'exited_without_report', 'agent.idle': 'idle_without_report',
                                   'agent.ready': 'idle_without_report', 'agent.blocked': 'blocked'}.get(kind, record['state'])
            record.pop('status_error', None)
        except DispatchError as exc:
            record['status_error'] = str(exc)
        record['watch_argv'] = ([executable('runpane'), 'watch', '--pane', record['pane_id'], '--follow',
                                 '--quiet', '--kinds', KINDS, '--json'] if record.get('pane_id') else None)
        store.save(record)
        return record


def usage(store, task_id):
    with store.locked():
        record = store.get(task_id)
    if not record:
        raise DispatchError('unknown_task')
    result = dict(task_id=task_id, pane_id=record.get('pane_id'), source='runpane panes cost',
                  fetched_at=now(), estimated_cost_usd=None, total_tokens=None,
                  attribution='unknown', quota=quota(PROVIDERS[record['cli']]))
    try:
        if not record.get('pane_id'):
            raise DispatchError('pane_unknown')
        data = pane('panes', 'cost', '--pane', record['pane_id'])
        rows = [r for r in data.get('panes', []) if r.get('paneId') == record['pane_id']]
        if len(rows) != 1 or not rows[0].get('messageCount'):
            raise DispatchError('no_attributed_usage')
        row = rows[0]
        result.update(attribution='pane_lifetime_last_30_days',
                      estimated_cost_usd=row.get('estimatedCostUsd'), total_tokens=row.get('totalTokens'),
                      incomplete=row.get('costIncomplete'), from_ms=data.get('fromMs'),
                      to_ms=data.get('toMs'), pricing_as_of=data.get('pricingAsOf'))
    except DispatchError as exc:
        result['error'] = str(exc)
    return result


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DispatchError('invalid_arguments: ' + message)


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, help='Private state directory (default ~/.local/state/pane-dispatch)')
    subs = parser.add_subparsers(dest='command', required=True)
    for name in ('inventory', 'recommend'):
        subs.add_parser(name)
    start_parser = subs.add_parser('start')
    start_parser.add_argument('--task-id', required=True)
    start_parser.add_argument('--repo', required=True)
    selection = start_parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--cli', choices=CLIS)
    selection.add_argument('--auto', action='store_true', help='Only when user explicitly authorized automatic choice')
    start_parser.add_argument('--prompt-file', required=True, type=Path)
    for name in ('status', 'usage'):
        subs.add_parser(name).add_argument('--task-id', required=True)
    try:
        args = parser.parse_args(argv)
        store = Store(args.state_dir)
        if args.command == 'start':
            result = start(store, args.task_id, args.repo, args.cli, args.prompt_file.read_text(), args.auto)
        elif args.command == 'status':
            result = status(store, args.task_id)
        elif args.command == 'usage':
            result = usage(store, args.task_id)
        else:
            result = inventory(store)
            if args.command == 'recommend':
                result = recommendation(result)
        print(json.dumps(dict(ok=True, result=result), ensure_ascii=False))
        return 0
    except (DispatchError, OSError, ValueError, KeyError, TypeError) as exc:
        error = str(exc) if isinstance(exc, DispatchError) else type(exc).__name__
        print(json.dumps(dict(ok=False, error=error)))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
