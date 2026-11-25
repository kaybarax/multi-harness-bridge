# Coordinator pickup: pull and push

Each harness chooses how its Coordinator chat learns about new mail, with the
per-project `coordinator_pickup` option.

| Mode | What happens | Default for |
| --- | --- | --- |
| `pull` | The Coordinator collects its own mail. A shared watcher forwards the inbox stages without a model turn and never opens or selects a chat in normal operation. | Codex, Claude Code, Devin Local |
| `push` | The original flow: the wake service nudges the Bridge Inbox chat, which forwards each message and wakes the Coordinator. | Antigravity Desktop (and any harness set to `push`) |

```sh
mhb option set my-project codex coordinator_pickup push   # move one harness back
mhb option unset my-project codex coordinator_pickup      # return to the default
```

`push` keeps the earlier behaviour exactly, so a harness can be moved back
with one setting and no new release.

**Upgrading from 1.4 or earlier.** Codex, Claude Code and Devin Local switch to
`pull` unless you set `push`. Their inbox stages are then forwarded only by the
shared watcher. Install it (`multi-harness-service --watch`); until you do, the
existing wake service starts a watcher when it finds none running. Claude Code
coordinators also need their listener: retype take coordinator in that chat,
or run `mhb pickup PROJECT claude coordinator` and start the command it
prints. To upgrade without changing behaviour first, set
`coordinator_pickup push` for each harness, then move them one at a time.

## Parts

- **Shared watcher** (`multi-harness-watch --project P`): one detached
  process per project, run by the OS service manager and owned by no harness
  app. For every `pull` harness it forwards the `outbound` and `inbox` stages,
  dispatches non-navigating nudges, applies the fallback below, appends to the
  history feed, schedules digests and checks listener health. It never claims
  or acknowledges Coordinator mail. An OS file lock beside the store keeps one
  watcher per project; a second start exits 0, and the kernel releases the
  lock when a watcher dies, so a leftover file never blocks a start.
- **Listener** (`mhb listen P HARNESS coordinator SESSION`): for a host that
  lets a chat watch for itself (Claude Code, through its Monitor tool). It
  prints one JSON line per hint with message IDs, senders and kinds, never a
  body. Requests are hinted at once; replies and notices are grouped for up to
  `batch_seconds`. Unclaimed mail is hinted again after the grace window. It
  exits with code 3 as soon as its chat is no longer the bound Coordinator, so a
  handover never leaves two chats listening; the chat must not re-arm after
  that code.
- **Nudge**: for a host without self-listening, the watcher sends a short hint
  with the notification ID through a non-navigating host interface (Codex:
  `codex queue` without opening the thread; Devin Local: ACP `session/prompt`
  to the bound session).

A message counts as delivered only when the bound session claims it. Hints,
repeat hints and the fallback can overlap; the claim and lease still let each
stage advance once.

## The fallback

Only the fallback may open a chat, and only when the host adapter can prove the
chat is cold:

| Setting | Values | Default |
| --- | --- | --- |
| `fallback_open` | `never`, `after_wait`, `always` | `after_wait` |
| `fallback_grace_seconds` | 10 to 86400 | 180 |

With `always`, every nudge also opens the chat (the earlier behaviour); use it
only as an explicit choice. With `after_wait`, the Codex adapter opens the thread once per message, after
the grace window, when the hint is unclaimed, the queued hint is not in the
thread's transcript, and the transcript shows no activity in the window. A busy
thread is left alone, because Codex drains its queue at the end of a turn. If
the transcript cannot be read, nothing is opened. Claude Code and Devin Local
have no way to open a chat from outside; their fallback is a report. A Codex
thread whose last turn was interrupted keeps its queue paused until Resume is
clicked; that is detected and reported, never opened. An open that still does
not deliver is reported, never repeated. Every decision, with the value of each
condition behind it, is recorded and can be listed with `wake.pickup_events`.

## Bridge chat: history board and digests

Under `pull` the Bridge Inbox chat manages nothing. The watcher writes each
delivery to a private history feed. Every `digest_minutes` (default 30), if
messages for that harness arrived, it marks a digest ready and tells the
bridge chat without opening it: a Claude Code bridge listener prints a digest
line, Codex receives a queued note, and Devin Local receives a scoped prompt
that may call only `bridge_digest`. A chat that is not loaded sees it when it is
opened. The bridge chat shows the digest with `multi-harness-skills --mailbox
digest` (Devin: `bridge_digest()`), one line per message, ending with "Reply
'stop digests' to pause them." `--digest-action off` and `on` (Devin:
`bridge_digest("off")`, `bridge_digest("on")`) store the choice in
`options.json`.

| Setting | Values | Default |
| --- | --- | --- |
| `digest` | `on`, `off` | `on` |
| `digest_minutes` | 1 to 1440 | 30 |
| `batch_seconds` | 0 to 600 | 30 |

## Take coordinator and take bridge

Under `pull`, a verified takeover also makes sure the project's watcher is
running and returns a `pickup` section: the watcher's health and, for a
self-listening host, the exact listener command for the chat to start. Typing
take coordinator again after a restart repairs and re-confirms both. `mhb watch
P status` reads the watcher's health; `mhb watch P ensure` starts it only if no
live watcher holds the lock (a hung watcher is restarted through the service
manager).

If role handoff in your project is done by another tool, call `mhb pickup P
HARNESS ROLE` after that tool binds a chat. It is the attachment point: it
starts or verifies the watcher and prints the listener command for the chat
that is currently bound (`--no-start` only reports).

## Alerts

A stuck message raises one alert, never one per retry. Each alert names the
chat and its fix (for example "click Resume in the codex coordinator chat
\"Name\"", "open the ... chat", or "retype take coordinator in the ... chat").
The bridge chat hears first: the alert enters the history feed and a digest is
marked ready at once, even with digests off. If the message is still stuck
after `alert_escalate_minutes` (default 15, `0` sends at once), the alert is
sent once through the `alert` argv in the `MHB_WAKE_CONFIG` file, for example
a chat relay. Placeholders: `{project}`, `{text}`.

```json
{"alert": ["/path/to/notify", "{project}", "{text}"]}
```

## Installing the watcher

```sh
multi-harness-service --watch           # writes a launchd agent on macOS
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.multi-harness-bridge.watch.my-project.plist
```

On Linux and Windows, run `multi-harness-watch --project my-project` under your
user service manager. Keep the original wake service for any harness that stays
on `push`; it now skips harnesses set to `pull`, so no harness is dispatched by
both.

## Adding a host

The pull behaviour sits behind one port, `pickup.CoordinatorPickup`, with
`listener()`, `check()`, `fallback()` and `digest_nudge()`. Register another
host with `pickup.register(name, factory)`; the store, dispatcher and watcher
need no change.
