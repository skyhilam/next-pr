"""Small operating-system boundaries shared by the coordinator and runners."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


class Blocked(RuntimeError):
    pass


def command(argv, cwd=None, env=None):
    result = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode:
        raise Blocked(f'{argv[0]} failed ({result.returncode}): {result.stderr[-2000:]}')
    return result.stdout.strip()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    return json.loads(Path(path).read_text())


@contextlib.contextmanager
def lock(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Blocked(f'lock held: {path.name}') from None
        # Close this descriptor on exit; do not explicitly unlock the shared open
        # file description. Children may still hold inherited copies after this
        # process exits, and their lock must remain until the last copy closes.
        yield handle


def lock_held(path):
    try:
        with lock(path, blocking=False):
            return False
    except Blocked:
        return True


def now():
    return time.time()
