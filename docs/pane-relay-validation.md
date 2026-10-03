# Relay validation and parent handoff

This fix is stacked on **PR #3**, commit `ff3224dd2881eab4474ba8053cbe52f0b5fe264d`.
Its PR targets `feat/grok-pane-dispatch-20261003`. Neither PR is to be merged by the worker.
The actual supervising owner is **總 · Eng in Grok Bot desktop**; the user routed work
through Eng after initial setup. The parent owns installing the persistent checkout and
updating both the main Bot and Eng’s supervision profile. **The real test disproved the
earlier claim that Shell completion reliably wakes the Bot.** CLI `wait` remains useful
for bounded observation, but cannot provide cross-turn supervision by itself. Parent verified the native `UpdateState` routine **`pane-cli-eng`** in desktop UI:
**enabled** (Pause button), **`@every 5m` / Every 5 minutes**. Five minutes is the native
minimum. Parent will update this same routine to the new wait/reply contract after installation
and perform the ordinary-question smoke test. Parent also verified a real unattended routine
tick at **19:01** with no new user input: Eng relayed a vendor permission menu. At **19:03**,
the user answered, Eng sent Down + Enter, and Claude resumed phpunit. This verifies native
routine wake and that live menu recovery, not the still-pending new-dispatcher smoke test.

## Install and verify (parent)

The existing executable symlink points to
`/Users/hotinlam/.local/share/pane-dispatch/source/bin/pane-dispatch`.
After checking that this retained checkout is clean, fast-forward it to the reviewed fix.
The install branch `fix/cli-conversation-relay-20261003` points to the same commit as draft
PR #4’s existing head branch `fix/pane-conversation-relay`; the PR stays open for its reviewer:

```sh
git -C /Users/hotinlam/.local/share/pane-dispatch/source status --short
git -C /Users/hotinlam/.local/share/pane-dispatch/source fetch origin fix/cli-conversation-relay-20261003
git -C /Users/hotinlam/.local/share/pane-dispatch/source merge --ff-only origin/fix/cli-conversation-relay-20261003
cd /Users/hotinlam/.local/share/pane-dispatch/source
python3 -m unittest discover -s tests -v
bin/pane-dispatch --help
# Shared snapshot: rearm only IDs owned by this Bot or explicitly handed off to it.
bin/pane-dispatch active
# Preserve owner/handle state; native routine activation must be verified separately.
```

Do not recreate or send a reply to the business task without its user's actual instruction.
Use the supervision bootstrap in [the dispatcher guide](pane-dispatch.zh-Hant.md).
`active` is a shared recovery snapshot, not a list of tasks every Bot may monitor. The
native routine must filter it to this Bot’s started-task/explicitly accepted-handoff IDs.
The routine also retains owned pending IDs in Bot state until their result is delivered: an
ID disappearing from `active` is not proof of a delivered result. Read its status once to
relay the final evidence, even if another observer already persisted `reported_ready`.
On handoff, the old owner removes the task from its recovery set; the new owner preserves
and verifies the existing Shell handle, pending question and notified fingerprints.

## Cross-turn wake boundary: failed callback and verified native routine wake

Parent’s real test: background Shell handle **138984**, **18:48:28–18:49:14**, 46 seconds,
exit 0, empty output. It did **not** wake Grok Bot/Eng after final. The next wake was the
parent’s new message at **18:51**, not a background completion callback. This overrides
the earlier Bot assertion and the previous version of these docs. Neither an endless
`watch --follow` nor a terminating `pane-dispatch wait` establishes reliable cross-turn
wake. `AwaitShell` waits only within the current turn. Do not report “monitoring restored”
from a Shell handle, successful process exit, `wait_argv`, or a routine-creation claim.

Parent verified **one existing native UpdateState routine for 總 · Eng** in desktop UI.
No old runner, custom scheduler or Python daemon is introduced. The API names are `Shell`,
`AwaitShell`, `UpdateState`; use the actual desktop schema, without invented arguments.

| Evidence | Status |
| --- | --- |
| Actual routine ID | `pane-cli-eng`, verified by parent in desktop UI. |
| Stored schedule | `@every 5m` / Every 5 minutes; **five minutes is the native minimum**, not one minute. |
| Enabled state | Enabled; UI shows the **Pause** button. |
| Current owner/task scope | 總 · Eng; only owned `tcg-staff-default-path-copy`. Current text checks status + last-message/screen and dedupes by panel/fingerprint. |
| Idle disable / same-ID re-enable | Present in the verified routine text: disable with no runnable owned tasks; re-enable after start/reply. |
| Unattended native routine evidence | Parent reports a 19:01 tick with no new user input, forwarding the vendor permission menu; user answered at 19:03, Eng sent Down + Enter, and Claude resumed phpunit. |
| New dispatcher integration | Parent will update the same routine to wait/reply after installation, then test unattended delivery and disable/re-enable end to end. |

Unattended observation can take **up to five minutes until the next scheduled check**, plus
tool execution time. Active foreground status/wait calls can observe updates promptly.
Never promise realtime callbacks or reliable background Shell completion wakeups.

The native routine contract is:

1. Every five minutes, consider only owned tasks still needing observation. Skip waiting_user,
   cancelled, terminal/error and unknown-delivery tasks awaiting inspection. Read `status`;
   use bounded `wait` only as needed, without overlapping ticks or multiple waits per task.
   Reconcile any existing Shell handle before another wait. Timeout output stays silent.
2. Relay new prompt/options or result fingerprints once, with provenance and Pane link.
   Dedup against **this owner’s** notified `task_id + event_id`/result fingerprint, not just
   `conversation.new` (another status read can make it false without a user notification).
   Consumed old transcripts are never new questions.
3. On a question, save pending event and enter waiting_user. Subsequent routine ticks skip
   that task: no duplicate prompt, no repeated permission request, no automatic answer.
   Terminal/errors stop its automatic observation. Completion still needs report/test/PR/head
   verification; routine execution is not task completion.
4. If no owned runnable tasks remain, disable the same routine. A newly started task or a
   successfully sent `action:submit` reply marks it runnable and re-enables **that same
   verified routine ID**. Do not create routines per task, reply or timeout. Navigation keeps
   menu pending until submission. Unknown delivery pauses for inspection, never retries.
5. Preserve owned IDs, routine ID, existing handles, pending events and notified fingerprints
   in Bot state. Keep receipts in UpdateState, a private `receipts/` subdirectory or outside
   the dispatcher root; never create receipt JSON beside root `*.json` task records.

The routine ID, schedule and enabled state are **UI-verified by parent**, and the 19:01
unattended tick plus 19:03 authorized menu reply/resumption are parent-observed evidence. Installation,
updating that routine to the new dispatcher commands, and end-to-end acceptance remain
with parent; CLI wait alone does not solve cross-turn wake. The detailed owner
bootstrap and tick result table are in [the dispatcher guide](pane-dispatch.zh-Hant.md).
Parent supplies the receipts and installs the contract in both main Bot and 總 · Eng. This
worker does not configure the routine or change desktop state. Parent may stop only the
exact obsolete watch process after confirming handle/command/owner; no broad process kill.

The user’s **go ready** has already been delivered in Eng and business Claude is running.
Do not resend it, recreate the task, or duplicate the observer. The temporary bounded watch
is not proof of unattended supervision; parent coordinates its transition to the verified
native routine.

## Reproduction evidence

Read-only inspection on 2026-10-03 found the business task panel idle without a report.
`panels last-message` returned the exact 925-character Claude “Your message contained
only pasted text” reply, with no message timestamp in the supported schema,
including “Proceed?”, “Draft or ready?”, and “Reply go”. The source explicitly requested
non-draft while the old wrapper said DRAFT. The real Pane IDs are UUIDs without the
literal `pane`/`panel` prefixes in the handoff:

- Pane: `625edc7a-63f3-478d-bc12-c166fc36fed1`
- Panel: `6eba675b-c319-4174-9393-6945997f3513`

A later fixed-status check used a **temporary copy** of its dispatcher record; by then
another actor had resumed the task. It correctly returned `working`, a fresh transcript
progress message, `activity:active`, and `replyable:false`. This fix worker never sent
input to that task or changed its original state/prompt.

## Real CLI acceptance (parent through 總 · Eng, disposable task)

After installation, the parent runs the prepared **read-only red/blue** smoke task through
Eng using `/private/tmp/pane-relay-smoke-task.txt` unchanged as `--prompt-file`; this worker
does not launch it or reply to it. The steps below describe the acceptance flow (use that
prepared prompt instead of creating another one).
The existing business source task’s explicit **ready PR** preference remains authoritative.
This fix PR remains draft as requested.

1. Use the parent's prepared private prompt unchanged; select a disposable saved repo and
   a unique task ID owned by Eng. Do not reuse the business task ID.
2. `pane-dispatch start --task-id relay-acceptance-UNIQUE --repo REPO --cli claude --prompt-file /private/tmp/pane-relay-smoke-task.txt`
3. After installation, update the existing `pane-cli-eng` instructions to this wait/reply
   contract; retain `@every 5m`, verify owner scope, and enable that same routine for the owned
   smoke task. Let Eng finish its turn; without sending any new message, allow up to five
   minutes for the next scheduled check and verify the tick
   actually wakes Eng, reads status/wait and forwards the red/blue question once. Save the
   tick/delivery receipt outside root task JSON. The task then enters waiting_user; verify
   subsequent ticks do not repeat it, and the routine disables if nothing else is runnable.
4. Save the user's exact red/blue answer to a private reply file. Call `reply --task-id ...
   --reply-file ... --event-id ... --reply-id acceptance-reply-1`, then repeat that exact
   command once. The second call must return the existing delivery, without another send.
5. After sent submission, clear pending state and re-enable the **same routine ID**. Verify
   the answered transcript is not forwarded again while Claude works. Let another unattended
   tick relay the chosen colour’s follow-up/result; record the actual routine/tick receipts.
   Stop on the next question or terminal event. `sent`, exit 0 or a scheduled routine alone
   is not proof of resumption/success; a no-PR smoke result cannot satisfy the dispatcher’s
   PR completion evidence contract.
6. For a genuine TUI, bind the user's choice to the current event, use one `--key` at a time,
   and inspect the selected option before Enter. Never approve a real permission prompt
   merely because it appeared in terminal output.

Automated tests cover ordinary Claude questions, existing questions before watch, report
questions, no-PR final text, noisy screens, follow-ups, byte-driven menu transitions,
stale/wrong identities, concurrent sends, durable crash intent, ambiguous send outcomes,
and hard timeout of a real sleeping subprocess. No third-party Python packages are needed.

## Live fake-panel checks performed

In this fix Pane (`2dac6a17-75c2-4343-b211-6b0fbd858f9a`), a Python-only echo
fixture returned an already-idle question through bounded wait, received `fixture-ok`
once, returned an identical result for a duplicate reply ID, and reported `done` on its
explicit panel `844d0132-7b75-4831-943f-4802d9752275`. The dispatcher correctly classified
that no-PR report as `incomplete_report`. A subsequent guard now rejects plain-shell
panels, including this completed fixture.

A second Python fixture declared as a Claude panel (`91fe1a64-84c9-4374-adca-239807a25b70`)
exercised the fail-closed path: Pane accepted panel creation but returned a readiness
failure; inspection reconciled the existing panel without creating a replacement.
`wait` exposed its question. `panels submit` rejected the fake Claude composer;
the dispatcher retained `delivery:unknown`, returned the same record on duplicate reply,
and timed out waiting for resume evidence. It did **not** resend. This is not a real
Claude end-to-end success claim; the parent acceptance steps above exercise that path
with an actual disposable CLI session. Both fixtures are harmless and isolated from
the business task; the second remains waiting with no further inputs sent.

## Inert menu fixture

`tests/fixtures/relay_menu.py` is a standalone Python TUI that only selects Blue/Green,
counts received keys, and exits after confirmation or two minutes. It does not run
commands, edit files, call APIs, make reports, or change any PR preference. Run it in a
terminal with `python3 tests/fixtures/relay_menu.py`; the automated PTY test exercises
actual Down/Up/numeric/Enter bytes:

```sh
python3 -m unittest discover -s tests -p test_pane_relay.py -v
```

A live Pane test used this script in isolated panel
`2d71d5e4-cf0b-4073-ad6a-4470c9e0ad71` of the fix Pane, declared as Claude only to
exercise Pane's CLI routing; no Claude process ran. Pane returned a readiness failure
but its returned panel ID was retained, without creating a replacement. A private
fixture record in `/var/folders/_2/vjprz7s55c30fclnyw7ry10r0000gn/T/pane-inert-menu-71vf00dy`
bound dispatcher replies to that exact panel. Results:

- `wait` returned the current menu and event ID.
- `reply --key down` changed Blue to Green; the same reply ID returned its stored
  delivery and left the count at one.
- Enter with the old event ID was rejected.
- Enter with the new event ID produced `FIXTURE CONFIRMED: Green | Keys received: 2`.
- The fixture exited; no completion report or PR success was fabricated.

The inert-menu addition passed **83 tests**; the subsequent answered-transcript, terminal
wakeup and receipt-isolation regressions bring the full suite to **86 passing tests**. The business task was not read or mutated
for this menu test. The parent’s real ordinary-Claude question/reply test remains with Eng
following installation and updating the UI-verified `pane-cli-eng` routine to the contract above.

## Navigation boundary and lock deadline checks

Confirmed Up/Down delivery returns `action:navigate` and does not consume the menu event.
At the first option, Up can leave screen/event unchanged; a following Enter uses the same
event ID with a new reply ID. Duplicate navigation reply IDs still send at most once.
Enter, text, and numeric keys (which may submit directly) consume the event. Unknown
navigation delivery retains its consuming intent, preventing an automatic retry or Enter.
The parent keeps the pending menu through navigation, reads status, then continues the
already-authorized selection; only submission marks the task runnable and re-enables the same native routine.

Regression tests cover unchanged Up-at-first followed by Enter, duplicate navigation and
submission IDs, ambiguous navigation, real PTY boundary behavior, and a per-task flock held
by a separate process. The lock test uses a 150 ms deadline and requires return within one
second while the other process still holds the lock; the same monotonic total deadline
controls the advertised maximum of 45 seconds. The full suite now has 89 passing tests.

A separate full-duration check held the task lock in another process and called
`wait(..., timeout_seconds=45)`: it returned `timeout` in **45.0027 seconds** (including
scheduling/return overhead), with the holder still alive and the lock still held. It did
not wait for the holder to release the lock or make any Pane calls while contended.

## Review follow-up: persistent blocked report after direct resume

Review of `3558691` identified two blockers. The unchanged Up-at-first then Enter case was
already fixed in `25e978e` and remains covered. A second regression reproduced a stale
blocked report authorizing another reply after a human resumed the panel directly, without
any dispatcher reply receipt. Active panel evidence now supersedes that blocked report;
its question stays in `evidence` with `superseded:true` and reason `observed_active_panel`,
but cannot override the current transcript or authorize input. The same report remains
superseded after idle transitions; a replaced report can supply a new idle question.
Replyability requires current idle evidence or a current explicit menu, never merely a
persistent blocked report. Tests cover rejection before send, first observation already
active, fresh follow-up questions and replacement reports. Parent's ordinary-question
smoke remains pending; this fix does not send input to the business panel. The full suite
after this review fix passes **91 tests** (including 23 relay tests).

## Review follow-up: cross-task reply payload isolation

Reply-file names now hash the JSON tuple `[task_id, reply_id]`, rather than joining IDs
with a colon. The regression interleaves `a:b` / `c` and `a` / `b:c` under independent
real task locks: the second send completes before the first panel reads its payload.
Before the fix, the first panel received the second user's text. With tuple hashing,
both panels receive their own text from distinct files and duplicate delivery remains
at most once. Existing delivery receipts still return before any file creation or send;
unknown intents are not retried. The full suite now passes **92 tests**.

## Real smoke follow-up: unnumbered Claude menu

Parent reported the trust prompt `❯ No, exit` / `  Yes, I trust this folder` with
`Enter to confirm · Esc to cancel`, no numeric labels, and activity `active`. The
regression reproduced `working`/no options before the fix. The parser now recognizes
one selector and a contiguous aligned choice block immediately before an explicit
Enter confirmation footer. It emits `key:null` and `selected` for these options,
never invented numeric shortcuts. Current explicit menus return needs_attention even
with active activity; numeric/text submissions are rejected, while user-bound arrow
and Enter input use the existing identity, fingerprint and at-most-once checks.
Malformed/one-option/unselected/stale-footer cases fail closed. Tests cover navigation,
unchanged boundary Up, duplicate navigation, stale Enter and numeric rejection.

This worker read the original smoke JSON and panel screen only. By that inspection,
task `relay-color-20261003` in `/private/tmp/pane-relay-acceptance-state` had already
advanced to a numbered outside-worktree read permission menu. No input or status-state
write was sent to that task; the unnumbered regression uses the parent's reported text.
User trust/permission decisions and real smoke continuation remain with parent.
The complete suite now passes **94 tests**.
