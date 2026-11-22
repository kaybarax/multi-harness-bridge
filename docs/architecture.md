# Design and failure boundaries

## Why SQLite

The bridge is designed for concurrent local processes on one user's machine. Separate stdio FastMCP processes, the native wake worker, and command-line clients share the portable queue for their registered project. Point each client at the same `MHB_HOME`. Project and native role verification scope every mailbox operation. Use dedicated state when deploying alongside an existing legacy mailbox. See the [local MCP setup](protocol.md) and [legacy deployment guide](deployment-reconciliation.md).

SQLite serializes writers. A short `BEGIN IMMEDIATE` transaction selects an eligible message, attaches a time-limited lease, and commits before an external service call. Schema constraints and transaction boundaries keep one delivery per recipient per request and one substantive reply per delivery. WAL lets readers continue while a write commits. It persists at the database level. Each Store connection waits up to ten seconds on contention and can still report a lock error. Base storage changes on measured writer contention, storage topology, or a need for cross-machine or multi-user authentication. See the [queue contract](queue.md) for transaction and recovery details.

## State and authority

| Component | Durable authority |
|---|---|
| Project registry | Registered project slugs and canonical workspace roots; the current directory or verified native workspace selects the tenant |
| Queue | Sender, recipient, original request ID, stage, timestamps and current lease; idempotency scoped by project and sender |
| Role bindings | Exact native chat, role and harness in a project; duplicate ownership and takeover with active work are rejected |
| Native wake adapter | Which notification was submitted to which chat; a wake signals that mailbox work is available |
| Skill manifest | Source content hash, installed hash and reviewed destination variant |
| Telegram conduit | Authorized bot and chat, external message ID, linked queue message, final recipient and ambiguous provider outcomes |

The coordinator and inbox are two distinct roles even when both are visible under the same native project window. A queued wake carries routing metadata so that a model reads the mailbox through the scoped tool. The caller never needs to paste the body of a peer message into another desktop composer's prompt. A delivery receipt proves the indicated stage only.

For replies, keep the shared `request_id` and each recipient's delivery ID from the send result. The origin processes replies through its usual mailbox tools, using `reply_to` to match each reply to a recipient delivery. If an assistant needs to wait, the in-session monitor and native wake worker report available messages while the database holds the durable state. Claiming grants a lease. Finishing records receipt, and a failed consumer leaves work available after lease expiry. The [native wake guide](native.md) covers notification deduplication and recovery when a submitted prompt has an uncertain outcome.

A Codex project can opt into the agent relay (`coordinator_wake: agent-relay` in `options.json`). The inbox agent then relays the coordinator wake inside its own turn through the native send-message-to-thread tool instead of waiting for `codex queue` to drain. The mailbox records that relay in the same wake ledger the dispatcher uses, so the fallback queue does not also fire; the ordinary queue path remains the fallback after a short grace window.

## Isolation and recovery checks

1. Create two project roots. Register separate coordinator/inbox pairs for the same harness in each. Use identical dedupe keys in both projects. Verify the resulting message IDs differ.
2. Stop one project route. Send it a message from a peer. Verify the stopped side has pending work, the running project advances, and rotation of the stopped pair leaves pickup stopped.
3. Lease a delivery, interrupt its consumer, and retry after expiry. Verify a stale lease cannot acknowledge the reclaimed message.
4. Rotate a role, then attempt an operation from the retired chat. Verify the original chat has lost its bound role. A native sidebar rename is a separate check.
5. Verify both directions for every intended pair, including a message started by each coordinator. Check exact correlation and the final origin inbox. For Telegram, verify a threaded reply in a shared bot returns to the same project and harness.
6. Simulate losing the provider response after external submission. Preserve the uncertain operation for reconciliation and inspect the actual destination before retrying. The bridge can deduplicate local queue operations but cannot impose exactly-once delivery on an external API that has accepted an unconfirmed call.
