"""Canonical git/GitHub observation and guarded mutations; no model verdicts here."""
import json
from pathlib import Path
import re

from .common import Blocked, command

SHA = re.compile(r'^[0-9a-f]{40}$')
FIELDS = 'number,url,state,isDraft,mergeable,mergeStateStatus,headRefName,headRefOid,baseRefName,reviewDecision'


def git(path, *args):
    return command(['git', '-C', str(path), *args])


def gh(repo, *args):
    return command(['gh', *args, '--repo', repo])


def view(repo, target):
    value = json.loads(gh(repo, 'pr', 'view', str(target), '--json', FIELDS))
    if not SHA.fullmatch(value.get('headRefOid', '')):
        raise Blocked('GitHub returned invalid head SHA')
    return value


def check_facts(runs, statuses, sha, require_summary):
    """Unknown/empty/malformed results never pass. Inputs are fetched by exact SHA."""
    checks, failed, pending = [], [], []
    latest = {}
    for item in runs:
        if (not isinstance(item, dict) or not isinstance(item.get('app'), dict)
                or not item.get('name') or not isinstance(item.get('id'), int)):
            raise Blocked('invalid GitHub check-run record')
        key = (item['app'].get('id'), item['name'])
        if key not in latest or item.get('id', 0) > latest[key].get('id', 0):
            latest[key] = item
    for item in latest.values():
        name = item.get('name') or '<unnamed>'
        valid = item.get('head_sha') == sha and item.get('status') == 'completed'
        conclusion = item.get('conclusion')
        checks.append({'name': name, 'status': item.get('status'), 'conclusion': conclusion})
        if not valid or conclusion not in {'success', 'neutral', 'skipped'}:
            (failed if conclusion in {'failure', 'cancelled', 'timed_out', 'action_required',
                                      'startup_failure', 'stale'} else pending).append(name)
    contexts = {}
    for item in statuses:
        if (not isinstance(item, dict) or not item.get('context')
                or not isinstance(item.get('id'), int)):
            raise Blocked('invalid GitHub commit-status record')
        name = item['context']
        if name not in contexts or item.get('id', 0) > contexts[name].get('id', 0):
            contexts[name] = item
    for name, item in contexts.items():
        state = item.get('state')
        checks.append({'name': name, 'status': state, 'conclusion': state})
        if state != 'success':
            (failed if state in {'failure', 'error'} else pending).append(name)
    summary = any(item.get('name') == 'CI summary' and item.get('head_sha') == sha
                  and item.get('app', {}).get('slug') == 'github-actions'
                  and item.get('status') == 'completed' and item.get('conclusion') == 'success'
                  for item in latest.values())
    conclusion = ('failure' if failed else 'pending' if pending else 'unknown'
                  if not checks or (require_summary and not summary) else 'success')
    return dict(ci_sha=sha, ci_conclusion=conclusion, checks=checks,
                failed_checks=failed, pending_checks=pending, ci_summary_success=summary)


def observe(repo, target):
    pr = view(repo, target)
    sha = pr['headRefOid']
    run_pages = json.loads(command(['gh', 'api', '--paginate', '--slurp',
                                   f'repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest']))
    status_pages = json.loads(command(['gh', 'api', '--paginate', '--slurp',
                                      f'repos/{repo}/commits/{sha}/statuses?per_page=100']))
    runs = [item for page in run_pages for item in page['check_runs']]
    statuses = [item for page in status_pages for item in page]
    facts = check_facts(runs, statuses, sha, repo == 'skyhilam/dkdm-monorepo')
    fresh = view(repo, target)
    if fresh['headRefOid'] != sha:
        raise Blocked('PR head changed during CI observation')
    return {**fresh, **facts}


def local_head(task, clean=True):
    path = task['worktree']
    if git(path, 'symbolic-ref', '--short', 'HEAD') != task['branch']:
        raise Blocked('worktree is on an unexpected branch')
    if clean and git(path, 'status', '--porcelain'):
        raise Blocked('worktree has uncommitted changes; checkpoint requires human inspection')
    return git(path, 'rev-parse', 'HEAD')


def reconciled_head(task, clean=True):
    head = local_head(task, clean)
    pr = view(task['repo'], task['pr_number'])
    if pr['headRefName'] != task['branch'] or pr['baseRefName'] != task['base']:
        raise Blocked('PR branch/base differs from task journal')
    if pr['state'] != 'OPEN':
        raise Blocked(f'PR is {pr["state"]}; manual reconciliation required')
    if pr['headRefOid'] != head:
        raise Blocked('local/PR head differs; reconcile push before resuming')
    return head


def open_task(task, repo, save):
    """Persist names first. Existing partial creation is reconciled, never blindly rerun."""
    shared = Path(repo['path']).resolve()
    if 'branch' not in task:
        task.update(branch=f'next-pr/task-{task["id"]}', base=repo['base'],
                    worktree=str(shared.parent / 'next-pr-worktrees' / f'task-{task["id"]}'),
                    stage='opening')
        save(task)
    worktree = Path(task['worktree'])
    git(shared, 'fetch', 'origin', task['base'])
    if not worktree.exists():
        # A branch without its recorded worktree is ambiguous, not a reason to delete it.
        branches = git(shared, 'branch', '--list', task['branch'])
        if branches:
            raise Blocked('branch exists without worktree; inspect partial creation')
        git(shared, 'worktree', 'add', '-b', task['branch'], str(worktree), f'origin/{task["base"]}')
    local_head(task)
    if git(worktree, 'rev-list', '--count', f'origin/{task["base"]}..HEAD') == '0':
        git(worktree, 'commit', '--allow-empty', '-m', f'draft: {task["title"]}')
    git(worktree, 'push', '-u', 'origin', task['branch'])
    existing = json.loads(gh(task['repo'], 'pr', 'list', '--head', task['branch'],
                             '--state', 'all', '--json', 'number,url,state'))
    if len(existing) > 1 or (existing and existing[0]['state'] != 'OPEN'):
        raise Blocked('ambiguous or closed PR for this branch')
    if not existing:
        body = worktree.parent / f'task-{task["id"]}-body.md'
        body.write_text('## User request\n\n' + task['prompt'] + '\n\nHardware acceptance remains manual.\n')
        gh(task['repo'], 'pr', 'create', '--draft', '--base', task['base'],
           '--head', task['branch'], '--title', task['title'], '--body-file', str(body))
        existing = json.loads(gh(task['repo'], 'pr', 'list', '--head', task['branch'],
                                 '--state', 'open', '--json', 'number,url'))
    if len(existing) != 1:
        raise Blocked('PR creation could not be reconciled')
    task.update(pr_number=existing[0]['number'], pr_url=existing[0]['url'], stage='coding')
    save(task)


def ready(task):
    if reconciled_head(task) != task.get('review_sha'):
        raise Blocked('review is stale')
    gh(task['repo'], 'pr', 'ready', str(task['pr_number']))


def can_merge(task, facts):
    return all((facts.get('headRefOid') == task.get('review_sha') == facts.get('ci_sha'),
                facts.get('ci_conclusion') == 'success', facts.get('state') == 'OPEN',
                facts.get('isDraft') is False, facts.get('mergeable') == 'MERGEABLE',
                facts.get('mergeStateStatus') == 'CLEAN',
                facts.get('reviewDecision') != 'CHANGES_REQUESTED'))


def merge(task):
    facts = observe(task['repo'], task['pr_number'])
    if not can_merge(task, facts) or reconciled_head(task) != task['review_sha']:
        raise Blocked('merge preconditions changed')
    gh(task['repo'], 'pr', 'merge', str(task['pr_number']), '--squash',
       '--match-head-commit', task['review_sha'])
    if view(task['repo'], task['pr_number'])['state'] != 'MERGED':
        raise Blocked('merge command returned without confirmed merged state')
    try:
        delete_remote_branch(task)
        return ''
    except Blocked:
        return 'Merged; remote branch retained because safe deletion failed. Inspect branch before deleting.'


def delete_remote_branch(task):
    # Git lease prevents deletion if someone pushed after the reviewed commit.
    git(task['worktree'], 'push', 'origin',
        f'--force-with-lease=refs/heads/{task["branch"]}:{task["review_sha"]}',
        f':refs/heads/{task["branch"]}')
