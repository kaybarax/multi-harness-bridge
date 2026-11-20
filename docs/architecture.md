# Design and failure boundaries

## Why SQLite

The bridge is designed for concurrent local processes on one user's machine. SQLite serializes writers. The bridge uses a short immediate transaction to select one eligible message, attach a time-limited random lease and commit before invoking any external service. WAL allows readers to continue while the queue changes. A failed consumer can be retried after lease expiry. See the [queue contract](queue.md) for transaction and state details.

A Postgres installation and row-level skip-locked queries are unnecessary for this local deployment. Aggregate traffic, shared-network disk use, multi-user authentication or cross-machine delivery would require a new deployment and storage design. A claim can complete well before the native model starts reasoning. A four-second polling interval is a pickup target, not a promise about total model or Telegram latency.

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

## Isolation and recovery checks

1. Create two project roots. Register separate coordinator/inbox pairs for the same harness in each. Use identical dedupe keys in both projects. Verify the resulting message IDs differ.
2. Stop one project route. Send it a message from a peer. Verify the stopped side has pending work, the running project advances, and rotation of the stopped pair leaves pickup stopped.
3. Lease a delivery, interrupt its consumer, and retry after expiry. Verify a stale lease cannot acknowledge the reclaimed message.
4. Rotate a role, then attempt an operation from the retired chat. Verify the original chat has lost its bound role. A native sidebar rename is a separate check.
5. Verify both directions for every intended pair, including a message started by each coordinator. Check exact correlation and the final origin inbox. For Telegram, verify a threaded reply in a shared bot returns to the same project and harness.
6. Simulate losing the provider response after external submission. Preserve the uncertain operation for reconciliation and inspect the actual destination before retrying. The bridge can deduplicate local queue operations but cannot impose exactly-once delivery on an external API that has accepted an unconfirmed call.
