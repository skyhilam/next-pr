"""Detached supervisor: actual child exit produces an atomic durable receipt.

Run locks are inherited by the CLI. Killing only this supervisor cannot authorize
another writer. Cancellation is a request file handled by the owning supervisor,
never a signal to a PID recovered from a database.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .common import Blocked, atomic_json, lock, now, read_json
from .providers import safe_environment


def group_alive(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def execute(manifest_path):
    manifest = read_json(manifest_path)
    directory = Path(manifest_path).parent
    receipt = directory / 'receipt.json'
    with lock(directory / 'run.lock', blocking=False) as run_lock:
        if receipt.exists():
            return
        # A runner is single-use, including after a supervisor crash.
        if (directory / 'started.json').exists():
            raise Blocked('runner already started; refuse duplicate execution')
        atomic_json(directory / 'started.json', {'run_id': manifest['id'], 'pid': os.getpid(),
                                                 'started_at': now()})
        result = dict(run_id=manifest['id'], started_at=now(), exit_code=None,
                      cancelled=False, safe_to_retry=True)
        try:
            if manifest['role'] == 'validation':
                while True:
                    try:
                        with lock(Path(manifest['home']) / 'validation.lock', blocking=False) as validation_lock:
                            result.update(run_child(manifest, directory, (run_lock.fileno(), validation_lock.fileno())))
                        break
                    except Blocked:
                        if (directory / 'stop.json').exists():
                            result.update(cancelled=True, exit_code=-15)
                            break
                        time.sleep(0.5)
            else:
                result.update(run_child(manifest, directory, (run_lock.fileno(),)))
        except Exception as error:
            result.update(error=str(error), safe_to_retry=False)
        result['finished_at'] = now()
        atomic_json(receipt, result)


def run_child(manifest, directory, lock_fds):
    if (directory / 'stop.json').exists():
        return dict(cancelled=True, exit_code=-15)
    env = safe_environment() if manifest['role'] != 'validation' else dict(os.environ)
    with (directory / 'stdout.log').open('w') as stdout, (directory / 'stderr.log').open('w') as stderr:
        child = subprocess.Popen(manifest['argv'], cwd=manifest['worktree'], env=env,
                                 stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                 start_new_session=True, pass_fds=lock_fds)
        atomic_json(directory / 'child.json', {'run_id': manifest['id'], 'pid': child.pid,
                                              'started_at': now()})
        cancelled, stop_at = False, None
        while child.poll() is None:
            if (directory / 'stop.json').exists():
                if stop_at is None:
                    cancelled, stop_at = True, now()
                    os.killpg(child.pid, signal.SIGTERM)
                elif now() - stop_at > 10:
                    os.killpg(child.pid, signal.SIGKILL)
            time.sleep(0.25)
        # Detached/background children make handoff unsafe, even on exit code zero.
        return dict(exit_code=child.returncode, cancelled=cancelled,
                    safe_to_retry=not group_alive(child.pid))


if __name__ == '__main__':
    try:
        execute(sys.argv[1])
    except Blocked as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
