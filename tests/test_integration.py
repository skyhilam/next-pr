"""Offline end-to-end pipeline using real git/processes and fake subscription CLIs/gh."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from next_pr.common import atomic_json
from next_pr.engine import SUPERVISORS, tick
from next_pr.providers import CREDENTIAL_VARIABLES
from next_pr.state import Store, initial_config

REPO = 'skyhilam/dkdm-monorepo'


class IntegrationTests(unittest.TestCase):
    def test_draft_to_guarded_merge_with_real_process_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, checkout, binaries = root / 'remote.git', root / 'checkout', root / 'bin'
            binaries.mkdir()
            env = {key: value for key, value in os.environ.items() if key not in CREDENTIAL_VARIABLES}
            env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       PATH=str(binaries) + os.pathsep + env['PATH'],
                       FAKE_PR=str(root / 'pr.json'), FAKE_REPO=str(checkout))
            def git(*args, cwd=None):
                return subprocess.run(['git', *args], cwd=cwd, env=env, check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '--bare', '--initial-branch=main', str(remote))
            git('clone', str(remote), str(checkout))
            git('config', 'user.name', 'Fixture', cwd=checkout)
            git('config', 'user.email', 'fixture@example.invalid', cwd=checkout)
            git('commit', '--allow-empty', '-m', 'initial', cwd=checkout)
            git('push', 'origin', 'main', cwd=checkout)
            git('remote', 'set-url', 'origin', 'https://github.com/' + REPO + '.git', cwd=checkout)
            git('config', 'url.' + remote.as_uri() + '.insteadOf',
                'https://github.com/' + REPO + '.git', cwd=checkout)
            # Keep production origin validation intact: the fixture git wrapper
            # exposes its logical GitHub origin while transporting only to local bare Git.
            real_git = shutil.which('git')
            wrapper = binaries / 'git'
            wrapper.write_text('#!' + sys.executable + '\n' +
                'import os, sys\n' + 'real = ' + repr(real_git) + '\n' +
                "args=sys.argv[1:]\nif args[-3:] == ['remote','get-url','origin']:\n" +
                "    args=args[:-3]+['config','--get','remote.origin.url']\n" +
                "os.execv(real,[real,*args])\n")
            wrapper.chmod(0o755)
            cli = binaries / 'fake-model'
            cli.write_text('#!' + sys.executable + '\n' + '''import json, subprocess, sys
from pathlib import Path
args=sys.argv[1:]
if args == ['login','status']:
    print('Logged in using ChatGPT'); raise SystemExit
if args == ['auth','status']:
    print(json.dumps({'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty'})); raise SystemExit
review = '--tools' in args or 'read-only' in args
cwd = args[args.index('--cd')+1] if '--cd' in args else '.'
def git(*args):
    return subprocess.check_output(['git','-C',cwd,*args],text=True).strip()
if not review:
    Path(cwd,'change.txt').write_text('A tested fixture change\\n')
    git('add','change.txt'); git('commit','-m','fixture implementation'); git('push','origin','HEAD')
head=git('rev-parse','HEAD')
print(json.dumps({'type':'result','session_id':'fake-session','usage':{'input_tokens':7},
    'result':json.dumps({'status':'no_blockers' if review else 'completed','summary':'fixture',
        'head_sha':head,'tests':['fixture checks'],'blockers':[]})}))
''')
            cli.chmod(0o755)
            gh = binaries / 'gh'
            gh.write_text('#!' + sys.executable + '\n' + '''import json, os, subprocess, sys
from pathlib import Path
path=Path(os.environ['FAKE_PR']); args=sys.argv[1:]
pr=json.loads(path.read_text()) if path.exists() else None
def head():
    return subprocess.check_output(['git','-C',os.environ['FAKE_REPO'],'rev-parse',pr['headRefName']],text=True).strip()
if args[:2] == ['pr','list']:
    print(json.dumps([pr] if pr else []))
elif args[:2] == ['pr','create']:
    pr={'number':1,'url':'https://github.com/test/repo/pull/1','state':'OPEN','isDraft':True,
        'mergeable':'MERGEABLE','mergeStateStatus':'CLEAN','baseRefName':'main',
        'headRefName':args[args.index('--head')+1]}
    path.write_text(json.dumps(pr)); print(pr['url'])
elif args[:2] == ['pr','view']:
    print(json.dumps(pr | {'headRefOid':head()}))
elif args[:2] == ['pr','ready']:
    pr['isDraft']='--undo' in args; path.write_text(json.dumps(pr))
elif args[:2] == ['pr','merge']:
    assert args[args.index('--match-head-commit')+1] == head()
    pr['state']='MERGED'; path.write_text(json.dumps(pr))
elif args[0] == 'api':
    if 'check-runs?' in args[-1]:
        print(json.dumps([{'check_runs':[{'id':1,'name':'CI summary','head_sha':head(),
            'status':'completed','conclusion':'success','app':{'id':1,'slug':'github-actions'}}]}]))
    else: print('[[]]')
else: raise RuntimeError(args)
''')
            gh.chmod(0o755)
            store = Store(root / 'state')
            try:
                cfg = initial_config(checkout)
                cfg['repos'][REPO]['validation'] = [sys.executable, '-c',
                    'from pathlib import Path; assert Path("change.txt").read_text().startswith("A tested")']
                for name in ('codex', 'claude'):
                    cfg['providers'][name].update(binary=str(cli), enabled=True,
                                                  billing_confirmed=True, config_audited=True)
                atomic_json(store.home / 'config.json', cfg)
                task = store.submit(REPO, 'Fixture request', 'Create a change', 'fixture', ['*'], [])
                with patch.dict(os.environ, env, clear=True):
                    deadline = time.time() + 15
                    while time.time() < deadline:
                        tick(store)
                        task = store.task(task['id'])
                        if task['stage'] in {'merged', 'blocked'}:
                            break
                        time.sleep(0.05)
                self.assertEqual(task['stage'], 'merged', task.get('note'))
                self.assertEqual(len(store.runs()), 3)
                self.assertTrue(all(run['consumed'] for run in store.runs()))
                self.assertEqual(task['review_sha'], task['ci_sha'])
                self.assertFalse(git('ls-remote', '--heads', str(remote), task['branch']))
                self.assertEqual((Path(task['worktree']) / 'change.txt').read_text(), 'A tested fixture change\n')
            finally:
                for process in SUPERVISORS:
                    process.wait(timeout=5)
                SUPERVISORS.clear()
                store.close()
