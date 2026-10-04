"""Local operator interface. All control mutations share the daemon's state lock."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import signal
import sys
import time

from . import engine, providers, ui
from .common import Blocked, atomic_json, command, lock, now, read_json
from .state import Store, config, default_home, initial_config, repo_config


def parser():
    p = argparse.ArgumentParser(description='Persistent subscription-only local PR coordinator')
    p.add_argument('--home', type=Path, default=default_home(), help='Private external state directory')
    sub = p.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', help='Write inactive initial configuration (dkdm allowlist only)')
    init.add_argument('--repo-path', required=True)
    submit = sub.add_parser('submit', help='Queue one request idempotently')
    submit.add_argument('--repo', default='skyhilam/dkdm-monorepo')
    submit.add_argument('--title', required=True)
    submit.add_argument('--prompt-file', type=Path, required=True)
    submit.add_argument('--request-key', required=True)
    submit.add_argument('--scope', action='append', default=[])
    submit.add_argument('--depends-on', action='append', default=[])
    sub.add_parser('status', help='Show durable task/provider facts; unknown quotas stay unknown')
    sub.add_parser('metrics', help='Report first ten tasks, durations, rework and emitted usage')
    page = sub.add_parser('ui', help='Serve the local task and conversation dashboard')
    page.add_argument('--port', type=int, default=8765, help='Loopback HTTP port (default: 8765)')
    for name in ('pause', 'resume', 'cancel'):
        control = sub.add_parser(name)
        control.add_argument('task', nargs='?', help='Task id (omit pause/resume for global scheduling)')
        if name == 'resume':
            control.add_argument('--handoff', choices=providers_names())
            control.add_argument('--provider', choices=providers_names())
    recover = sub.add_parser('recover', help='Manually close an uncertain run after process inspection')
    recover.add_argument('task')
    recover.add_argument('--confirm-no-processes', action='store_true', required=True)
    confirm = sub.add_parser('confirm-provider', help='Verify login and record manual billing/config audit')
    confirm.add_argument('provider', choices=providers_names())
    confirm.add_argument('--subscription-only', action='store_true', required=True)
    confirm.add_argument('--config-audited', action='store_true', required=True)
    confirm.add_argument('--note', required=True, help='Evidence, e.g. subscription settings checked on date')
    daemon = sub.add_parser('daemon', help='Run durable scheduler in foreground')
    daemon.add_argument('--once', action='store_true')
    install = sub.add_parser('install-launchagent', help='Write LaunchAgent; explicit --load activates it')
    install.add_argument('--load', action='store_true')
    sub.add_parser('uninstall-launchagent', help='Stop/remove scheduler; workers must be paused separately')
    return p


def providers_names():
    return ('codex', 'claude', 'grok', 'cursor')


def activation_gate(cfg):
    enabled = [(name, item) for name, item in cfg['providers'].items() if item.get('enabled')]
    if not enabled:
        raise Blocked('no provider is enabled and billing verified')
    for name, _ in enabled:
        providers.gate(name, cfg, {})


def launchagent(home, load=False, remove=False):
    label = 'com.next-pr.coordinator'
    destination = Path.home() / 'Library/LaunchAgents' / f'{label}.plist'
    domain = f'gui/{os.getuid()}'
    if remove:
        # A stopped scheduler does not claim to stop its independent workers.
        command(['launchctl', 'bootout', f'{domain}/{label}'])
        destination.unlink(missing_ok=True)
        return {'removed': str(destination), 'workers': 'unchanged; use pause/cancel before uninstalling'}
    activation_gate(config(home))
    executable = Path(sys.executable).resolve()
    if not executable.is_file():
        raise Blocked('Python executable is not persistent')
    destination.parent.mkdir(parents=True, exist_ok=True)
    value = {'Label': label, 'ProgramArguments': [str(executable), '-m', 'next_pr',
             '--home', str(home), 'daemon'], 'WorkingDirectory': str(engine.ROOT),
             'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15,
             'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
             'StandardOutPath': str(home / 'daemon.stdout.log'),
             'StandardErrorPath': str(home / 'daemon.stderr.log')}
    temporary = destination.with_suffix('.tmp')
    with temporary.open('wb') as output:
        plistlib.dump(value, output)
    os.replace(temporary, destination)
    if load:
        command(['launchctl', 'bootstrap', domain, str(destination)])
    return {'plist': str(destination), 'loaded': load,
            'note': 'Keep the code directory at this path. Workers are independently supervised.'}


def recover(store, task):
    if not task['run_id']:
        raise Blocked('task has no uncertain run')
    run = next(run for run in store.runs(task['id']) if run['id'] == task['run_id'])
    directory = Path(run['directory'])
    with lock(directory / 'run.lock', blocking=False):
        if now() - run['created_at'] < 15:
            raise Blocked('run is still locked or starting')
        receipt_path = directory / 'receipt.json'
        prior_receipt = read_json(receipt_path) if receipt_path.exists() else None
        if prior_receipt and prior_receipt.get('safe_to_retry') is True:
            raise Blocked('safe receipt exists; let the daemon reconcile it instead')
        for filename in ('started.json', 'child.json'):
            path = directory / filename
            if path.exists():
                pid = read_json(path)['pid']
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise Blocked(f'process {pid} exists; inspect it manually (never signal reused PIDs)')
        if prior_receipt:
            atomic_json(directory / 'unsafe-receipt.json', prior_receipt)
        atomic_json(directory / 'receipt.json', dict(run_id=run['id'], exit_code=-15,
                    cancelled=True, safe_to_retry=True, finished_at=now(),
                    operator_attestation='No remaining worker or descendant processes after manual inspection'))
    engine.consume(store, task, run)
    return store.task(task['id'])


def metrics(store):
    result = []
    for task in store.tasks()[:10]:
        runs = store.runs(task['id'])
        result.append({'task': task['id'], 'stage': task['stage'], 'fix_rounds': task['fix_round'],
                       'elapsed_seconds': (task.get('finished_at') or now()) - task['created_at'],
                       'runs': len(runs), 'usage': [run['usage'] for run in runs if run.get('usage')],
                       'quota': 'unknown'})
    return result


def operate(args, store):
    cfg = config(store.home)
    if args.command == 'submit':
        repo_config(cfg, args.repo)
        prompt = args.prompt_file.read_text()
        if not prompt.strip() or not args.title.strip() or not args.request_key.strip():
            raise Blocked('title, request key and prompt must not be empty')
        return store.submit(args.repo, args.title, prompt, args.request_key,
                            args.scope or ['*'], args.depends_on)
    if args.command == 'status':
        return {'tasks': store.tasks(), 'runs': [{k: v for k, v in run.items() if k != 'argv'}
                                                for run in store.runs()],
                'paused': store.meta('paused', False),
                'providers': {name: {'enabled': item.get('enabled'),
                                    'billing_confirmed': item.get('billing_confirmed'),
                                    'paused': store.meta('provider_pauses', {}).get(name),
                                    'quota': 'unknown'} for name, item in cfg['providers'].items()}}
    if args.command == 'metrics':
        return metrics(store)
    if args.command == 'confirm-provider':
        item = cfg['providers'][args.provider]
        evidence = providers.verify_auth(args.provider, item)
        item.update(enabled=True, billing_confirmed=True, config_audited=True,
                    confirmation_note=args.note, auth=evidence)
        atomic_json(store.home / 'config.json', cfg)
        return {'provider': args.provider, 'auth': evidence, 'billing': 'manually confirmed'}
    if args.command == 'recover':
        return recover(store, store.task(args.task))
    if args.command in {'pause', 'resume', 'cancel'}:
        if args.command == 'resume' and args.provider:
            providers.gate(args.provider, cfg, {})
            pauses = store.meta('provider_pauses', {})
            pauses.pop(args.provider, None)
            store.set_meta('provider_pauses', pauses)
        if not args.task:
            if args.command == 'cancel':
                raise Blocked('cancel requires a task id')
            if args.command == 'resume' and args.handoff:
                raise Blocked('handoff requires a task id')
            store.set_meta('paused', args.command == 'pause')
            if args.command == 'pause':
                for task in store.tasks():
                    if task['run_id']:
                        engine.stop_task(store, task)
            return {'paused': store.meta('paused'), 'note': 'Per-task stops still require per-task resume.'}
        task = store.task(args.task)
        if args.command == 'resume':
            engine.resume_task(store, cfg, task, args.handoff)
        else:
            engine.stop_task(store, task, cancel=args.command == 'cancel')
        return store.task(args.task)
    if args.command == 'install-launchagent':
        return launchagent(store.home, args.load)
    if args.command == 'uninstall-launchagent':
        return launchagent(store.home, remove=True)
    raise Blocked('unknown command')


def daemon(store, once):
    activation_gate(config(store.home))
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with lock(store.home / 'daemon.lock', blocking=False):
        while not stopping:
            with lock(store.home / 'state.lock'):
                engine.tick(store)
            if once:
                break
            time.sleep(2)


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    store = None
    try:
        if args.command == 'ui':
            ui.serve(args.home, '127.0.0.1', args.port)
            return 0
        store = Store(args.home)
        if args.command == 'daemon':
            daemon(store, args.once)
            return 0
        with lock(store.home / 'state.lock'):
            if args.command == 'init':
                path = store.home / 'config.json'
                if path.exists():
                    raise Blocked('configuration already exists; edit it explicitly')
                value = initial_config(args.repo_path)
                repo_config(value, 'skyhilam/dkdm-monorepo')
                atomic_json(path, value)
                result = {'config': str(path), 'note': 'Inactive until provider billing confirmation.'}
            else:
                result = operate(args, store)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (Blocked, ValueError, KeyError, OSError) as error:
        print(json.dumps({'blocked': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if store is not None:
            store.close()
