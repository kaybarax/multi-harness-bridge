# Operating a bridge

## First run

Start with two local chats in one harness. Register the workspace, bind a coordinator and an inbox, and send a short request with a unique key. Record the message ID. Confirm the inbox receives it, the coordinator sees it after forwarding, and the origin receives a reply linked to that ID. Repeat in a second project with the same human-readable key. A reply should be invisible to the other project. The [skills guide](skills.md) covers naming and rotating chats.

A coordinator holds the conversation with its user, assigns work to subagents as the host permits, and makes decisions. An inbox reads, forwards and acknowledges. Rotating a coordinator changes where future work is delivered. Existing subagents and their local work remain attached to their original sessions. Use a short handoff summary identifying their owners, open decisions and pending message IDs.

## Distinguish the stages

| Observation | Where to look next |
|---|---|
| Sender has a bridge message ID | `mhb trace PROJECT ID` shows the recipient and current queue stage. |
| Message stays in inbox | Run `mhb status PROJECT HARNESS`. Check both role bindings, pickup state, native process, and the exact workspace. |
| Native chat woke but forwarding stalled | Read the tool result in that inbox. A skipped permission request, busy session, stale binding or unsent draft can prevent forwarding. Check the lease before retrying. |
| Inbox forwarded but coordinator has not acted | Inspect the coordinator queue stage, destination chat ID and native pickup result. |
| Wake reports `sending`, `uncertain`, or `sent` while the message is pending | Run `multi-harness-wake --project PROJECT --show-attempts` and match the ID and role to `mhb trace PROJECT ID`. Check the lane's last update and the lease deadline. The dispatcher can recover a 135-second-old lane when the exact queue stage is still pending and any active lease has expired. |
| Devin reports `session_locked` | The bound chat was busy at `session/load`. The dispatcher keeps the notification pending and tries again on a later poll. Compare the exact session, timestamp, and notification ID in the wake report with the current native chat. |
| Devin reports an active or recent unfinished turn | The wake stays pending while the bound chat runs. Check that chat's latest turn and the exact wake ID in the dispatcher log. Recent unfinished transcript evidence expires after five minutes. A native running or waiting state continues to defer pickup while the turn is active. |
| Codex wake sits queued | `codex queue` items dispatch at turn boundaries in a thread the Desktop app has loaded. On macOS the dispatcher opens `codex://threads/<id>` first. A busy coordinator can use the optional agent relay. An unconfirmed relay holds the session until it finishes or reaches the recovery window. See [native wake](native.md#host-differences). |
| Telegram displayed the message but the reply hasn't returned | Reply to the specific Telegram message. Check the bot, chat, authorized human ID and provider message ID against the route receipt. |
| GUI seems slower than the CLI | Measure enqueue, first claim, native wake acceptance, coordinator pickup and external receipt separately. A web or desktop agent may take much longer than a SQLite claim. |
| A fix is installed and the same old error recurs | Check the executable path, version, running PID, log modification time and last actual error. Reload the affected long-running process after an in-flight send drains. Check its new PID. |

`mhb pending PROJECT --harness HARNESS` lists unfinished deliveries. Use `mhb doctor` to inspect available command-line tools. An empty queue, a successful process exit, or a sent wake by itself is insufficient evidence of a completed round trip.

## Stop and resume

Use the coordinator's pickup tool or the trusted operator commands `mhb stop PROJECT HARNESS` and `mhb resume PROJECT HARNESS`. Stop prevents new sends from that harness and new claims by its receiver and coordinator. A peer can enqueue a message addressed to the stopped harness. A lease already granted may finish. The database retains pending work during the stop. Verify that only the intended project and harness changed state.

To shut down service processes, stop the Telegram consumer and native wake monitor through the OS service manager used to start them. Existing state survives restart. Refer to [native services](native.md) for the difference between queue control and an OS process.

## Recover a delivery

1. Identify the project, message ID, current stage, sender, recipient and current role. Keep ordinary chat titles out of routing decisions.
2. Examine durable receipts and, for a desktop handoff, the actual source and destination chats. Preserve an uncertain provider outcome for reconciliation.
3. Retry a known unsubmitted operation with its original idempotency key and payload. A new key denotes a different logical message. An expired queue lease can be reclaimed with a new lease token.
4. For a possible external submission, match a native receipt or provider message ID before recovery. A lost HTTP response can follow a successful Telegram send. Blindly repeating it can duplicate the message.
5. Confirm the return path, the exact reply reference and the final recipient acknowledgement. Record which step was observed when reporting a failure.

For Antigravity, keep the App event, committed native turn, mailbox receipt, and correlated reply as separate evidence. A provider quota error can leave a committed turn at the Inbox or Coordinator stage. Record the precise reset time and preserve the wake history, leases, and original delivery IDs while waiting. See the [Antigravity recovery procedure](antigravity.md#verify-delivery-and-recover-a-held-notice).

When the final recipient is a person, complete the machine delivery once the provider receipt has been persisted. A reply hours later creates a new, correlated message. A human response time never determines a queue lease duration.

## State, upgrades and moving machines

Keep the state directory on a local filesystem with working SQLite locks. Multiple bridge processes may share that directory on **one** machine. Independent machines running separate clones have independent queues. The current system does not provide a distributed SQLite cluster.

For an offline backup, stop the user services and active shell clients first. Copy the complete state directory including any SQLite sidecar files and Telegram state configured elsewhere. Protect the copy as you would a credential store. Saving the main database file alone while writers are active can miss committed WAL data. Native transcripts and desktop bindings live in their respective applications.

On a second computer, install the package, register the new workspace paths, connect the native apps and bind fresh local chats. Restore bot secrets through the user's secret manager. Old native conversation IDs, absolute workspace paths and macOS Accessibility permissions are machine-specific. Keep the old Telegram consumer stopped before enabling a new consumer for the same bot token.

For an upgrade, snapshot state, install the new package into the service environment, run `mhb self-test`, check schema compatibility, and restart the appropriate services. A client that encounters a newer unsupported queue schema fails closed. Keep user state and authentication files outside the Git checkout.
