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
- **Work log (facts)**: task, PR, branch, worktree, owner agent, stage, SHAs, CI conclusions; check before re-dispatch. Do not trust stale “passed” labels.
- **Same owner**: CI / review / conflict feedback always returns to the owner agent on that worktree.
- **Observe then act**: read GitHub facts into the log before fixer / ready / merge.
- **Review scope**: bind outcomes to the reviewed head SHA; new commits need a new review.
- **Fix cap**: at most three fixer rounds; then keep draft and report leftovers.

Patterns inspired in part by [Untrivial-ai/agent-orchestrator](https://github.com/Untrivial-ai/agent-orchestrator) (durable facts, observe→act, same-owner feedback).

## Scope

Only when `git remote get-url origin` is `skyhilam/dkdm-monorepo`. Ignore in other repos.
