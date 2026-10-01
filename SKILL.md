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

Parent opens the draft and dispatches workers. Parent does not edit the shared checkout. Workers own implementation, tests, commits, and pushes on that PR's worktree.

## Work log (facts, not display labels)

Keep one durable log entry per open pull request (session notes or a small file beside the worktree is fine). Store **facts** you observed or scheduled. Do not store stale verdict labels such as `passed` or `ready to merge` as if they were still true; derive the next action from the facts below each time you wake.

Record at least:

- task text (quoted)
- PR number and URL
- branch name
- worktree absolute path
- **owner agent id**: the one coding/fixing conversation that owns this PR's implementation (set on first coder; reuse for every fixer)
- current agent id / role (`coder` | `reviewer` | `fixer` | `observe` | `ci-wait`)
- stage fact: `opened` | `coding` | `review` | `fixing` | `ready` | `ci` | `merged` | `stopped`
- last reviewed head SHA (empty until a review finishes; void when tip moves)
- last observed head SHA (from the most recent Observe step)
- last CI conclusion + check-run names/conclusions you read (with the head SHA they applied to)
- last review outcome on that SHA: `no_blockers` | `blockers` plus the blocker text
- fix round count (starts at 0)
- blocked-on-decision note (empty unless waiting on the user)

On every new turn or background wake that might touch this PR: read the log first. If an agent for that stage is already running or the facts already advanced, do not spawn a duplicate. Update the log whenever any fact above changes.

## Same owner for feedback

CI failures, review blockers, merge conflicts, and follow-up fix prompts always go to the **owner agent** recorded in the work log (or a new fixer spawned on the **same worktree / branch / PR**, with the owner id updated to that fixer). Never open a second unrelated coding agent for the same PR. Reviewers observe only; they do not become the owner.

If the owner is blocked on a permission or user decision (`blocked-on-decision` is set), do not inject more fix prompts. Leave stage `stopped` (or keep the note) and tell the user what is missing.

## Observe then act

Separate **Observe** from **Act**. Do not spawn a fixer, mark ready, or merge in the same breath as reading GitHub unless you just finished a dedicated Observe pass whose facts are written to the log.

Observe (role `observe` or the parent on a wake) must, for this PR:

1. Read PR state, draft flag, mergeability, and head SHA (`gh pr view` / checks).
2. Read latest check runs for that head SHA.
3. Read review comments / requested changes if any.
4. Write `last observed head SHA`, CI conclusions bound to that SHA, and any conflict/draft facts into the work log.

Only after those facts are recorded may you Act: spawn fixer, `gh pr ready`, start CI wait, or squash-merge. Re-observe after every push that moves HEAD.

## Background completion

Continue a finished background agent only when this host delivers a native completion wake for that agent (for example a subagent-finished event that names the agent id). Treat that wake as the only automatic handoff signal.

If this host has no native completion event for background agents, do not claim automatic follow-through. Leave the PR in a recoverable state: draft stays open, work log facts are accurate, and tell the user what finished and what the next manual step is. Never invent a poll loop that pretends to be completion-driven handoff. A deliberate Observe step that reads GitHub is allowed; pretending Observe is a completion wake is not.

When a completion wake arrives and the user has already sent another prompt in this session, finish opening that newer draft first (Open steps), then continue the finished PR from its work log.

## Open

1. Write the pull request body from the prompt: what the user will see, how to test it, and what is not included.
2. Leave every previous pull request alone. Do not add commits to one the user is testing.
3. `git fetch origin main`. Create a worktree and branch from `origin/main`. Do not use the dirty workspace. Never delete a dirty worktree to make room.
4. In that worktree only, `git commit --allow-empty` with message `draft: <short title>`. Push the branch. `gh pr create --draft` with the body from step 1.
5. Write the work log for this PR (stage `coding`, fix round `0`, SHAs empty). Spawn `spawn_subagent` with `background: true` and `cwd` set to that worktree. The prompt is the user task, the branch, and these limits: change only files this task needs; commit and push to this branch; run the tests for that scope; do not open, ready, or merge a pull request; do not spawn subagents. Record that agent id as both current agent and **owner agent**.
6. Reply with the pull request link, the worktree path, and that coding is in the background. Stop. Do not wait for the subagent.

A later prompt in this session starts at step 1 again, even if an earlier coder is still running. One worktree and one owner coder per prompt.

## When a background agent finishes

Use only a native completion wake (see Background completion). Then continue that pull request from its work log without waiting for a new user prompt, unless a newer Open is already in flight.

- Coder finished: set stage `review`. Spawn a background reviewer on the same worktree. It reads `references/code-review.md` beside this skill and follows that approval bar. It reviews that branch against `origin/main` and does not edit. It must report the head SHA it reviewed. If that file cannot be read, leave the draft open, set stage `stopped`, and say so.
- Review finished: record `last reviewed head SHA` and `last review outcome` from the report. Before treating the review as current, confirm worktree / PR head still equals that SHA. If the tip moved, prior outcome is void: set stage `review` and spawn a new reviewer on the new tip. Do not reuse an older pass on a newer commit.
- Review outcome `no_blockers` on the current head SHA: run **Observe**, then Act with `gh pr ready <number>`. Set stage `ready` then `ci`. CI does not run while the pull request is draft. After ready, Observe again on a schedule or wake; when facts show CI success for that head SHA and mergeability is clean, squash-merge and keep the head branch. Command is `gh pr merge --squash` with no `--delete-branch`. One merge at a time. Leave conflicting or failing pull requests unmerged (stage `stopped`) after Observe records those facts.
- Review outcome `blockers` on the current head SHA: if fix round is already 3, do not spawn another fixer. Keep the draft open, set stage `stopped`, and report the remaining blockers. Otherwise increment fix round, set stage `fixing`, and spawn a background **fixer on the same worktree**, quoting blockers in full; set **owner agent** to that fixer. It commits and pushes to the same branch and does not spawn subagents. When it finishes, clear last reviewed SHA applicability, set stage `review`, and review again (new SHA required).
- Observe facts show CI failure or merge conflict on the current head: route feedback to the **owner agent** (fixer round rules apply). Do not start a new unrelated agent.
- Review, fixer, or Observe stops for a missing decision: leave the draft open, set `blocked-on-decision`, stage `stopped`, and say what is missing.

Hard cap: at most three fixer rounds per PR (`fix round` 1..3). No unbounded review↔fix loops.

## Stay on the current pull request

Only when the user says to continue that pull request, or names its number. Still do not stage unrelated dirty files. Read the work log first; **Observe** if GitHub facts may be stale; resume from recorded facts, same-owner, and SHA rules above.

## Do not open a pull request

Questions, reviews, acceptance checks, and explanations.
