# Relay validation and parent handoff

This fix is stacked on **PR #3**, commit `ff3224dd2881eab4474ba8053cbe52f0b5fe264d`.
Its PR targets `feat/grok-pane-dispatch-20261003`. Neither PR is to be merged by the worker.
The actual supervising owner is **總 · Eng in Grok Bot desktop**; the user routed work
through Eng after initial setup. The parent owns installing the persistent checkout and
updating both the main Bot and Eng’s supervision profile. Eng confirmed that native
`Shell` background completion notifies and wakes the Bot after a turn ends, but stdout
lines from a still-running process do not. `AwaitShell` only waits within a turn.
`UpdateState` stores the supervising Bot state; no routine or daemon is needed.

## Install and verify (parent)

The existing executable symlink points to
`/Users/hotinlam/.local/share/pane-dispatch/source/bin/pane-dispatch`.
After checking that this retained checkout is clean, fast-forward it to the reviewed fix:

```sh
git -C /Users/hotinlam/.local/share/pane-dispatch/source status --short
git -C /Users/hotinlam/.local/share/pane-dispatch/source fetch origin fix/pane-conversation-relay
git -C /Users/hotinlam/.local/share/pane-dispatch/source merge --ff-only origin/fix/pane-conversation-relay
cd /Users/hotinlam/.local/share/pane-dispatch/source
python3 -m unittest discover -s tests -v
bin/pane-dispatch --help
# Shared snapshot: rearm only IDs owned by this Bot or explicitly handed off to it.
bin/pane-dispatch active
# Recover only owned task IDs; preserve Eng’s existing bounded-watch handle.
```

Do not recreate or send a reply to the business task without its user's actual instruction.
Use the supervision bootstrap in [the dispatcher guide](pane-dispatch.zh-Hant.md).
`active` is a recovery snapshot; `wait_argv` alone is not monitoring. Use **native Shell
background execution of bounded `pane-dispatch wait --task-id ID --timeout-seconds 45`**,
not an endless `runpane watch --follow`. Store the returned shell handle per task with
UpdateState. Both Bots share this dispatcher: filter `active` to IDs in that Bot’s own
started-task/accepted-handoff ownership set before considering any rearm. Never adopt all
active records or infer ownership from visibility. Unowned/ambiguous tasks remain inspection
only. On handoff, the old owner removes the task from its recovery set and the new owner
preserves/verifies the transferred handle, pending question and forwarded event IDs.
Installing the contract in both profiles does not authorize a second waiter. Maintain
exactly one active wait per task; on a matching completion callback,
clear that handle once, inspect the result, and silently rearm only if the task is still
active without a pending question. Ignore duplicate/stale callbacks. Do not spam timeout
output. A question is forwarded once, bound to its event ID, and **stops background
rearming until the user's reply**. After a successfully sent reply, arm again; unknown
delivery/errors stop for inspection. Terminal/error events stop rearming; completion
requires full result evidence and verification. AwaitShell is not a cross-turn callback.
See the exact owner contract and result table in the dispatcher guide.

A reply consumes its observed question fingerprint. The unchanged transcript remains
visible as evidence with `consumed:true`, `new:false`, `replyable:false`; a busy transition
does not make it a new message. `wait` stays blocked until new content, terminal/error
evidence, or timeout. A panel exit must still wake it. Shell/notification receipts belong
in UpdateState, a private `receipts/` subdirectory, or outside the dispatcher state root;
never put them among the root `*.json` task records. Reply payloads remain `.reply` files
and durable delivery intent stays inside the owning task record.

The parent installs this instruction in both the main Bot and actual 總 · Eng owner. The
parent alone will stop their exact obsolete `watch --follow` process after confirming its
Shell handle and command; no broad process kill, routine, daemon, or unsupported callback
API is part of this fix. The user has now answered **go ready** in Eng and the business
Claude is running. Eng has a temporary bounded watch restoring supervision. Do not send
that reply again, recreate the task, or duplicate its existing observer. Parent coordinates
any transition to the installed dispatcher using the existing owner/handle state.

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
3. Use native Shell background mode for `wait --task-id relay-acceptance-UNIQUE
   --timeout-seconds 45` and store its returned handle. Let the Bot turn end. Verify its
   completion callback wakes Eng. On timeout while active, clear the handle and silently
   rearm exactly one wait. On the question, forward text/options, save `conversation.event_id`,
   and verify **no waiter is rearmed while this question awaits the user**.
4. Save the user's exact red/blue answer to a private reply file. Call `reply --task-id ...
   --reply-file ... --event-id ... --reply-id acceptance-reply-1`, then repeat that exact
   command once. The second call must return the existing delivery, without another send.
5. After sent delivery, clear pending state and arm one new bounded background wait.
   Verify the old question is not forwarded again while Claude works. Handle callbacks
   until the chosen colour’s follow-up/result appears; stop on the next question or terminal
   event. `sent` alone is not proof of resumption, and a read-only no-PR smoke result cannot
   satisfy the dispatcher’s PR completion evidence contract.
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
following installation, using the confirmed Shell completion contract above.

## Navigation boundary and lock deadline checks

Confirmed Up/Down delivery returns `action:navigate` and does not consume the menu event.
At the first option, Up can leave screen/event unchanged; a following Enter uses the same
event ID with a new reply ID. Duplicate navigation reply IDs still send at most once.
Enter, text, and numeric keys (which may submit directly) consume the event. Unknown
navigation delivery retains its consuming intent, preventing an automatic retry or Enter.
The parent keeps the pending menu through navigation, reads status, then continues the
already-authorized selection; only submission rearms the background wait.

Regression tests cover unchanged Up-at-first followed by Enter, duplicate navigation and
submission IDs, ambiguous navigation, real PTY boundary behavior, and a per-task flock held
by a separate process. The lock test uses a 150 ms deadline and requires return within one
second while the other process still holds the lock; the same monotonic total deadline
controls the advertised maximum of 45 seconds. The full suite now has 89 passing tests.

A separate full-duration check held the task lock in another process and called
`wait(..., timeout_seconds=45)`: it returned `timeout` in **45.0027 seconds** (including
scheduling/return overhead), with the holder still alive and the lock still held. It did
not wait for the holder to release the lock or make any Pane calls while contended.
