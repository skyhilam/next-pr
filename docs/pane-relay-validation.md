# Relay validation and parent handoff

This fix is stacked on **PR #3**, commit `ff3224dd2881eab4474ba8053cbe52f0b5fe264d`.
Its PR targets `feat/grok-pane-dispatch-20261003`. Neither PR is to be merged by the worker.
The actual supervising owner is **總 · Eng in Grok Bot desktop**; the user routed work
through Eng after initial setup. The parent owns installing the persistent checkout and
updating Eng’s supervision profile. Native background callback/reminder support is under
parent investigation; no particular API or unattended capability is assumed yet.

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
bin/pane-dispatch active
bin/pane-dispatch status --task-id tcg-staff-default-path-copy
bin/pane-dispatch wait --task-id tcg-staff-default-path-copy --timeout-seconds 45
```

Do not recreate or send a reply to the business task without its user's actual instruction.
Use the supervision bootstrap in [the dispatcher guide](pane-dispatch.zh-Hant.md).
`active` is a recovery snapshot; `wait_argv` is not unattended monitoring. If the desktop has
no native tool callback, the Bot must disclose when its observation loop stops and recover
with `active` plus `status`/`wait` on its next turn.

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

After installation, the parent runs this ordinary-question/reply check through Eng.
The existing business source task’s explicit **ready PR** preference remains authoritative.
This fix PR remains draft as requested.

1. Write a private prompt for a disposable repo: “First ask me which label to print and
   wait. After my reply, print that exact label and use the validated explicit Pane/panel
   identity protocol to report blocked with question 'Fixture finished; no PR was created.'
   Do not edit, commit, push, or open a PR.”
2. `pane-dispatch start --task-id relay-acceptance-UNIQUE --repo REPO --cli claude --prompt-file PRIVATE_FILE`
3. Immediately `wait --task-id relay-acceptance-UNIQUE --timeout-seconds 45`. Continue on
   timeout; forward the actual question/options, retaining `conversation.event_id`.
4. Save the user's exact label to a private reply file. Call `reply --task-id ...
   --reply-file ... --event-id ... --reply-id acceptance-reply-1`, then repeat that exact
   command once. The second call must return the existing delivery, without another send.
5. Continue wait/status until the label and explicit blocked report appear. A send returning
   `sent` alone is not proof of resumption, and this fixture must never report task success.
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

All **83 tests** passed after this addition. The business task was not read or mutated
for this menu test. The parent’s real ordinary-Claude question/reply test remains with Eng
following installation; native callback/reminder specifics will be documented when verified.
