# next-pr

> For main Grok Bot desktop coordination, use the independent [Pane dispatcher](docs/pane-dispatch.zh-Hant.md): recommend → user selects → Pane worktree → draft PR → user merges. This workflow does not invoke the skill or persistent coordinator below.

One request, one isolated worktree, one draft PR, one writer. The original
[agent skill](SKILL.md) remains available. The optional Python coordinator runs
local subscription CLIs independently of a chat session and records their actual
process completion. It does not rely on native Codex agent-finished events.

The coordinator is an explicit alternative to the skill's native subagent
workflow. Do not run both on the same request or ask a worker to invoke the skill.
It never installs or changes personal skills.

**Draft safety limitation:** unattended use is not yet safe for arbitrary CLI
descendants. A child can close inherited lock descriptors and leave its process
group with `setsid()`, escaping the current completion check. The coordinator
does not yet provide OS containment or exhaustive descendant accounting; normal
exit and an empty process group do not prove all work has stopped. Keep this
implementation in evaluation until that boundary is resolved. Offline fake-CLI
tests also do not establish real CLI permission compatibility.

## Install and verify billing

Requires macOS, Python **3.11+**, Git, authenticated `gh`, and a persistent checkout
of this repository. No Python packages are required. Linux supports tests and the
foreground daemon; LaunchAgent installation is macOS only.

From the persistent next-pr checkout:

```sh
python3 -m unittest discover -s tests -v
python3 -m next_pr init --repo-path /Users/hotinlam/Sites/dkdm.game-853.com/v2
```

State defaults to `~/Library/Application Support/next-pr/`, with private directory
permissions, SQLite WAL/full synchronization, `config.json`, runner manifests,
logs and atomic receipts. `--home /absolute/external/path` or `NEXT_PR_HOME` selects
another location. State inside a Git checkout is rejected. Logs contain prompts,
source diffs and CLI output; keep the directory private.

**Nothing starts before manual billing confirmation.** In each CLI/account's
settings, verify the subscription identity, subscription entitlement, and that
API billing/overage/paid fallback is disabled. Audit user **and project** CLI
configuration, API key helpers, custom providers/endpoints, hooks and MCP servers.
The coordinator rejects known API credential/endpoint environment overrides,
checks subscription login before each model run, and never selects a model or
changes model defaults. Configuration auditing is a human attestation, not an
inferred entitlement or a promise that a vendor will not change its billing.

```sh
codex login status
python3 -m next_pr confirm-provider codex --subscription-only --config-audited \
  --note 'Checked ChatGPT subscription and CLI/project billing settings; no API fallback'

claude auth login
claude auth status
python3 -m next_pr confirm-provider claude --subscription-only --config-audited \
  --note 'Checked Claude subscription and CLI/project billing settings; no API fallback'
```

Initial configuration enables only Codex, unconfirmed; Claude, Grok and Cursor are
disabled. You may activate with verified Codex alone: tasks needing a Claude
writer or independent reviewer visibly block until Claude is verified. A login
shown inside a restricted sandbox may differ from the user's normal terminal;
perform confirmation in the same environment as the service. A login alone does
not establish subscription billing. Do not attest until you have checked it.

Before running work, edit the external `config.json`. Each allowlisted repository
has a `validation` **argv array**, for example:

```json
"validation": ["/absolute/path/to/project-validation-script"]
```

Use the repository's approved test commands (`php84`, appropriate package/build
commands, etc.) in that script. Configure one safe, relevant validation entrypoint;
the coordinator does not guess which monorepo suites to run. An empty array
blocks at validation. Never put shell command strings into the argv array. Only
`skyhilam/dkdm-monorepo` is enabled initially. Adding another repository requires an
explicit config entry with `path`, `base`, `enabled`, `validation` and `auto_merge`;
its checkout's GitHub origin must match exactly. Other repositories always stop
at `mergeable`, even if `auto_merge` is set true.

After those checks, the exact service installation/activation command is:

```sh
python3 -m next_pr install-launchagent --load
```

Without `--load`, it only writes
`~/Library/LaunchAgents/com.next-pr.coordinator.plist`. The LaunchAgent pins the
current Python executable, source checkout and PATH. Keep them installed at those
paths. Use a stable clone for installation, not a disposable development worktree.
`python3 -m next_pr daemon` runs in the foreground; `daemon --once` performs one
reconciliation/scheduling pass. LaunchAgent restarts the manager after a crash.
Workers are separate supervisors and survive a manager restart.

## Submit and operate

After initialization, start the local dashboard in a separate terminal:

```sh
python3 -m next_pr ui --port 8765
```

Open `http://127.0.0.1:8765/` (or `http://localhost:8765/`). The default port is
8765. For a custom state directory, use `python3 -m next_pr --home /path/to/state ui`.
The server runs in the foreground; stop it with Ctrl-C. It does not start the
coordinator. Select a run's **對話** button in the task list to read its Markdown
conversation and terminal output, alongside task controls and coordinator status.
The dashboard accepts only these loopback Host names with the selected port;
task-control POST requests must include the matching `http://host:port` Origin.

Write the complete request to a UTF-8 text file, then:

```sh
python3 -m next_pr submit --title 'Fix the pickup screen' --prompt-file /tmp/request.txt \
  --request-key pickup-screen-20261003 --scope apps/retail-floor-sunmi
python3 -m next_pr status
python3 -m next_pr metrics
python3 -m next_pr pause TASK_ID
python3 -m next_pr resume TASK_ID
python3 -m next_pr cancel TASK_ID
```

A repeated request key with identical input returns the same task. Different
input with that key is rejected. Scope defaults to `*` (the entire repository).
Declare all paths/components the work can affect. Overlap with an unfinished
request requires `--depends-on TASK_ID`; descendants wait for the dependency to
**merge**, not merely finish coding. Dependency declarations are scheduling
contracts; the coordinator cannot infer every semantic conflict from prose.

Primary writers alternate Codex/Claude by submission order. A disabled provider
blocks its assigned request rather than silently substituting another. Codex
writers use a Claude reviewer; other writers use Codex. At most two development
stages and one reviewer run concurrently. All configured heavy validation is
serialized by an OS lock, inherited by its child process. Writer instructions
reserve heavy builds/tests for that configured stage; arbitrary commands inside
a third-party CLI cannot be classified automatically.

The pipeline is draft → writer → serialized validation → read-only independent
review → ready → exact-SHA CI → merge. The worker must commit/push and return the
required JSON contract. Session identifiers, actual usage when emitted, test
claims, process receipts, log paths, PR/worktree/branch, validation/review/CI SHAs,
and fix rounds are durable. Reported test claims are distinct from the supervised
validation exit code and log. Unknown quotas stay `unknown`. `metrics` reports
the first ten tasks' elapsed duration, fix rounds and emitted usage; it does not
estimate vendor credits or available requests.

Review uses the repository's strict [review reference](references/code-review.md),
the exact head and diff. Codex uses read-only sandbox; Claude receives only
Read/Glob/Grep tools with MCP and slash commands disabled; Cursor uses ask mode.
Grok review is disabled until a read-only boundary is verified. Review output
must name the pinned SHA and provide either blockers or a clean verdict. A changed
head invalidates review and validation. Three fixer rounds are allowed in total
across review, validation and CI failures; further failures block for human action.

For dkdm, merge requires `CI summary` success from GitHub Actions, all observed
checks terminal and acceptable, exact reviewed/CI/current head, OPEN, non-draft,
CLEAN, MERGEABLE, and no changes-requested review decision. Unknown, absent,
pending or invalid checks never pass. Merges are serialized and use
`--match-head-commit`; branch deletion uses a matching Git lease. Local worktrees
are retained. GitHub branch protection remains the final server-side protection
against checks changing concurrently. Sunmi hardware acceptance is manual.

## Pauses, failures and recovery

`pause TASK_ID` asks the owning supervisor to stop its process group and retains
the draft/worktree. It does not send signals to stored PIDs. `pause` without an ID
also pauses global scheduling and requests stops for active tasks. `resume`
without an ID resumes scheduling; individually stopped tasks still need an
explicit `resume TASK_ID` after inspection. Cancellation waits for a completion
receipt before releasing a worker slot and never deletes the draft/worktree.

A native rate/usage-limit result pauses that provider. There is no guessed reset
time and no automatic paid fallback:

```sh
python3 -m next_pr resume --provider codex
python3 -m next_pr resume TASK_ID
# Or, after the stopped writer is checkpointed and local/remote heads agree:
python3 -m next_pr resume TASK_ID --handoff claude
```

Handoff is explicit, subscription gated, and only allowed at a stopped writer
checkpoint. The next worker receives the original request, fixes and recorded
evidence. Native session IDs are retained for inspection; v1 starts a fresh CLI
session for each stage instead of guessing whether a vendor session can resume.
Permission denial is a visible block. The coordinator does not add bypass flags,
auto-approve, weaken repository rules, or resolve missing product decisions.

A runner acquires a single-use run lock, records its start, launches the CLI and
writes an atomic completion receipt **after actual exit**. The run assignment is
committed before spawn. Missing receipts, detected surviving process-group
members and ambiguous startup keep the task blocked and its concurrency slot
reserved. Inherited locks remain held until the last inherited descriptor closes,
even when the supervisor exits normally; descriptors closed by descendants are
not a containment boundary. A missing receipt never automatically starts another
writer. If the supervisor itself died,
manually inspect processes and logs, ensure **all** descendants have stopped,
and checkpoint/commit/push any dirty worktree before resuming. Only after that:

```sh
python3 -m next_pr recover TASK_ID --confirm-no-processes
python3 -m next_pr resume TASK_ID
```

Recovery refuses an existing lock, recent startup, existing safe receipt or any still
present recorded PID (including an unrelated reused PID); it never kills that
PID. The explicit attestation covers descendants that cannot be reconstructed
from a crashed supervisor. After the same manual process inspection, recovery preserves an unsafe receipt
as `unsafe-receipt.json` before recording the operator attestation. Do not edit
the SQLite database to force a retry.

Before removing the service, pause tasks and wait for their receipts:

```sh
python3 -m next_pr pause
python3 -m next_pr status
python3 -m next_pr uninstall-launchagent
```

Uninstalling the manager alone does not stop independently supervised workers.

## Optional providers

Grok (`grok --prompt-file ... --no-subagents`) and Cursor (`agent -p ...`) adapters
are present but disabled. Their current login commands do not supply a stable,
verified subscription-entitlement schema. Configure `subscription_probe` as an
operator-audited, read-only, **non-model** argv command whose stdout is exactly
JSON containing `{"subscription": true, "api_credentials": false}` based on actual
authentication evidence, then run `confirm-provider PROVIDER ...` as above.
A hardcoded success/echo script is not verification. No probe means no activation.
An explicitly verified adapter can be selected with a stopped-writer handoff.
OpenCode is disabled. Models remain CLI defaults for every provider.

## Validation and current limits

`python3 -m unittest discover -s tests -v` runs offline fake CLI/GitHub fixtures,
real temporary Git repos and disposable child processes. Tests cover lifecycle,
quota/permissions, idempotency, stale SHA and CI failures/conflicts, concurrency,
dependencies, cancellation, supervisor interruption and inherited locks. CI also
runs CLI help and shell syntax checks. Tests do not call a model or modify remote
repositories. Real vendor CLI output/authentication may change; unknown output
blocks for adapter inspection. Very large prompts/diffs can exceed a CLI's argv
limit and block; they are never interpolated into shell code. No paid calls,
service activation or Sunmi acceptance are part of this repository's test suite.

繁體中文操作摘要：[README.zh-Hant.md](README.zh-Hant.md).
