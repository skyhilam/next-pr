# next-pr

Grok skill: one draft PR per code/copy/UI change in `skyhilam/dkdm-monorepo`, coding in a background subagent so you can keep prompting.

## Install

Copy or symlink this folder to `~/.grok/skills/next-pr`.

```bash
git clone https://github.com/skyhilam/next-pr.git ~/.grok/skills/next-pr
```

Or keep a clone elsewhere and:

```bash
ln -s /path/to/next-pr ~/.grok/skills/next-pr
```

## Layout

- `SKILL.md` — workflow (open draft → background coder → review → ready → squash-merge)
- `references/code-review.md` — reviewer approval bar

## Scope

Only when `git remote get-url origin` is `skyhilam/dkdm-monorepo`. Ignore in other repos.
