# next-pr 本機持久協調器

> 主 Grok Bot 桌面協調請使用獨立的 [Pane dispatcher](docs/pane-dispatch.zh-Hant.md)：先建議、等使用者選 CLI、Pane worktree、草稿 PR、使用者 merge。此流程不呼叫下方舊協調器或 next-pr skill。

每個需求有獨立 worktree、分支、草稿 PR，同一時間只有一位寫入者。
Python 3.11+ / SQLite 記錄任務及 CLI 完成收據；關閉聊天或重啟管理程序
不會令系統盲目重開第二個寫入者。原有 [SKILL](SKILL.md) 仍可獨立使用，
同一個需求不要同時用兩套流程。

在固定保留的 next-pr checkout 執行：

```sh
python3 -m unittest discover -s tests -v
python3 -m next_pr init --repo-path /Users/hotinlam/Sites/dkdm.game-853.com/v2
```

資料放在 `~/Library/Application Support/next-pr/`，不可放在 Git checkout。
先在供應商帳戶確認訂閱、停用 API／超額付費，並檢查 CLI 及專案設定、
API key helper、自訂 provider、hooks、MCP。登入成功不代表已確認訂閱計費。
只有實際檢查後才執行：

```sh
python3 -m next_pr confirm-provider codex --subscription-only --config-audited \
  --note '已確認 ChatGPT 訂閱及 CLI／專案設定，沒有 API 付費 fallback'
# Claude 必須先完成訂閱登入，再用相同命令確認 claude。
```

編輯外部 `config.json`，把 dkdm 的 `validation` 設為已核准測試腳本的 argv
陣列，例如 `["/absolute/path/to/project-validation-script"]`。空值會停在驗證階段。
確認完成後安裝並啟動：

```sh
python3 -m next_pr install-launchagent --load
python3 -m next_pr submit --title '修正取貨畫面' --prompt-file /tmp/request.txt \
  --request-key pickup-20261003 --scope apps/retail-floor-sunmi
python3 -m next_pr status
python3 -m next_pr metrics
```

初始只啟用尚未確認計費的 Codex；可只確認 Codex 後啟動，但需要 Claude
寫入／獨立審查的任務會明確阻塞。主要寫入者按提交順序交替 Codex／Claude；
最多兩個開發工作、一個審查，重型驗證逐一執行。重疊 scope 必須指定
`--depends-on 任務ID`，待前一個 PR 合併。沒有 scope 等同整個 repository。
Grok／Cursor 預設停用，需額外可驗證訂閱的非模型 probe；OpenCode 停用。
配額不明就顯示 unknown，不估算，不偷偷改用 API。

```sh
python3 -m next_pr pause 任務ID
python3 -m next_pr resume 任務ID
python3 -m next_pr cancel 任務ID
python3 -m next_pr resume --provider codex
python3 -m next_pr resume 任務ID --handoff claude
```

交接前必須收到舊程序停止收據，且 worktree 乾淨、本機／遠端 HEAD 一致。
權限不足、計費未確認、未知 CLI 輸出、缺少收據會阻塞，不加 bypass 旗標。
若 supervisor 意外終止，先人工確認所有相關程序已停止並保存程式修改，才可用
`recover 任務ID --confirm-no-processes`，再 resume。仍存在的鎖／PID 不會被強殺。

獨立審查綁定 HEAD；改動後重新驗證／審查。最多三輪修正。
dkdm 只有同一 SHA 的 CI summary 成功、所有檢查完成且可接受、PR 非草稿、
OPEN／CLEAN／MERGEABLE 才逐一 squash merge；其他 repository 停在 mergeable。
Sunmi 真機驗收仍由人手完成。解除安裝前先 pause、確認程序已停止，再執行
`python3 -m next_pr uninstall-launchagent`。

完整權限、計費、復原限制及精確設定見 [English README](README.md)。
