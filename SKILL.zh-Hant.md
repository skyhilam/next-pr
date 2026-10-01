# Next PR（繁中閒讀版）

> 給人看的說明。Agent 執行時仍以英文 [`SKILL.md`](./SKILL.md) 為準。

## 一句話

喺**而家呢個 git repo**（跟 `origin`），你每一句會改 code／文案／UI 嘅指令，都會變成**一個 draft PR**；寫 code 丟畀背景 agent，主對話可以即刻接下一個指令。

呢個 skill **唔綁死**某個 monorepo；邊個 repo 開 session，就對邊個 `origin` 開工。

## 什麼時候用／唔用

| 情況 | 做法 |
|------|------|
| 會改行為、文案、UI | 用 next-pr；你嘅整段 prompt 就係範圍 |
| 只是提問、純 review、驗收、解釋 | **唔開** PR |
| 你講明「繼續呢個 PR」或報 PR 號碼 | 留喺現有 PR／worktree |

要有 `origin`；共用 checkout 用 `git worktree list` 第一條，唔好寫死本機路徑。

## 邊個做咩

| 角色 | 做咩 | 點解 |
|------|------|------|
| Parent（主對話） | 開 draft、記工作紀錄、派工、先 Observe 再 Act | 讓你繼續打下一句；唔改 shared checkout |
| Owner（coder／fixer） | 喺嗰個 worktree 改、測、commit、push | 同一個 PR 只有一個「負責人」 |
| Reviewer | 讀 `references/code-review.md`，回報結果＋head SHA，唔改碼 | 判斷同動手分開 |

背景 agent 入面唔好跑 `/implement`。Dirty worktree 唔好強制刪。

## 目錄點擺

```
next-pr/
├── SKILL.md
├── SKILL.zh-Hant.md
├── references/code-review.md
└── scripts/
    ├── open-pr.sh
    └── check-pr.sh
```

## 工作紀錄記咩

記**事實**（含 **repo** `owner/name`、SHA、CI 結論），唔好當「已通過」永遠成立。派工前先睇紀錄，避免重複派工。

## 流程（白話）

1. **開單**：寫 PR body → `scripts/open-pr.sh` → 背景 coder（記做 owner）→ 回 PR 連結，唔等。  
2. **完成接續**：只靠宿主原生 agent-finished；冇就留可恢復 draft。  
3. **Review**：對 `origin/<base>`；要報 head SHA。  
4. **之後**：冇 blocker → Observe → ready → CI 綠先 squash merge；有 blocker → 最多 3 輪 fixer，打返同一 owner。  
5. **Observe → Act**：fixer／ready／merge 前一定跑 `check-pr.sh`。

## Script 速查

```bash
scripts/open-pr.sh --title "短標題" --body-file /tmp/body.md
scripts/check-pr.sh 123
# 或
scripts/check-pr.sh 123 --repo owner/name
```

可選環境變數：`NEXT_PR_REPO`、`NEXT_PR_BASE`。
