# Native automatic wake drivers

`multi-harness-wake` polls the registered project's mailbox and nudges the
exact already-bound native session. It never claims or acknowledges a message;
the receiving assistant remains responsible for reading and processing the
mailbox through the normal protocol. The dispatcher checks the Store's enabled
state and role binding on every cycle, and polls every four seconds by default.
SIGINT and SIGTERM stop the polling loop.

## Adapter contract

Codex, Claude Code Monitor, Devin ACP, and Antigravity Desktop have built-in
drivers. `MHB_WAKE_CONFIG` is an optional per-harness override or fallback for
another host or operating system. There are no project names, session IDs, or
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
  "devin": {"verify": ["/path/to/verify", "{session}"], "wake": ["/path/to/wake", "{session}", "{body}"]},
  "antigravity": {"verify": ["/path/to/verify", "{session}"], "wake": ["/path/to/wake", "{session}", "{body}"]}
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
and submits a fixed ID-only `session/prompt` to the exact bound session. Its
temporary server is named `mhb_devin_<project>` and exposes only
`pickup_notification(notification_id)`. The ACP client approves that call only
when the exact native session, uncompleted native tool-call ID, server, tool,
and UUID argument all match. Unknown calls and shell permissions are
cancelled. The Inbox tool durably forwards the exact notification to the bound
Coordinator; the Coordinator tool returns the message and records its
receipt. Message content is never included in the wake prompt or process argv.
Antigravity Desktop uses the packaged macOS native adapter and accessibility
helper. Each driver validates the current Store role and stop state first.

The Antigravity AX helper is macOS-only. Other operating systems should
configure a native host executable that implements the same verifier/launcher
contract; this package does not automate GUI input with coordinates or keyboard
shortcuts.

Install the optional MCP dependency (`multi-harness-bridge[mcp]`) to run the
temporary Devin server. Configure executable overrides only when a built-in
driver does not cover the host installation.

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
`--outcome not-sent`. The latter permits only the remaining bounded retry; it
does not change the mailbox item or acknowledge it.

## Duplicate and uncertain sends

Before dispatch the wake driver records a durable per-notification attempt in
the Store database. A confirmed send is not repeated. A verified pre-submit
refusal can be retried at most three times. If a host call times out or returns
an ambiguous result after it may have submitted the prompt, the attempt is
marked `uncertain` and is not automatically retried; inspect the native
session and reconcile manually. A pre-existing wake record remains
authoritative even if a role is later rebound, because the old send's outcome
cannot safely be inferred from the new binding.
