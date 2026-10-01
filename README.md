# next-pr

Grok skill: one draft PR per code/copy/UI change in the **current** git repo, coding in a background subagent.

Repo-agnostic — uses `origin` (optional `NEXT_PR_REPO` / `NEXT_PR_BASE`). Not tied to a specific monorepo.

## 中文閒讀

- [SKILL.zh-Hant.md](./SKILL.zh-Hant.md) — 繁中說明（畀人睇）
- [SKILL.md](./SKILL.md) — 英文（畀 agent 跟）

## Install

```bash
git clone https://github.com/skyhilam/next-pr.git ~/.grok/skills/next-pr
```

## Layout

```
next-pr/
├── SKILL.md
├── SKILL.zh-Hant.md
├── references/code-review.md
└── scripts/
    ├── open-pr.sh
    └── check-pr.sh
```
