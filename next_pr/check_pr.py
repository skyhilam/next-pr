"""Compatibility interface for the skill's read-only check-pr.sh."""
import argparse
import json
import os
from .common import command
from .github import observe


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('target')
    parser.add_argument('--repo', default=os.environ.get('NEXT_PR_REPO'))
    args = parser.parse_args()
    repo = args.repo or json.loads(command(['gh', 'repo', 'view', '--json', 'nameWithOwner']))['nameWithOwner']
    facts = observe(repo, args.target)
    aliases = dict(repo=repo, pr_number=facts['number'], pr_url=facts['url'],
                   head_sha=facts['headRefOid'], is_draft=facts['isDraft'],
                   mergeable=facts['mergeable'], merge_state_status=facts['mergeStateStatus'],
                   branch=facts['headRefName'], base=facts['baseRefName'], state=facts['state'])
    print(json.dumps({**facts, **aliases}, indent=2))


if __name__ == '__main__':
    main()
