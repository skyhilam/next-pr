"""One-shot Pane dispatcher. No scheduler, retries, credential access or merge path."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
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
import time
from urllib.parse import urlencode
import uuid

CLIS = ('codex', 'claude', 'cursor', 'agy', 'grok', 'opencode')
BUILTIN = CLIS[:3]
# These are worker CLIs: xAI's Grok CLI is distinct from Grok Bot desktop.
PROVIDERS = dict(codex='codex', claude='claude', cursor='cursor', agy='antigravity',
                 grok='grok', opencode='opencode')
KINDS = 'agent.report,agent.blocked,agent.idle,agent.ready,agent.busy,panel.exited,pane.gone'
DEADLINE = ContextVar('deadline', default=None)
TEXT_LIMIT = 12000
WORKER_ENV = ['/usr/bin/env', '-u', 'PANE_SESSION_ID', '-u', 'PANE_PANEL_ID',
              '-u', 'PANE_ORCHESTRATION_SESSION_ID']


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
    if DEADLINE.get() is not None:
        timeout = min(timeout, DEADLINE.get() - time.monotonic())
        if timeout <= 0:
            raise DispatchError('wait_deadline')
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
    def locked(self, task_id=None):
        name = digest(task_id) + '.lock' if task_id is not None else 'lock'
        fd = os.open(self.root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = (DEADLINE.get() - time.monotonic()) if DEADLINE.get() else 0.05
                    if remaining <= 0:
                        raise DispatchError('wait_deadline')
                    time.sleep(min(0.05, remaining))
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
    if path and (name not in BUILTIN or name == 'claude'):
        try:
            proc = subprocess.run([path, '--help'], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=10, shell=False)
            help_text = proc.stdout + proc.stderr
            required = {'agy': ('--prompt-interactive',), 'grok': ('[PROMPT]', '--no-subagents', '--session-id'),
                        'opencode': ('--prompt',), 'claude': ('[prompt]',)}[name]
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


def envelope(task_id, prompt, record_path, pr_mode=None):
    if not Path(record_path).is_absolute():
        raise DispatchError('task_record_path_must_be_absolute')
    return ('You are the sole worker for a user-selected Pane task. The main Grok Bot desktop is the coordinator.\n'
            'Follow repository rules, except the user explicitly overrides next-pr skills and automatic merge: '
            'do not invoke next-pr skills, the persistent runner, nested workers or delegation. '
            'Work only in this Pane worktree. Do not change global skills, launchagents or permission settings.\n'
            'REPORT IDENTITY PROTOCOL (mandatory before editing files and again before every report):\n'
            'Read only the absolute task_record_path in the JSON below. Parse that JSON record and require '
            'record.task_id to equal the task_id below. Do not use another task record or modify this record. '
            'The dispatcher writes it atomically; pane_id, panel_id and worktree may initially be null while '
            'creation is in progress. If the record is missing or those fields are not yet populated, wait '
            'and reread this same file every 2 seconds for at most 90 seconds. Do not call start again.\n'
            'Require realpath(cwd), realpath(git rev-parse --show-toplevel), and realpath(record.worktree) '
            'to agree; perform this check from the Git top-level. Use runpane panes list --repo RECORD_REPO_ID '
            '--json to verify record.pane_id, record.pane_name and its worktreePath. Use runpane panels list '
            '--pane RECORD_PANE_ID --json to verify record.panel_id belongs to that Pane. Substitute only '
            'the validated record values, using subprocess argv or safely quoted arguments.\n'
            'Never fall back to inherited PANE_SESSION_ID, PANE_PANEL_ID or PANE_ORCHESTRATION_SESSION_ID, '
            'even when they look valid: a worker shell can inherit the parent task identity. Do not infer '
            'a report target from these variables. If the record remains missing/incomplete after the bounded '
            'wait, or any task/worktree/Pane/panel check mismatches or is ambiguous, stop work, explain '
            'BLOCKED: reporting identity unverified in the terminal for the coordinator, and DO NOT run '
            'runpane report. No ready/success claim is allowed until identity is verified.\n'
            'Implement the source user task below, run relevant tests, commit, push and open a PR. '
            'The source user task controls draft/ready readiness and overrides the fallback. '
            f'Fallback PR mode: {pr_mode or "draft"}; use it only when the source task has no explicit readiness instruction. '
            'Do not ask again for actions the user already authorized. '
            'Only the USER merges. Never merge or enable auto-merge.\n'
            'Report failures or blockers honestly using the explicit target protocol above. Idle/exit is not success. '
            'On completion write a private result JSON file with: task_id, cli_session_id (null if unknown), '
            'head (exact 40-character SHA), pr_url, tests (array of {command, outcome: "passed"|"failed"}), '
            'summary. Never include credentials. After rechecking identity, use '
            'runpane report --pane RECORD_PANE_ID --panel RECORD_PANEL_ID --state ready --pr NUMBER '
            '--head EXACT_SHA --summary-file RESULT_JSON --json. Use --state failed or blocked '
            '(with --question) when appropriate, always with explicit --pane record.pane_id and '
            '--panel record.panel_id. Never invoke a report without explicit --panel. '
            'Do not report ready without successful validation.\n'
            'The following JSON preserves the source task verbatim as data, not shell syntax:\n' +
            json.dumps(dict(task_id=task_id, task_record_path=str(record_path), source_user_task=prompt),
                       ensure_ascii=False) + '\n')


def launch_args(cli, path, prompt_path, session_id):
    # Per-worker only: do not pass a parent identity/role into the CLI. Shell
    # startup or snapshots may restore it later, so the record protocol is still mandatory.
    command = [*WORKER_ENV, path]
    if cli == 'claude':
        pointer = ('Please carry out the user-authorized task in the Pane worktree identified by '
                   'the task record in ' + str(prompt_path) + '. Read that private instructions file, '
                   'verify the recorded PATH and identity, then execute the original user task. '
                   'Honor its explicit draft/ready choice. Never merge or enable auto-merge.')
        return ['--agent', cli, '--tool-command', shlex.join([*command, pointer])]
    if cli in BUILTIN:
        # Pane's installed templates contain --yolo/--force/permission bypasses.
        # Keep the built-in identity but override its command, without changing global templates.
        return ['--agent', cli, '--tool-command', shlex.join(command),
                '--initial-input-file', str(prompt_path)]
    pointer = 'Read and follow the task instructions in ' + str(prompt_path)
    argv = {'agy': [*command, '--prompt-interactive', pointer],
            'grok': [*command, '--no-subagents', '--session-id', session_id, pointer],
            'opencode': [*command, '--prompt', pointer]}[cli]
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
    if record.get('worktree') and Path(record['worktree']).resolve() != Path(match['worktreePath']).resolve():
        record['reconcile_error'] = 'worktree_identity_changed'
        return
    record.update(pane_id=match['id'], worktree=match['worktreePath'])
    record.pop('reconcile_error', None)
    if record['state'] == 'creation_unknown':
        record['state'] = 'needs_inspection'


def start(store, task_id, repo, cli, prompt, auto=False, pr_mode=None):
    with store.locked(task_id):
        return _start(store, task_id, repo, cli, prompt, auto, pr_mode)


def _start(store, task_id, repo, cli, prompt, auto=False, pr_mode=None):
    if bool(cli) == bool(auto):
        raise DispatchError('select_explicit_cli_or_auto')
    if not task_id or len(task_id) > 200:
        raise DispatchError('invalid_task_id')
    request = dict(repo=repo, selection=cli or 'auto', prompt_sha256=digest(prompt))
    if pr_mode:
        request['pr_mode'] = pr_mode
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
        private_write(prompt_path, envelope(task_id, prompt, store.path(task_id), pr_mode))
        record = dict(task_id=task_id, request=request, repo_id=matches[0]['id'],
                      task_record_path=str(store.path(task_id)),
                      repo_path=matches[0]['path'], cli=cli, cli_path=info['path'],
                      pane_name=name, pane_id=None, panel_id=None, worktree=None,
                      cli_session_id=str(uuid.uuid4()) if cli == 'grok' else None,
                      state='creation_unknown', created_at=now(), pr=None, evidence=None,
                      pr_mode=pr_mode, pr_mode_policy='source_task_overrides_fallback',
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
    raw_summary = report.get('summary')
    result = dict(provenance='pane_worker_report', independently_verified=False,
                  reported_at=report.get('reportedAt'), state=report.get('state'),
                  head=report.get('head'), pr=report.get('pr'), complete=False,
                  question=report.get('question') if isinstance(report.get('question'), str) else None,
                  summary=raw_summary if isinstance(raw_summary, str) else None)
    try:
        summary = json.loads(raw_summary or '')
        # Expose only the human summary from a structured report, not arbitrary fields.
        result['summary'] = (summary.get('summary') if isinstance(summary, dict) and
                             isinstance(summary.get('summary'), str) else None)
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


def journal(record, timeout_ms=0):
    events = pane('watch', '--pane', record['pane_id'], '--since', str(record.get('watch_cursor', 0)),
                  '--timeout-ms', str(timeout_ms), '--kinds', KINDS, '--include-shells',
                  ndjson=True, timeout=timeout_ms / 1000 + 2)
    generations = [e['gen'] for e in events if type(e.get('gen')) is int]
    if any(e.get('kind') == '_reset' for e in events):
        record['watch_cursor'] = 0
    if generations:
        record['watch_cursor'] = max(generations)
    record['journal_warnings'] = [e['kind'] for e in events if e.get('kind') in ('_reset', '_dropped', '_error')]
    scoped = [e for e in events if e.get('paneId') == record['pane_id'] and
              (e.get('panelId') == record.get('panel_id') or e.get('kind') == 'pane.gone')]
    record['events'] = (record.get('events', []) +
                        [pick(e, ('gen', 'at', 'kind', 'exitCode')) for e in scoped])[-50:]
    return scoped


def panel_identity(record):
    reconcile(record)
    if record.get('reconcile_error'):
        raise DispatchError(record['reconcile_error'])
    repos = pane('repos', 'list')['repos']
    if not any(str(r['id']) == str(record['repo_id']) and
               Path(r['path']).resolve() == Path(record['repo_path']).resolve() for r in repos):
        raise DispatchError('repo_identity_changed')
    panels = pane('panels', 'list', '--pane', record['pane_id'])['panels']
    if not record.get('panel_id'):
        candidates = [p for p in panels if record.get('launch_command') and
                      p.get('launchCommand') == record['launch_command']]
        if len(candidates) == 1:
            record['panel_id'] = candidates[0]['id']
    matches = [p for p in panels if p['id'] == record.get('panel_id')]
    if len(matches) != 1 or matches[0].get('paneId', record['pane_id']) != record['pane_id']:
        raise DispatchError('panel_identity_changed')
    if matches[0].get('launchCommand') and matches[0]['launchCommand'] != record.get('launch_command'):
        raise DispatchError('panel_launch_changed')
    return matches[0]


def checked_panel_read(record, command, limit):
    result = pane('panels', command, '--panel', record['panel_id'], '--limit', str(limit))
    if result.get('paneId') != record['pane_id'] or result.get('panelId') != record['panel_id']:
        raise DispatchError('panel_read_identity_mismatch')
    return result


def screen_text(text):
    # Remove ANSI, trailing whitespace and volatile spinner/timing/status chrome.
    # This is only a terminal excerpt: never infer a question or authorization.
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text or '')
    lines = [line.rstrip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line.strip() and not
                    re.match(r'^\s*[✻✽✶✳✢·⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏].*(?:\d|…|\.\.\.)', line))[-TEXT_LIMIT:]


def menu_options(text):
    # Conservative, explicit TUI signature. Prose numbered questions alone are not a menu.
    options = re.findall(r'^\s*[❯>›]?\s*([1-9])[.)]\s+(.+)$', text, re.M)
    selected = bool(re.search(r'^\s*[❯>›]\s*[1-9][.)]\s+', text, re.M))
    hint = bool(re.search(r'(?:↑|↓|arrow keys|enter to (?:select|confirm)|select an option)', text, re.I))
    return [dict(key=key, text=value) for key, value in options] if len(options) >= 2 and (selected or hint) else []


def observe(record):
    panel = panel_identity(record)
    try:
        scoped = journal(record)
        record.pop('watch_error', None)
    except DispatchError as exc:
        scoped = []
        record['watch_error'] = str(exc)
    errors = {}
    reads = {}
    for command, limit in (('last-message', TEXT_LIMIT), ('screen', 80)):
        try:
            reads[command] = checked_panel_read(record, command, limit)
        except DispatchError as exc:
            if str(exc) == 'panel_read_identity_mismatch':
                raise
            errors[command] = str(exc)
    if DEADLINE.get() is not None and time.monotonic() >= DEADLINE.get():
        raise DispatchError('wait_deadline')
    screen = reads.get('screen', {})
    activity = pick(screen.get('state', {}), ('initialized', 'activityStatus', 'isCliReady', 'isCliPanel', 'lastActivity'))
    record['panel_activity'] = activity
    text = screen_text(screen.get('text', ''))
    screen_truncated = bool(screen.get('hasMore')) or len(screen.get('text', '')) > TEXT_LIMIT
    record['terminal_evidence'] = dict(provenance='pane_terminal_screen', source=screen.get('source'),
                                       excerpt=text, truncated=screen_truncated,
                                       untrusted=True)
    options = menu_options(text)
    message = reads.get('last-message', {})
    content_signature = digest(message.get('text', '') if message.get('text') and not options else text)
    report = panel.get('report')
    evidence = report_evidence(report, record['task_id']) if report else None
    record['evidence'] = evidence
    report_key = digest(json.dumps(report, sort_keys=True)) if report else None
    # Reports persist even when a human resumes the panel outside this dispatcher.
    # Active work contradicts a blocked report; retain it as evidence, never as a
    # pending prompt. Remember this across idle transitions until the report changes.
    if evidence and evidence['state'] == 'blocked' and activity.get('activityStatus') == 'active':
        record['superseded_blocked_report'] = report_key
    superseded = report_key is not None and record.get('superseded_blocked_report') == report_key
    if superseded:
        evidence.update(superseded=True, superseded_reason='observed_active_panel')
    # Persistent reports remain visible after reply, but must not resurrect a consumed question.
    consumed = record.get('consumed_report') == report_key and report_key is not None
    if evidence and not consumed and not superseded:
        state = report.get('state')
        record['state'] = ('reported_ready' if evidence['complete'] else 'incomplete_report') if state in ('ready', 'done') else (
            'reported_' + str(state) if state in ('failed', 'blocked') else 'incomplete_report')
        if evidence['complete']:
            record['pr'] = dict(url=evidence['pr_url'], number=evidence['pr'], head=evidence['head'])
            if evidence.get('cli_session_id'):
                record['cli_session_id'] = evidence['cli_session_id']
    else:
        kind = scoped[-1]['kind'] if scoped else None
        if activity.get('activityStatus') == 'active' and not options:
            record['state'] = 'working'
        elif activity.get('activityStatus') == 'idle' or options or kind in (
                'agent.idle', 'agent.ready', 'agent.blocked', 'panel.exited', 'pane.gone'):
            record['state'] = 'needs_attention'
    source, excerpt, truncated = 'pane_terminal_screen', text, screen_truncated
    if message.get('text') and not options:
        source, excerpt = 'pane_agent_last_message', message['text'][-TEXT_LIMIT:]
        truncated = bool(message.get('truncated')) or len(message['text']) > TEXT_LIMIT
    question = None
    if evidence and not superseded and evidence.get('question') and not options and (not consumed or
            content_signature == record.get('consumed_content_signature')):
        source, excerpt, question = 'pane_worker_report', evidence['question'][-TEXT_LIMIT:], evidence['question'][-TEXT_LIMIT:]
        truncated = len(evidence['question']) > TEXT_LIMIT
    # Activity is separate evidence, not a new message: last-message has no timestamp.
    # In particular, becoming busy must not resurrect the question we just answered.
    previous = record.get('conversation') or {}
    conversation = dict(provenance=source, excerpt=excerpt, question=question,
                        options=options, truncated=truncated, untrusted=True,
                        activity=activity.get('activityStatus'), read_errors=errors,
                        held_input=bool(screen.get('composer', {}).get('hasUndeliveredText')),
                        report_key=report_key, content_signature=content_signature)
    # Freeze the legacy epoch so upgrading does not invalidate a pending prompt's ID.
    fingerprint = dict(pane=record['pane_id'], panel=record['panel_id'], source=source,
                       text=excerpt, options=options, epoch=record.get('turn_epoch', 0))
    if source == 'pane_worker_report':
        fingerprint['report'] = report_key
    signature = digest(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False))
    if signature != record.get('conversation_signature'):
        record['conversation_revision'] = record.get('conversation_revision', 0) + 1
    record['conversation_signature'] = signature
    conversation['event_id'] = digest(signature + ':' + str(record['conversation_revision']))
    conversation['new'] = conversation['event_id'] != previous.get('event_id')
    conversation['replyable'] = (bool(excerpt) and activity.get('isCliPanel') is True and
                                not conversation['held_input'] and
                                record['state'] not in ('reported_ready', 'reported_failed', 'incomplete_report') and
                                (bool(options) or activity.get('activityStatus') == 'idle'))
    # Submission/unknown intents consume the event; confirmed navigation does not.
    conversation['consumed'] = (any(r['event_id'] == conversation['event_id'] and r.get('consumes_event', True)
                                   for r in record.get('replies', {}).values()) or
                               (not options and record.get('consumed_content_signature') == content_signature and
                                record.get('consumed_report') == report_key))
    if conversation['consumed']:
        conversation.update(new=False, replyable=False)
        if record['state'] == 'needs_attention':
            record['state'] = 'awaiting_reply_evidence'
    terminal = next((e for e in reversed(scoped) if e.get('kind') in ('panel.exited', 'pane.gone')), None)
    if terminal:
        record['terminal_event'] = pick(terminal, ('gen', 'at', 'kind', 'exitCode'))
    if record.get('terminal_event'):
        # A consumed transcript must not suppress termination or permit input into a shell.
        if record['state'] not in ('reported_ready', 'reported_failed', 'incomplete_report'):
            record['state'] = 'needs_attention'
        conversation['replyable'] = False
    record['conversation'] = conversation
    record['pane_link'] = 'pane://open?' + urlencode(dict(pane=record['pane_id'], panel=record['panel_id']))
    latest = record.get('last_reply_id')
    if latest and activity.get('activityStatus') == 'active':
        record['replies'][latest]['resume_evidence'] = dict(provenance='pane_panel_activity', observed_at=now(), **activity)
    record.pop('status_error', None)


def status(store, task_id):
    with store.locked(task_id):
        record = store.get(task_id)
        if not record:
            raise DispatchError('unknown_task')
        try:
            observe(record)
        except DispatchError as exc:
            record['status_error'] = str(exc)
            record['state'] = 'needs_attention'
            if record.get('conversation'):
                record['conversation'].update(new=False, replyable=False, stale=True)
        record['wait_argv'] = ['pane-dispatch', '--state-dir', str(store.root), 'wait', '--task-id', task_id,
                               '--timeout-seconds', '45']
        store.save(record)
        return record


def wait(store, task_id, timeout_seconds=45):
    if not 0 < timeout_seconds <= 45:
        raise DispatchError('timeout_seconds_must_be_between_0_and_45')
    deadline = time.monotonic() + timeout_seconds
    token = DEADLINE.set(deadline)
    record = None
    try:
        # Baseline FIRST: the question may predate the first journal subscription.
        while True:
            record = status(store, task_id)
            if record.get('status_error') in ('wait_deadline', 'process_timeout') and time.monotonic() >= deadline:
                break
            conversation = record.get('conversation', {})
            if record.get('status_error') or record['state'] in ('needs_attention', 'reported_blocked',
                    'reported_failed', 'reported_ready', 'incomplete_report') or conversation.get('new'):
                return dict(outcome='update', task=record)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # No state lock while blocked in the daemon. Re-read/merge cursor at next status.
            journal(record, max(1, int(remaining * 1000)))
            # status drains from the persisted cursor, so no wakeup can be lost here.
    except DispatchError as exc:
        if str(exc) not in ('wait_deadline', 'process_timeout'):
            return dict(outcome='needs_attention', error=str(exc), task=record)
    finally:
        DEADLINE.reset(token)
    return dict(outcome='timeout', task=record, task_id=task_id)


def reply(store, task_id, reply_text, event_id, reply_id, key=None):
    if not reply_id or len(reply_id) > 200 or not reply_text.strip() or len(reply_text) > TEXT_LIMIT:
        raise DispatchError('invalid_reply')
    if key is not None and key not in ('up', 'down', 'enter', *tuple('123456789')):
        raise DispatchError('invalid_menu_key')
    if any((ord(c) < 32 and c not in '\n\t') or 127 <= ord(c) <= 159 for c in reply_text) or reply_text.lstrip().startswith(('!', '/', '#', '@')):
        raise DispatchError('reply_must_be_plain_text')
    request = dict(event_id=event_id, reply_sha256=digest(reply_text), key=key)
    with store.locked(task_id):
        record = store.get(task_id)
        if not record:
            raise DispatchError('unknown_task')
        replies = record.setdefault('replies', {})
        if reply_id in replies:
            existing = replies[reply_id]
            if any(existing.get(k) != v for k, v in request.items()):
                raise DispatchError('reply_id_request_mismatch')
            return existing
        observe(record)  # Validates identity and current fingerprint immediately before intent/send.
        current = record['conversation']
        if event_id != current['event_id'] or not current['replyable']:
            store.save(record)
            raise DispatchError('stale_or_unreplyable_event')
        if current['options']:
            if key is None:
                raise DispatchError('menu_requires_explicit_key')
            if key.isdigit() and key not in [option['key'] for option in current['options']]:
                raise DispatchError('menu_key_not_present')
        elif key is not None:
            raise DispatchError('no_current_menu')
        path = store.root / (digest(task_id + ':' + reply_id) + '.reply')
        private_write(path, reply_text)
        navigation = key in ('up', 'down')
        delivery = dict(reply_id=reply_id, **request, delivery='unknown', intent_at=now(),
                        action='navigate' if navigation else 'submit', consumes_event=True,
                        pane_id=record['pane_id'], panel_id=record['panel_id'], resume_evidence=None)
        replies[reply_id] = delivery
        record['last_reply_id'] = reply_id
        if not navigation:
            record['consumed_report'] = current.get('report_key')
            record['consumed_content_signature'] = current['content_signature']
        record['state'] = 'awaiting_reply_evidence'
        store.save(record)  # Durable unknown intent BEFORE send. A crash must never cause a resend.
        try:
            if key is None:
                result = pane('panels', 'submit', '--panel', record['panel_id'], '--input-file', str(path), '--yes')
            else:
                payload = {'up': '\x1b[A', 'down': '\x1b[B', 'enter': '\r'}.get(key, key)
                result = pane('panels', 'input', '--panel', record['panel_id'], '--text', payload, '--yes')
            delivery.update(delivery='sent', sent_at=now(), consumes_event=not navigation,
                            evidence=pick(result, ('delivery', 'verifiedSubmitted', 'verification', 'submitted')))
            if navigation:
                record['state'] = 'needs_attention'
        except DispatchError as exc:
            delivery['error'] = str(exc)
        store.save(record)
        return delivery


def active(store):
    return [pick(r, ('task_id', 'state', 'repo_id', 'worktree', 'pane_id', 'panel_id', 'pane_link',
                     'conversation', 'last_reply_id', 'status_error', 'updated_at'))
            for r in store.all() if r.get('state') not in ('reported_ready', 'reported_failed')]


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
    start_parser.add_argument('--pr-mode', choices=('draft', 'ready'), help='Fallback only; explicit source-task instruction wins')
    subs.add_parser('active')
    wait_parser = subs.add_parser('wait')
    wait_parser.add_argument('--task-id', required=True)
    wait_parser.add_argument('--timeout-seconds', type=float, default=45)
    reply_parser = subs.add_parser('reply')
    for flag in ('task-id', 'event-id', 'reply-id'):
        reply_parser.add_argument('--' + flag, required=True)
    reply_parser.add_argument('--reply-file', type=Path, required=True)
    reply_parser.add_argument('--key', choices=('up', 'down', 'enter', *tuple('123456789')),
                              help='One explicitly user-selected TUI key; observe again before the next key')
    for name in ('status', 'usage'):
        subs.add_parser(name).add_argument('--task-id', required=True)
    try:
        args = parser.parse_args(argv)
        store = Store(args.state_dir)
        if args.command == 'start':
            result = start(store, args.task_id, args.repo, args.cli, args.prompt_file.read_text(), args.auto, args.pr_mode)
        elif args.command == 'active':
            result = active(store)
        elif args.command == 'wait':
            result = wait(store, args.task_id, args.timeout_seconds)
        elif args.command == 'reply':
            result = reply(store, args.task_id, args.reply_file.read_text(), args.event_id, args.reply_id, args.key)
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
