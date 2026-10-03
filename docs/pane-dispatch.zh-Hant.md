# Pane CLI dispatcher

主 Grok Bot **桌面對話**負責協調；`pane-dispatch` 只執行一次命令，不是排程器。
預設先建議 CLI，**等使用者選擇才開始**。使用者明確指定 CLI 即視為已選；
只有使用者明確允許自動選擇，才使用 `--auto`。
每個任務由 Pane 建立自己的 worktree。Worker 測試、commit、push、開草稿 PR；
review 通過可標記 ready，**永遠由使用者 merge**。

## 安裝與桌面啟動

需要 macOS/Linux、Python 3.11+、Pane/runpane、已登入的工作 CLI，以及 Git/gh。
零 Python 第三方依賴。CodexBar 與本機設定由上層安裝；此程式不修改全域 skills、
LaunchAgents，也不載入 next-pr skill 或舊 runner。

在會持續保留的 checkout 執行以下命令。連結指向版本控制內的 executable；
不要把 module 複製到另一份會過期的本機副本。`bin/pane-dispatch` 會解析連結找到同版 module。

```sh
python3 -m unittest discover -s tests -p 'test_pane_dispatch.py' -v
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/bin/pane-dispatch" "$HOME/.local/bin/pane-dispatch"
"$HOME/.local/bin/pane-dispatch" --help
```

若目標已存在，先檢查連結指向再由上層更新；上面的命令不會覆蓋它。
不要刪除仍被連結引用的 worktree。正式使用可讓上層將連結切到保留的 checkout。

在**主 Grok Bot 桌面對話**貼上以下 bootstrap（將路徑換成實際安裝位置）：

> 你是 coordinator。透過本機 shell 執行 `$HOME/.local/bin/pane-dispatch`。
> 不要假設桌面會讀取 `~/.grok` 的 CLI skills。不要呼叫 next-pr skill、舊持久 runner、
> 其他 coordinator 或巢狀 worker。先執行 inventory/recommend，說明可用 CLI、負載與 quota；
> 等使用者選擇。使用者已明確指定 CLI 就直接使用；明確授權 auto 才傳 `--auto`。
> 把原始任務原文寫入私有 prompt 檔，以穩定 task-id 執行 start。
> 使用回傳的 Pane/watch argv 監看，status 讀取現有報告；不輪詢 shell 畫面推測完成。
> 遇到 blocked、permission prompt、未知建立結果，呈現證據讓使用者決定，不自動重試或換 CLI。
> 成功候選必須有 worker report、測試、PR URL、精確 head SHA；自行核對 PR head/CI/review。
> 沒有證據就保持 unknown。review 可標記 ready，只有使用者能 merge，永不啟用 auto-merge。

## 使用

所有正常輸出是單一 JSON：`{ok, result}`；參數／程序錯誤為 `{ok:false,error}`、exit 2。
`--help` 是文字。`ok:true` 只表示查詢／提交請求已處理，**不代表任務完成**。

```sh
pane-dispatch inventory
pane-dispatch recommend
# 建議後等待使用者；請用 editor 或安全的檔案 API 儲存原文，不把任務插入 shell command。
umask 077
# 編輯 /tmp/task.txt，儲存使用者原文
pane-dispatch start --task-id issue-42 --repo 'Next PR' --cli codex --prompt-file /tmp/task.txt
# 僅在使用者明確允許自動選擇時：
pane-dispatch start --task-id issue-43 --repo 'Next PR' --auto --prompt-file /tmp/task.txt
pane-dispatch status --task-id issue-42
pane-dispatch usage --task-id issue-42
```

Repo 必須是 Pane 已儲存的 ID、完整路徑或唯一名稱；不接受會隨畫面改變的 `active`。
新任務先向 `origin` 查詢遠端目前的預設分支，fetch 當時的精確 commit，再以該 SHA
傳入 Pane `--base-branch`；不依賴可能過期的本機預設分支或 `origin/HEAD`。
`base` 記錄保留 remote、ref、SHA 與 fetch 時間。查詢／fetch 失敗或沒有可確認的遠端
預設分支，就在建立 worker 前停止；不偷偷改用舊 base。這不會 checkout、merge 或改動
本機未提交內容。重送既有 task-id 保留原 base，不重新 fetch 或重開任務。
程式以 `shutil.which` 及已知安裝目錄找到絕對 CLI 路徑，包括 `~/.local/bin/runpane`。
Codex/Claude/Cursor 使用 Pane built-in agent identity；由於 Pane 2.4.152 預設模板帶有
permission bypass flags，透過支援的 `--agent` + `--tool-command` 覆寫成安全的絕對 executable。
不改全域模板。agy/Grok/OpenCode 必須先通過本機 `--help` 檢查，才建立安全引用參數的 custom command。
沒有 bypass flags；CLI 原有 permission prompts 可能需要使用者處理。

## 結果、恢復與用量

狀態放在 `~/.local/state/pane-dispatch`（目錄 0700、檔案 0600），可用全域
`--state-dir PATH` 指向獨立測試目錄。原子 replace、fsync、flock 保護並行提交。
請保留狀態目錄：相同 task-id、repo 字串、選擇模式與任務原文重送會回傳原任務；
同 ID 改內容會拒絕。建立前先記錄 intent，使用 task-id hash 命名 Pane/branch。
逾時、程序錯誤或 crash 後只對照現有同名 Pane，**絕不重開第二個 worker**。
即使查無 Pane，也保留 `creation_unknown`，由人檢查；不要刪狀態或換 ID 盲目重送。

`status` 讀 Pane panel 的持久 report 與一次性的 watch journal，並提供 `watch_argv`。
長時間監看由桌面 shell 執行該 argv（`watch --follow --quiet --kinds ...agent.report... --json`）；
dispatcher 沒有背景程序。idle、ready UI、exit 0 都不是完成。
`reported_ready` 表示具完整證據的 **worker 自述**，`independently_verified:false`；
不是 CI 或 code review 保證。主 Bot 應檢查 GitHub 的 PR head 與測試結果，head 改變須重新 review。
`incomplete_report`／`needs_inspection`／`status_error` 不得呈現為成功。

Worker 的 `runpane report --summary-file` 應包含 JSON（prompt envelope 已要求）：

```json
{"task_id":"issue-42","cli_session_id":null,"head":"0123456789012345678901234567890123456789","pr_url":"https://github.com/owner/repo/pull/42","tests":[{"command":"python3 -m unittest discover -s tests","outcome":"passed"}],"summary":"修改與限制"}
```

配合 `runpane report --state ready --pr 42 --head EXACT_40_CHAR_SHA --summary-file RESULT.json --json`。
未知 CLI session ID 維持 null；不把 Pane ID 當成 CLI session ID。報告不能包含憑證。

Quota 由 [CodexBar CLI](https://raw.githubusercontent.com/steipete/CodexBar/main/docs/cli.md)
的 `usage --provider PROVIDER --format json` 取得；保留 source、snapshot time、reset、fetch time、
去識別 account key 與粗粒度 error。原始 stderr、帳號 email、credential/config 檔不輸出或讀取。
`grok` worker 是 xAI Grok CLI（`~/.grok/bin/grok`），使用 CodexBar `grok` provider，
其 xAI 額度與 Cursor 分開；不要把 Cursor 內的 Grok Bot 桌面協調器誤當成此 CLI。
詳見 [CodexBar Grok provider](https://raw.githubusercontent.com/steipete/CodexBar/main/docs/grok.md)。
每個 provider 只抓一次，同一 provider 的重複帳戶快照去重，不加總成額外容量。OpenCode quota
僅代表 CodexBar 的 opencode provider，不能證明 CLI 目前使用的 model/provider，因此 OpenCode 僅接受手動選擇。
多帳號無法確認當前 CLI 帳戶時，不自動選擇。Auto 需要 10 分鐘內的單帳戶有效 quota，
每個回傳 window 均未耗盡；quota 百分比只用作 eligibility，不跨 provider 當成同一預算排序。
可用 CLI 按 dispatcher 尚未結束的任務數排序，同分按名稱。負載只涵蓋此 dispatcher 記錄。
Quota 失敗／缺少 CodexBar 不阻止明確的手動選擇。

`usage` 的 per-task cost 來自 `runpane panes cost --pane ID`，是該 Pane 最近 30 日的
API 價格估算，不是帳單或剩餘 quota。Pane 未支援／沒有 attributed messages 時是 null，
不把空的零統計當成免費。已知部分值保留 incomplete；不以全域 CodexBar cost 猜單一任務成本。
