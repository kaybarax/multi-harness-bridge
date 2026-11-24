# Native automatic wake drivers

`multi-harness-wake` polls the registered project's mailbox and nudges the
exact already-bound native session. It never claims or acknowledges a message;
the receiving assistant remains responsible for reading and processing the
mailbox through the normal protocol. The dispatcher checks the Store's enabled
state and role binding on every cycle, and polls every four seconds by default.
SIGINT and SIGTERM stop the polling loop.

## Adapter contract

Codex, Claude Code Monitor, Devin ACP, and Antigravity Desktop have built-in
drivers. `MHB_WAKE_CONFIG` is an optional override for the Codex, Claude, and
Devin drivers. There are no project names, session IDs, or
credentials compiled into this package. Each configured adapter has `verify`
and `wake` argv arrays and an optional `timeout_seconds` value:

```json
{
  "codex": {
    "verify": ["/path/to/verify-session", "{project}", "{harness}", "{role}", "{session}"],
    "wake": ["/path/to/wake-session", "{project}", "{harness}", "{role}", "{session}", "{notification_id}", "{body}"],
    "timeout_seconds": 8
  },
  "claude": {"verify": ["/path/to/verify", "{session}"], "wake": ["/path/to/wake", "{session}", "{body}"]},
  "devin": {"verify": ["/path/to/verify", "{session}"], "wake": ["/path/to/wake", "{session}", "{body}"]}
}
```

The `verify` executable must exit zero and print one JSON object containing
exactly matching `project`, `harness`, `role`, and `session` values. It should
query the host's actual current destination, not echo its arguments. The
dispatcher runs it before sending and again afterward to detect a changed
native binding. A verifier refusal is a safe retryable preflight failure.
The `wake` executable receives its arguments directly (no shell expansion),
must target that verified session through its native host, and returns zero
only after the host confirms acceptance. Exit code 2 or 3 explicitly means
the host deferred before submission and the dispatcher can retry on a later poll. Other
nonzero codes and timeouts can leave the wake uncertain until the recovery window. `{body}` is a fixed ID-only wake
prompt. The queued message body is never copied into a native prompt or process
argument. Supported placeholders are
`project`, `harness`, `role`, `session`, `notification_id`, and `body`.

An example executable can use the macOS accessibility helper in
[`../native/ax_wake.swift`](../native/ax_wake.swift) for a desktop host that
exposes the session ID in its accessibility tree. Compile it locally:

```sh
swiftc native/ax_wake.swift -framework AppKit -framework ApplicationServices \
  -o "$HOME/.local/bin/mhb-ax-wake"
```

The helper requires the exact app bundle ID, exact window title, visible
session ID, one accessible empty composer, one uniquely identified Send or
Submit button, and macOS Accessibility permission. It refuses a missing or
ambiguous window, a hidden session identity, a non-empty composer, or uncertain
controls. Configure the `wake` argv for the appropriate host wrapper to invoke
this helper with those exact UI identity values plus a fixed mailbox wake
prompt. Pair it with a `verify` program that independently checks the same
session against the host's current native role binding. The helper cannot
establish the native mailbox role by itself.

## Host differences

Codex uses its `codex queue --thread SESSION --message PROMPT` host interface,
which queues a prompt for the exact session. On macOS the dispatcher first
opens `codex://threads/SESSION` so an unloaded Desktop thread attaches to the
app-server and can drain the queue; the deep link briefly navigates the
Desktop UI. Queued items only dispatch at turn boundaries, so a busy
coordinator chat can hold a wake behind a manual steer. A project can instead
opt into the agent relay with `mhb option set PROJECT codex coordinator_wake
agent-relay`, which records the choice in `<MHB_HOME>/options.json` and
merges `approval_policy = "on-request"` and `approvals_reviewer =
"auto_review"` into each registered workspace's `.codex/config.toml` without
removing existing keys. Codex applies approval mode per thread at creation,
so the bound inbox thread must be created after that file exists; the relay
tool refuses older threads. While a coordinator-bound message is younger than
the relay grace window the dispatcher reports `relay-pending` instead of
queueing. The inbox agent's `bridge-mailbox` finish then returns a
`wake_relay` instruction, and the agent invokes the native
`send_message_to_thread` tool once with the exact `threadId` and `prompt` to
inject the wake into the coordinator's active turn. The relay prompt is a
short reviewer-safe variant because Codex's `auto_review` approval reviewer
rejects the queue `WAKE_PROMPT` wording for cross-thread sends. Only after that tool call
succeeds does the agent run `--mailbox relay-confirm --message-id <id>`, which
records the relay in the wake ledger and suppresses the fallback queue for
that notification. A failed or skipped tool call leaves the fallback eligible:
past the grace window the normal `codex queue` path can deliver. An unconfirmed
relay reservation holds that session until it finishes or reaches the recovery window.
Claude Code uses its in-session
`mhb monitor PROJECT claude ROLE SESSION` subscription by default. It prints
JSON hints for the bound session and never invokes a detached model turn. A
detached `claude --resume SESSION --print PROMPT` wake is available only when
explicitly enabled with `MHB_CLAUDE_DETACHED_WAKE=1`; it runs in a separate
process and does not wake a desktop window. Devin Local uses ACP `session/load`
and submits a fixed ID-only `session/prompt` to the exact bound session. The
resumed Desktop session uses the installed `mhb_roles_<project>` server. Its
`pickup_wake(notification_id)` and `finish_wake` tools require a matching
active dispatch record for the current native turn, role, and notification.
An exact native turn can still finish after its ACP connection times out if the
bound route and pickup lease remain valid. A `session_locked` response to
`session/load` defers the wake before prompt submission and leaves it eligible
when that chat is available again. The driver also checks the bound chat's
daemon state with `devin list --format json --process-model daemon` and reads
its recent native transcript before loading and again just before submitting
the prompt. A running or waiting native turn defers pickup. If the transcript
has a recent user prompt or tool activity without a terminal assistant result,
the dispatcher holds the wake for up to five minutes after that activity.
An unreadable transcript defers pickup until it can be verified. ACP v1 uses
separate state and submission operations, so a user turn can start between
the final check and submission.
The ACP client checks the native session, pending tool-call ID, server, tool,
and exact notification ID for permission requests. It checks native tool
results and the durable queue even when Devin handles approval internally.
Inbox pickup forwards the notification. For a free-text Coordinator wake,
pickup returns the message, shared `request_id`, and lease. Once the native
transcript records that result, a second scoped call can record receipt using
the lease and send one correlated reply if the request asks for one. The
coordinator's regular project MCP server also exposes `reply_free_text` for a
reply in the bound Local chat. Message content stays in the tool result and
mailbox. The wake prompt and process arguments carry the notification ID.
Antigravity Desktop uses an App-managed sidecar and its documented
`agentapi send-message` command. Enable it as described in the
[Antigravity adapter guide](antigravity.md). The sidecar verifies the exact
pending notification, workspace, and role before sending a prompt to the
bound conversation. An accepted submission records a wake. The native
mailbox tool records pickup when the assistant calls it. If the model fails,
inspect the native turn and process the same notification from its bound chat
after the App recovers.
For a free-text request or reply, the wake sequence is the sender's Inbox,
then the recipient's Inbox, then the recipient's Coordinator. Each wake
provides the delivery ID to process using the `bridge-mailbox` skill.

Keep native tool approvals scoped to the project, role and operation.

Install the optional MCP dependency (`multi-harness-bridge[mcp]`) and the
project's native Devin server before running automatic wake. Refresh the
server in the existing Desktop chat after an upgrade. Configure executable
overrides when a built-in driver does not cover the host installation.

## Installation and monitoring

Install the package in the user's Python environment, configure
`MHB_WAKE_CONFIG`, and run one cycle before enabling a background service:

```sh
export MHB_WAKE_CONFIG="$HOME/.config/multi-harness-bridge/wake.json"
multi-harness-wake --project my-project --once
multi-harness-wake --project my-project
mhb monitor my-project claude inbox BOUND_SESSION_ID
```

`--harness` and `--role` can narrow a run. Omit `--once` for continuous
polling. Run under the same logged-in desktop account that owns native-session
accessibility permissions. A macOS LaunchAgent or user-level systemd service
can launch the command at login. Fill in the paths and project in
[`../examples/multi-harness-wake.plist.example`](../examples/multi-harness-wake.plist.example)
or [`../examples/multi-harness-wake.service.example`](../examples/multi-harness-wake.service.example).
Set the working directory to a registered workspace so project resolution
remains unambiguous. Stop it with the service manager; SIGTERM is handled
cleanly. Keep the wake JSON private if its executable paths contain private
data. Do not put message bodies, session identifiers, or credentials in
service arguments.

Install one service per registered project. Run `multi-harness-wake` once
manually for that project's workspace before enabling the service. A native
busy, draft, or active-user deferral can retry on a later poll without spending
an attempt. Other definite rejections retry with a delay that grows from two
seconds to one minute. There is no attempt limit.

Inspect recent sends with `multi-harness-wake --project my-project
--show-attempts`. Each row includes the exact notification ID, attempt and lane
states, queue stage, lease deadline, earliest time-based retry eligibility, and
the number of automatic recoveries. A bound session receives one dispatch at a
time. Finishing the current stage starts a short cooldown. The lane survives
dispatcher restarts and applies independently to each project, harness, role,
and session. The cooldown gives the native turn time to exit.

For an ambiguous `sending` attempt, inspect the exact native session and use
`--reconcile NOTIFICATION_UUID --outcome sent` or `--outcome not-sent`. If
multiple role attempts for that ID are unresolved, also supply `--harness`,
`--role`, and `--session` from the status output. A `sending` attempt becomes
eligible for reconciliation after 135 seconds. `not-sent` re-arms the exact
wake after backoff and retains its previous attempt count. Both reconciliation
outcomes record the prior attempt in wake history.

For a previously accepted wake whose message is still pending and unprocessed,
first verify that the exact bound native session is idle and the ID has not
already been handled. Re-arm only that ID with the actual harness, role, and
session for the route:

```sh
multi-harness-wake --project my-project --renotify NOTIFICATION_UUID \
  --harness codex --role inbox --session BOUND_SESSION
```

The previous accepted attempt remains in wake history. The exact mailbox item
stays available for the bound receiver to claim.

## Duplicate and uncertain sends

Before dispatch the driver records each attempt in the Store. An ambiguous
native result leaves an `uncertain` attempt. After 135 seconds a dispatcher can
recover an aged `sending`, `uncertain`, `sent`, or `claimed` lane. It waits for
any active lease on the bound route. If the exact notification is still pending,
the next attempt preserves its previous state and timestamps in wake history.
If the queue already advanced, the dispatcher gives the native turn its cooldown
and moves on to later work. A replacement chat can receive a wake for pending
work after the previous lease expires.

A late host can display another notice for the same ID. The exact-ID claim
and lease allow each durable queue stage to advance once. A late dispatcher
result can only finalize the reservation that produced it.
