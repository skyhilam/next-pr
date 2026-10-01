# next-pr

Grok skill: one draft PR per code/copy/UI change in `skyhilam/dkdm-monorepo`, coding in a background subagent so you can keep prompting.

## Install

```bash
git clone https://github.com/skyhilam/next-pr.git ~/.grok/skills/next-pr
```

Or keep a clone elsewhere and symlink into `~/.grok/skills/next-pr`.

## Layout

- `SKILL.md` — workflow (open draft → background coder → review → ready → squash-merge)
- `references/code-review.md` — reviewer approval bar

## Reliability rules (in SKILL.md)

- **Background completion**: continue only on a native agent-finished wake; otherwise stop in a recoverable draft state.
- **Work log**: task, PR, branch, worktree, agent, stage; check before re-dispatch.
- **Review scope**: bind pass/fail to the reviewed head SHA; new commits need a new review.
- **Fix cap**: at most three fixer rounds; then keep draft and report leftovers.

## Scope

Only when `git remote get-url origin` is `skyhilam/dkdm-monorepo`. Ignore in other repos.
