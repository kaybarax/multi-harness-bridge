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
the host deferred before submission and is safe for a bounded retry. Other
nonzero codes and timeouts are ambiguous and held for reconciliation. `{body}` is a fixed ID-only wake
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
which queues a prompt for the exact session. Claude Code uses its in-session
`mhb monitor PROJECT claude ROLE SESSION` subscription by default. It prints
JSON hints for the bound session and never invokes a detached model turn. A
detached `claude --resume SESSION --print PROMPT` wake is available only when
explicitly enabled with `MHB_CLAUDE_DETACHED_WAKE=1`; it runs in a separate
process and does not wake a desktop window. Devin Local uses ACP `session/load`
and submits a fixed ID-only `session/prompt` to the exact bound session. The
resumed Desktop session uses the installed `mhb_roles_<project>` server. Its
`pickup_wake(notification_id)` and `finish_wake` tools require a matching
active dispatch record for the current native turn, role, and notification.
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
busy/draft/active-user deferral is retried on a later poll and does not consume
the bounded rejection attempt count.

Inspect recent sends with `multi-harness-wake --project my-project
--show-attempts`. After checking the exact native session, resolve an ambiguous
attempt with `--reconcile NOTIFICATION_UUID --outcome sent` or
`--outcome not-sent`. If multiple role attempts for that ID are unresolved,
also supply `--harness`, `--role`, and `--session` from the status output. An
active send cannot be reconciled; wait for its 75 second maximum window to
expire and inspect it again. `not-sent` permits only the remaining bounded
retry; it does not change the mailbox item or acknowledge it.

## Duplicate and uncertain sends

Before dispatch the wake driver records a durable per-notification attempt in
the Store database. A confirmed send is not repeated. A verified pre-submit
refusal can be retried at most three times. If a host call times out or returns
an ambiguous result after it may have submitted the prompt, the attempt is
marked `uncertain` and requires manual reconciliation. Later deliveries for the
same role remain eligible for a wake. Each wake record identifies the exact
project, harness, role, and native session. A replacement chat can receive a
new wake for pending work. The durable queue validates the current binding and
lease before advancing the message.
