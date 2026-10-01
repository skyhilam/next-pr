# Next PR（繁中閒讀版）

> 給人看的說明。Agent 執行時仍以英文 [`SKILL.md`](./SKILL.md) 為準。

## 一句話

在 `skyhilam/dkdm-monorepo` 裡，你每一句會改 code／文案／UI 的指令，都會變成**一個 draft PR**；寫 code 丟給背景 agent，主對話可以立刻接下一個指令。

## 什麼時候用／唔用

| 情況 | 做法 |
|------|------|
| 會改行為、文案、UI（包括 Sunmi 櫃檯相關） | 用 next-pr；你的整段 prompt 就是範圍 |
| 只是提問、純 review、驗收、解釋 | **唔開** PR |
| 你講明「繼續呢個 PR」或報 PR 號碼 | 留在現有 PR／worktree |

只在 `git remote` 係 `skyhilam/dkdm-monorepo` 時生效；共用 checkout 用 git 查，唔好寫死本機路徑。其他 repo 忽略呢個 skill。

## 邊個做咩

| 角色 | 做咩 | 點解 |
|------|------|------|
| Parent（主對話） | 開 draft、記工作紀錄、派工、先 Observe 再 Act | 讓你繼續打下一句；唔改 shared checkout |
| Owner（coder／fixer） | 喺嗰個 worktree 改、測、commit、push | 同一個 PR 只有一個「負責人」，CI／review 意見有地方落地 |
| Reviewer | 讀 `references/code-review.md`，回報結果＋head SHA，唔改碼 | 判斷同動手分開，避免自己審自己改 |

背景 agent 入面唔好跑 `/implement`（再 spawn 唔到）。Dirty worktree 唔好強制刪。

## 目錄點擺

```
next-pr/
├── SKILL.md                 # Agent 用（英文）
├── SKILL.zh-Hant.md         # 你閒讀（呢份）
├── references/code-review.md
└── scripts/
    ├── open-pr.sh           # 開 worktree、branch、draft PR
    └── check-pr.sh          # 讀 PR／commit／CI（Observe）
```

重複嘅 git／gh 步驟用 script，每次行為先一致。

## 工作紀錄記咩（事實，唔係標籤）

每個進行中嘅 PR 留一份耐久紀錄（session 筆記或 worktree 旁邊檔都行）。

記**事實**：agent id、SHA、某次觀察到嘅 CI 結論……  
唔好當「已通過」「可以 merge」永遠成立——每次醒返要用最新事實再決定下一步。

最少要有：任務原文、PR 號／連結、branch、worktree、**owner agent id**、而家邊個 agent／角色、階段（`opened` → `coding` → `review` → `fixing` → `ready` → `ci` → `merged`／`stopped`）、上次 review 嘅 head SHA、上次 Observe 嘅 head SHA、該 SHA 嘅 CI 結論、review 結果（`no_blockers`／`blockers`＋說明）、修正輪數（0–3）、等你拍板嘅備註。

派工前先睇紀錄；嗰個階段已經有 agent 跑緊就唔好再開一個。

## 流程（白話）

### 1. 開單

1. 用你嘅 prompt 寫 PR 說明：用戶會見到咩、點測、邊啲唔做。
2. 唔好動舊 PR（可能你仲喺度試緊）。
3. 跑 `scripts/open-pr.sh --title "…" --body-file …`，用輸出嘅 JSON 寫入工作紀錄（階段 `coding`）。
4. 背景 coder：工作目錄＝嗰個 worktree；只改呢單需要嘅檔；commit／push；跑相關 test；唔 ready／唔 merge；唔再 spawn。呢個 agent 就係 **owner**。
5. 回你 PR 連結＋worktree，話 coding 喺背景，**唔等**。

下一句新指令＝再開一張新單（即使舊 coder 未完）。

### 2. 背景完成點樣接

只有宿主有**原生**「agent 做完」事件（帶 agent id）先自動接落去。  
冇呢類事件就唔好扮會自動跟：draft 留住、紀錄寫清楚、話你知下一步手動做咩。  
故意跑 `check-pr.sh` 睇狀態可以，但嗰唔係「完成事件」。

若果你已經打咗下一句，先開完新單，再返嚟跟舊單。

### 3. Coder 完 → Review

階段改 `review`，同一個 worktree 開 reviewer，對 `origin/main` 睇，唔改碼，一定要報**審過邊個 head SHA**。參考檔讀唔到就停（`stopped`）。

### 4. Review 完

記低結果＋SHA。若 tip 已經唔係嗰個 SHA，舊結論作廢，要再審。

- **冇 blocker**：先 `check-pr.sh` Observe，再 `gh pr ready`；之後再 Observe，CI 綠同可 merge 先 squash merge（保留 branch）。一次只 merge 一個。
- **有 blocker**：修正已滿 3 輪 → 留 draft，列出剩低問題。否則加一輪，同一 worktree 開 fixer（owner 轉佢），改完再 review。

### 5. 先睇再動手（Observe → Act）

開 fixer、ready、merge 之前，一定先跑 `check-pr.sh` 把 JSON 寫入紀錄。  
CI 掛、撞 conflict → 打返 **owner**（受 3 輪上限約束）。同一 PR 唔好再開一個無關嘅 coding agent。  
等你決定嘅時候唔好再塞 fix prompt，直接講缺咩。

## 幾時停

- 只係問／review／驗收／解釋 → 唔開 PR  
- 冇原生完成事件 → 可恢復嘅 draft＋講清下一步  
- review 參考讀唔到 → 停  
- 修正滿 3 輪仲有問題 → 停，列問題  
- Observe 見 CI 掛／conflict → 交 owner 或超上限就停；唔 merge  
- 缺你決定 → 記低，停  

## Script 速查

```bash
# 開 draft PR（會印 JSON）
scripts/open-pr.sh --title "短標題" --body-file /tmp/body.md

# 觀察 PR／CI（會印 JSON）
scripts/check-pr.sh 123
```
