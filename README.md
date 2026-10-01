# next-pr

Grok skill for `skyhilam/dkdm-monorepo`: one draft PR per change, coding in a background subagent.

Organized like [anthropics/skills skill-creator](https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md): lean `SKILL.md`, `references/` loaded on demand, `scripts/` for deterministic steps.

## Install

```bash
git clone https://github.com/skyhilam/next-pr.git ~/.grok/skills/next-pr
```

## Layout

```
next-pr/
├── SKILL.md                 # when to trigger, roles, flow, stop conditions
├── references/
│   └── code-review.md       # reviewer bar + report format
└── scripts/
    ├── open-pr.sh           # worktree, branch, draft PR
    └── check-pr.sh          # Observe: PR / commit / CI JSON
```

## Scope

Only when `git remote get-url origin` is `skyhilam/dkdm-monorepo`.
