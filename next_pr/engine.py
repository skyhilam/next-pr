"""One conservative state machine. External actions are preceded by durable intent."""
import json
from pathlib import Path
import subprocess
import sys
import uuid

from . import github, providers
from .common import Blocked, atomic_json, lock, lock_held, now, read_json
from .state import TERMINAL, config, repo_config

ROOT = Path(__file__).resolve().parent.parent
SUPERVISORS = []  # Only for reaping this manager's children; never used as durable identity.


def block(store, task, note):
    if task['stage'] != 'blocked':
        task['resume_stage'] = task['stage']
    task.update(stage='blocked', note=note)
    store.save(task)


def prompt_for(task, role, head, home):
    contract = ('Your final response MUST be a single JSON object: '
                '{"status":"completed|blocked|rate_limited|no_blockers|blockers",'
                '"summary":"...","head_sha":"full SHA","tests":["commands and results"],'
                '"blockers":["actionable finding"]}. '
                'Report permission or missing-decision blocks explicitly; never bypass permissions. ')
    if role == 'review':
        reference = (ROOT / 'references/code-review.md').read_text()
        return (f'Read-only independent review of {task["repo"]} at EXACT HEAD {head}. '
                'Do not edit, commit, push, ready, merge, or run tests. '
                'Use status no_blockers or blockers. An uncertain review is blocked.\n'
                + contract + '\n' + reference + '\nDiff follows:\n'
                + github.git(task['worktree'], 'diff', f'origin/{task["base"]}...{head}'))
    return (f'Implement this request only in {task["worktree"]}, branch {task["branch"]}. '
            'You are the sole writer. Do not spawn agents or invoke /next-pr or /implement. '
            'Do not create, ready, merge or close PRs. Commit and push this branch, never force-push. '
            'Respect repository instructions and existing permissions. Never modify shared checkouts. '
            'Do not run heavy validation/builds directly; the coordinator serializes the configured '
            'validation command after you finish. Perform lightweight targeted checks as appropriate. '
            'Sunmi hardware acceptance is manual. If blocked, checkpoint safely and describe why.\n'
            + contract + '\nUser request:\n' + task['prompt']
            + '\nPrevious checkpoint / required fixes:\n' + task.get('feedback', '')
            + '\nAvailable previous evidence:\n' + json.dumps(task.get('evidence', [])[-3:]))


def start_run(store, cfg, task, role):
    head = github.reconciled_head(task)
    provider = task['writer'] if role != 'review' else ('claude' if task['writer'] == 'codex' else 'codex')
    if role == 'validation':
        args = repo_config(cfg, task['repo']).get('validation', [])
        if not args:
            raise Blocked('repository validation argv is not configured; configure it before continuing')
        provider = None
    else:
        item = providers.gate(provider, cfg, store.meta('provider_pauses', {}))
    run_id = uuid.uuid4().hex
    directory = store.home / 'runs' / run_id
    directory.mkdir(parents=True, mode=0o700)
    if role != 'validation':
        prompt = directory / 'prompt.txt'
        prompt.write_text(prompt_for(task, role, head, store.home))
        args = providers.argv(provider, item, role, task['worktree'], prompt)
    manifest = dict(id=run_id, task_id=task['id'], role=role, provider=provider,
                    head_sha=head, argv=args, worktree=task['worktree'], home=str(store.home),
                    created_at=now(), consumed=False, directory=str(directory))
    atomic_json(directory / 'manifest.json', manifest)
    # Persist run AND assignment atomically before spawning. A crash here is ambiguous
    # and deliberately blocks; it does not create a second writer.
    task['run_id'] = run_id
    task['note'] = ''
    with store.db:
        store.db.execute('INSERT INTO runs VALUES (?,?,?)', (run_id, task['id'], json.dumps(manifest)))
        store.db.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(task), task['id']))
    with (directory / 'supervisor.log').open('a') as log:
        SUPERVISORS.append(subprocess.Popen(
            [sys.executable, '-m', 'next_pr.runner', str(directory / 'manifest.json')],
            cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True))


def consume(store, task, run):
    directory = Path(run['directory'])
    receipt_path = directory / 'receipt.json'
    if not receipt_path.exists():
        if lock_held(directory / 'run.lock'):
            return
        if now() - run['created_at'] < 15:
            return
        block(store, task, 'run disappeared or startup is uncertain; no completion receipt. '
              'Inspect logs/processes; no automatic retry or handoff is allowed.')
        return
    if lock_held(directory / 'run.lock'):
        return
    receipt = read_json(receipt_path)
    if receipt.get('run_id') != run['id'] or receipt.get('safe_to_retry') is not True:
        block(store, task, 'invalid receipt or surviving descendants; manual reconciliation required')
        return
    # Result interpretation and task/run consumption commit together below.
    run['receipt'] = receipt
    run['consumed'] = True
    task['run_id'] = None
    try:
        if task.get('cancel_requested'):
            task.update(stage='cancelled', note='Worker stopped; draft/worktree retained.', finished_at=now())
        elif receipt.get('cancelled'):
            raise Blocked('worker stopped by pause; inspect/checkpoint worktree before resume')
        elif receipt.get('error'):
            raise Blocked(receipt['error'])
        elif run['role'] == 'validation':
            task['evidence'].append({'run_id': run['id'], 'role': 'validation',
                                     'head_sha': run['head_sha'], 'exit_code': receipt['exit_code'],
                                     'log': str(directory / 'stdout.log')})
            if github.reconciled_head(task) != run['head_sha']:
                raise Blocked('head changed during validation')
            if receipt['exit_code'] != 0:
                request_fix(task, 'Configured validation failed; inspect ' + str(directory / 'stderr.log'))
            else:
                task['validated_sha'] = run['head_sha']
                task['stage'] = 'review'
        else:
            result = providers.parse_output(directory / 'stdout.log')
            run.update(session_id=result['session_id'], usage=result['usage'], result=result)
            task['evidence'].append({'run_id': run['id'], 'provider': run['provider'],
                                     'role': run['role'], 'tests': result['tests'],
                                     'summary': result['summary'], 'log': str(directory / 'stdout.log')})
            if result['status'] == 'rate_limited':
                pauses = store.meta('provider_pauses', {})
                pauses[run['provider']] = {'at': now(), 'task_id': task['id']}
                store.set_meta('provider_pauses', pauses)
                raise Blocked('provider rate limited; stopped receipt saved. Resume provider or explicitly hand off.')
            if result['status'] == 'blocked':
                raise Blocked(result['summary'])
            if receipt['exit_code'] != 0:
                raise Blocked(f'CLI exit {receipt["exit_code"]}; inspect stderr before retry')
            head = github.reconciled_head(task)
            if result.get('head_sha') != head:
                raise Blocked('worker final SHA does not match local and remote HEAD')
            if run['role'] == 'review':
                if head != run['head_sha'] or head != task.get('validated_sha'):
                    task.update(stage='validation', review_sha=None)
                elif result['status'] == 'blockers':
                    if not result['blockers']:
                        raise Blocked('review reported blockers without findings')
                    request_fix(task, '\n'.join(result['blockers']))
                elif result['status'] == 'no_blockers' and not result['blockers']:
                    task.update(review_sha=head, stage='ready')
                else:
                    raise Blocked('review contract did not provide a clean independent verdict')
            elif result['status'] == 'completed':
                task.update(stage='validation', review_sha=None)
            else:
                raise Blocked('writer returned a reviewer-only outcome')
    except Blocked as error:
        if task['stage'] != 'blocked':
            task['resume_stage'] = task['stage']
        task.update(stage='blocked', note=str(error))
    with store.db:
        store.db.execute('UPDATE runs SET data=? WHERE id=?', (json.dumps(run), run['id']))
        store.db.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(task), task['id']))


def request_fix(task, feedback):
    if task['fix_round'] >= 3:
        raise Blocked('three fix rounds exhausted: ' + feedback)
    task.update(fix_round=task['fix_round'] + 1, stage='fixing', review_sha=None,
                validated_sha=None, feedback=feedback)


def tick(store):
    SUPERVISORS[:] = [child for child in SUPERVISORS if child.poll() is None]
    cfg = config(store.home)
    runs = {run['id']: run for run in store.runs()}
    for task in store.tasks():
        if task['run_id']:
            try:
                consume(store, task, runs[task['run_id']])
            except (Blocked, ValueError, OSError, KeyError) as error:
                block(store, task, str(error))
    # Count assignments, including ambiguous/lost runs: uncertainty never frees a slot.
    active = [task for task in store.tasks() if task['run_id']]
    developers = sum(runs[task['run_id']]['role'] != 'review' for task in active)
    reviewers = sum(runs[task['run_id']]['role'] == 'review' for task in active)
    for task in store.tasks():
        if task['run_id'] or task['paused'] or task['stage'] in (TERMINAL - {'mergeable'}) | {'blocked'}:
            continue
        if store.meta('paused', False):
            continue
        try:
            repo = repo_config(cfg, task['repo'])
            if any(store.task(dep)['stage'] == 'cancelled' for dep in task['dependencies']):
                raise Blocked('dependency was cancelled; human replanning is required')
            if any(store.task(dep)['stage'] != 'merged' for dep in task['dependencies']):
                task['note'] = 'Waiting for dependencies to merge (mergeable alone is insufficient).'
                store.save(task)
                continue
            stage = task['stage']
            if stage in {'queued', 'opening'}:
                if developers >= 2:
                    continue
                providers.gate(task['writer'], cfg, store.meta('provider_pauses', {}))
                github.open_task(task, repo, store.save)
                stage = task['stage']
            if stage in {'coding', 'fixing', 'validation', 'review'}:
                role = 'review' if stage == 'review' else 'validation' if stage == 'validation' else 'writer'
                if (role == 'review' and reviewers >= 1) or (role != 'review' and developers >= 2):
                    continue
                start_run(store, cfg, task, role)
                if role == 'review':
                    reviewers += 1
                else:
                    developers += 1
            elif stage == 'ready':
                if github.reconciled_head(task) != task.get('review_sha'):
                    task.update(stage='validation', review_sha=None)
                else:
                    github.ready(task)
                    task['stage'] = 'ci'
                store.save(task)
            elif stage in {'ci', 'merging', 'mergeable'}:
                advance_ci(store, task, repo)
        except (Blocked, ValueError, OSError, KeyError) as error:
            block(store, task, str(error))


def advance_ci(store, task, repo):
    facts = github.observe(task['repo'], task['pr_number'])
    task.update(last_observation=facts, ci_sha=facts['ci_sha'])
    if facts['state'] == 'MERGED':
        if task['stage'] not in {'merging', 'mergeable'} or facts['headRefOid'] != task.get('review_sha'):
            raise Blocked('externally merged PR requires manual reconciliation')
        task.update(stage='merged', finished_at=now(), note='Merge recovered; remote branch may need cleanup.')
    elif github.reconciled_head(task) != task.get('review_sha'):
        github.gh(task['repo'], 'pr', 'ready', str(task['pr_number']), '--undo')
        task.update(stage='validation', review_sha=None)
    elif facts['ci_conclusion'] == 'failure' or facts['mergeable'] == 'CONFLICTING':
        github.gh(task['repo'], 'pr', 'ready', str(task['pr_number']), '--undo')
        request_fix(task, 'CI failure or merge conflict:\n' + json.dumps(facts))
    elif github.can_merge(task, facts):
        if repo.get('auto_merge') and task['repo'] == 'skyhilam/dkdm-monorepo':
            with lock(store.home / 'merge.lock', blocking=False):
                task['stage'] = 'merging'
                store.save(task)
                note = github.merge(task)
                task.update(stage='merged', finished_at=now(), note=note)
        else:
            task.update(stage='mergeable', finished_at=now(), note='Ready for manual merge.')
    store.save(task)


def stop_task(store, task, cancel=False):
    task['paused'] = not cancel
    if cancel:
        task['cancel_requested'] = True
    if task['run_id']:
        run = next(run for run in store.runs(task['id']) if run['id'] == task['run_id'])
        atomic_json(Path(run['directory']) / 'stop.json', {'requested_at': now(), 'cancel': cancel})
        task['note'] = 'Stop requested; waiting for the owning supervisor and completion receipt.'
    elif cancel:
        task.update(stage='cancelled', finished_at=now())
    store.save(task)


def resume_task(store, cfg, task, handoff=None):
    if task['run_id']:
        raise Blocked('run is not reconciled; cannot resume or hand off a possibly live writer')
    if task['stage'] in TERMINAL:
        raise Blocked('terminal task cannot be resumed')
    if task['fix_round'] >= 3 and task['stage'] == 'blocked':
        raise Blocked('fix cap reached; create a separately scoped task after human review')
    if 'pr_number' in task:
        github.reconciled_head(task)
    if handoff:
        if task.get('resume_stage', task['stage']) not in {'coding', 'fixing'}:
            raise Blocked('handoff is only allowed at a stopped writer checkpoint')
        providers.gate(handoff, cfg, store.meta('provider_pauses', {}))
        task['writer'] = handoff
        task['review_sha'] = None
        task['feedback'] = task.get('feedback', '') + '\nHandoff checkpoint: ' + task.get('note', '')
    task.update(paused=False, note='')
    if task['stage'] == 'blocked':
        task['stage'] = task.get('resume_stage', 'coding')
    store.save(task)
