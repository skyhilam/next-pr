---
name: next-pr
description: >
  Open one draft pull request per code, copy, or UI change in the current
  git repository, hand coding to a background subagent, and return so the
  user can send the next prompt. Use before editing on every such request.
  The user prompt is the scope. Do not stage unrelated dirty files. Skip
  for questions, reviews, acceptance, and explanations. Continue the
  current pull request only when the user says to. /next-pr
metadata:
  short-description: "Draft PR, code in background"
user-invocable: true
---

# Next PR

One user prompt that changes code, copy, or UI in the **current** git repository becomes one draft pull request. Coding runs in a background subagent so the parent can return and take the next prompt.

This skill is **repo-agnostic**: it uses `origin` from the session directory (and optional `NEXT_PR_REPO` / `NEXT_PR_BASE`). It is not limited to any one monorepo.

## When to use / skip

**Use** when the prompt will change behavior, copy, or UI. The prompt text is the whole scope.

**Skip** (do not open a PR) for questions, reviews, acceptance checks, and explanations.

**Stay on an existing PR** only when the user says to continue it or names its number.

Resolve the shared checkout from git: `git remote get-url origin` must exist; first `worktree` path in `git worktree list --porcelain` is the shared checkout. Do not hardcode a machine path.

## Roles

| Role | Does | Why |
|------|------|-----|
| Parent | Opens draft, writes work-log facts, dispatches workers, Observes then Acts | Keeps the chat free for the next prompt; never edits the shared checkout |
| Owner (coder/fixer) | Implements, tests, commits, pushes on that worktree | One owner per PR so CI/review feedback has a single place to land |
| Reviewer | Reads `references/code-review.md`, reports outcome + head SHA, does not edit | Separates judgment from the hands that change code |

Do not run `/implement` inside a subagent (subagents cannot spawn subagents). Never force-delete a dirty worktree.

## Layout

```
next-pr/
├── SKILL.md                 # agent instructions (English)
├── SKILL.zh-Hant.md         # human-readable Traditional Chinese
├── references/code-review.md
└── scripts/
    ├── open-pr.sh
    └── check-pr.sh
```

Casual reading in Chinese: [`SKILL.zh-Hant.md`](./SKILL.zh-Hant.md). Agents still follow this English file.

Prefer the scripts for repetitive git/gh steps so each run behaves the same way.

## Work-log facts

Keep one durable log per open PR (session notes or a file beside the worktree). Store **facts** (ids, SHAs, CI conclusions bound to a SHA), not stale display labels like “passed”. Derive the next action from those facts on every wake.

Minimum fields: task, **repo** (`owner/name`), PR number/URL, branch, worktree, **owner agent id**, current agent/role, stage (`opened`|`coding`|`review`|`fixing`|`ready`|`ci`|`merged`|`stopped`), last reviewed head SHA, last observed head SHA, last CI conclusions for that SHA, last review outcome (`no_blockers`|`blockers` + text), fix round (0–3), blocked-on-decision note.

Read the log before any dispatch. If that stage’s agent is already running, do not spawn a duplicate.

## Flow

### 1. Open

1. Write the PR body from the prompt (what the user will see, how to test, what is out of scope).
2. Leave prior PRs alone (the user may still be testing them).
3. From the session directory, run:

   `scripts/open-pr.sh --title "<short title>" --body-file <path>`

   Use the JSON it prints (`repo`, branch, worktree, pr_number, pr_url, head_sha, base) to seed the work log (stage `coding`, fix round `0`).
4. Spawn a background coder with `cwd` = that worktree. Prompt = user task + branch + limits: only files this task needs; commit/push to this branch; run scoped tests; do not open/ready/merge a PR; do not spawn subagents. Record that agent as **owner**.
5. Reply with PR link + worktree + “coding in background”. Stop. Do not wait.

A later prompt starts at Open again (new worktree / new owner), even if an earlier coder is still running.

### 2. Background completion

Continue only on a **native** agent-finished wake that names the agent. That is the only automatic handoff signal; without it, leave a recoverable draft and tell the user the next manual step. Do not pretend a poll loop is a completion wake. A deliberate Observe via `check-pr.sh` is fine; it is not a completion event.

If a newer user prompt arrived first, finish that Open, then resume the finished PR from its log.

### 3. After coder finishes

Set stage `review`. Spawn a background reviewer on the same worktree. It reads `references/code-review.md` and reviews the branch against `origin/<base>` (base from the work log / open-pr JSON) without editing. It must report the **head SHA** it reviewed. If the reference file is missing, stage `stopped` and say so.

### 4. After review finishes

Record review outcome + SHA. If worktree/PR head ≠ that SHA, the outcome is void — review again on the new tip.

- **`no_blockers`**: Observe with `scripts/check-pr.sh <pr>`, write facts, then Act: `gh pr ready <n>`. Stage `ready` → `ci`. Re-observe after ready; when facts show CI success for that head SHA and mergeability is clean, `gh pr merge --squash` (no `--delete-branch`). One merge at a time.
- **`blockers`**: if fix round is already 3, keep draft, stage `stopped`, report leftovers. Else increment fix round, spawn fixer on the **same** worktree with blockers quoted; update **owner** to that fixer. After fixer finishes, clear old reviewed SHA, review again.

### 5. Observe then act

Before fixer / ready / merge, run `scripts/check-pr.sh` and write its JSON into the work log. CI failure or merge conflict on the current head goes to the **owner** (fixer-round rules apply). Do not open an unrelated coding agent for the same PR. If `blocked-on-decision` is set, do not inject more fix prompts — tell the user what is missing.

## Stop conditions

- Prompt is a question / review / acceptance / explanation → no PR.
- Native completion unavailable → recoverable draft + clear next step; no fake auto-handoff.
- Review reference unreadable → draft stays open, stage `stopped`.
- Fix round reaches 3 with remaining blockers → draft stays open, report blockers.
- CI fail / conflict after Observe → route to owner or stop if over fix cap; do not merge.
- Missing user decision → `blocked-on-decision`, stage `stopped`.
