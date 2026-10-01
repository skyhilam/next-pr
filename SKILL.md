---
name: next-pr
description: >
  Open a dkdm draft pull request, hand coding to a background subagent,
  and return so the user can send the next prompt. Use before editing
  on every code change, fix, feature, copy, or UI request in this repo,
  including Sunmi desk work. The user prompt is the scope. Do not stage
  unrelated dirty files. Skip for questions, reviews, acceptance, and
  explanations. Continue the current pull request only when the user
  says to. /next-pr
metadata:
  short-description: "Draft PR, code in background"
user-invocable: true
---

# Next PR

A prompt that changes code, copy, or behavior becomes one draft pull request. This session only opens the draft, then returns. Coding, review, and fixes run in background subagents so the user can send the next prompt.

Do not run `/implement` inside a subagent. A subagent cannot spawn subagents.

This skill applies only when `git remote get-url origin` from the session directory is `skyhilam/dkdm-monorepo`. Resolve the shared checkout as the first `worktree` path in `git worktree list --porcelain`. Do not hardcode a machine path. Create the new worktree outside that directory, and tell the coder not to edit it. In every other repository, ignore this skill.

## Open

1. Write the pull request body from the prompt: what the user will see, how to test it, and what is not included.
2. Leave every previous pull request alone. Do not add commits to one the user is testing.
3. `git fetch origin main`. Create a worktree and branch from `origin/main`. Do not use the dirty workspace.
4. In that worktree only, `git commit --allow-empty` with message `draft: <short title>`. Push the branch. `gh pr create --draft` with the body from step 1.
5. `spawn_subagent` with `background: true` and `cwd` set to that worktree. The prompt is the user task, the branch, and these limits: change only files this task needs; commit and push to this branch; run the tests for that scope; do not open, ready, or merge a pull request; do not spawn subagents.
6. Reply with the pull request link, the worktree path, and that coding is in the background. Stop. Do not wait for the subagent.

A later prompt in this session starts at step 1 again, even if an earlier coder is still running. One worktree and one coder per prompt.

## When a background agent finishes

Continue that pull request without waiting for a new prompt. If the user has already sent another prompt, open that draft first, then continue the finished one.

- Coder finished: spawn a background reviewer. It reads `references/code-review.md` beside this skill and follows that approval bar. It reviews that branch against `origin/main` and does not edit. If that file cannot be read, leave the draft open and say so.
- Review has no presumptive blockers: `gh pr ready <number>`. CI does not run while the pull request is draft. Poll the CI summary in the background. When it is success and the pull request merges cleanly, squash-merge and keep the head branch. Command is `gh pr merge --squash` with no `--delete-branch`. The user keeps merged branches for later investigation. One merge at a time. Leave conflicting or failing pull requests unmerged.
- Review has blockers: spawn a background fixer on the same worktree with those blockers quoted in full. It commits and pushes to the same branch and does not spawn subagents. When it finishes, review again.
- Review or fixer stops for a missing decision: leave the draft open and say what is missing.

## Stay on the current pull request

Only when the user says to continue that pull request, or names its number. Still do not stage unrelated dirty files.

## Do not open a pull request

Questions, reviews, acceptance checks, and explanations.
