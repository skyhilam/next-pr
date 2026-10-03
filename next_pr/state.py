"""Durable facts. Callers serialize mutations with state.lock."""
import json
import os
from pathlib import Path
import re
import subprocess
import sqlite3
import uuid

from .common import Blocked, command, now, read_json

TERMINAL = {'merged', 'mergeable', 'cancelled'}
PROVIDERS = ('codex', 'claude', 'grok', 'cursor')


def default_home():
    return Path(os.environ.get('NEXT_PR_HOME', '~/Library/Application Support/next-pr')).expanduser()


def initial_config(repo_path):
    return {
        'version': 1,
        'repos': {'skyhilam/dkdm-monorepo': {
            'path': str(Path(repo_path).resolve()), 'base': 'main', 'enabled': True,
            'auto_merge': True, 'validation': [],
        }},
        'providers': {name: {'enabled': name == 'codex', 'billing_confirmed': False,
                             'binary': 'agent' if name == 'cursor' else name}
                      for name in PROVIDERS},
    }


def config(home):
    value = read_json(Path(home) / 'config.json')
    if value.get('version') != 1:
        raise Blocked('unsupported configuration version')
    return value


def repo_config(cfg, repo):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise Blocked('invalid repository name')
    item = cfg.get('repos', {}).get(repo)
    if not item or not item.get('enabled'):
        raise Blocked('repository is not enabled in the explicit allowlist')
    path = Path(item['path']).resolve()
    origin = command(['git', '-C', str(path), 'remote', 'get-url', 'origin'])
    allowed = (f'git@github.com:{repo}', f'https://github.com/{repo}',
               f'ssh://git@github.com/{repo}')
    if origin.removesuffix('.git') not in allowed:
        raise Blocked('allowlisted checkout origin does not match repository')
    command(['git', 'check-ref-format', '--branch', item['base']])
    if not isinstance(item.get('validation', []), list) or any(
            not isinstance(arg, str) or not arg for arg in item.get('validation', [])):
        raise Blocked('validation must be a single argv array of nonempty strings')
    return item


class Store:
    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        ancestor = self.home
        while not ancestor.exists():
            ancestor = ancestor.parent
        probe = subprocess.run(['git', '-C', str(ancestor), 'rev-parse', '--show-toplevel'],
                               text=True, capture_output=True)
        if probe.returncode == 0:
            raise Blocked('state directory must be outside every Git checkout')
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.home, 0o700)
        self.db = sqlite3.connect(self.home / 'state.sqlite3')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, data TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def tasks(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM tasks ORDER BY rowid')]

    def task(self, task_id):
        row = self.db.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise Blocked('unknown task')
        return json.loads(row[0])

    def save(self, task):
        task['updated_at'] = now()
        with self.db:
            self.db.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(task), task['id']))

    def runs(self, task_id=None):
        rows = self.db.execute('SELECT data FROM runs' + (' WHERE task_id=?' if task_id else ''),
                               (task_id,) if task_id else ())
        return [json.loads(row[0]) for row in rows]

    def save_run(self, run):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?)',
                            (run['id'], run['task_id'], json.dumps(run)))

    def meta(self, key, default=None):
        row = self.db.execute('SELECT data FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    def submit(self, repo, title, prompt, request_key, scopes, dependencies):
        old = self.db.execute('SELECT data FROM tasks WHERE request_key=?', (request_key,)).fetchone()
        if old:
            task = json.loads(old[0])
            if (task['repo'], task['title'], task['prompt'], task['scopes'], task['dependencies']) != (
                    repo, title, prompt, scopes, dependencies):
                raise Blocked('request key already belongs to different input')
            return task
        for dependency in dependencies:
            if self.task(dependency)['repo'] != repo:
                raise Blocked('dependencies must belong to the same repository')
        for scope in scopes:
            if scope != '*' and (scope.startswith('/') or '..' in Path(scope).parts):
                raise Blocked('scopes must be relative paths without parent traversal')
        for prior in self.tasks():
            if prior['repo'] == repo and prior['stage'] not in {'merged', 'cancelled'} and overlaps(scopes, prior['scopes']):
                if prior['id'] not in dependencies:
                    raise Blocked(f'overlapping work requires --depends-on {prior["id"]}')
        task_id = uuid.uuid4().hex[:16]
        task = dict(id=task_id, request_key=request_key, repo=repo, title=title,
                    prompt=prompt, scopes=scopes, dependencies=dependencies,
                    writer=('codex', 'claude')[len(self.tasks()) % 2], stage='queued',
                    fix_round=0, created_at=now(), updated_at=now(), run_id=None,
                    review_sha=None, ci_sha=None, paused=False, note='', evidence=[])
        with self.db:
            self.db.execute('INSERT INTO tasks VALUES (?,?,?)', (task_id, request_key, json.dumps(task)))
        return task


def overlaps(left, right):
    return any(a == '*' or b == '*' or a.rstrip('/') == b.rstrip('/') or
               a.startswith(b.rstrip('/') + '/') or b.startswith(a.rstrip('/') + '/')
               for a in left for b in right)
