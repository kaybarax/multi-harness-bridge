# Durable project queue

`multi_harness_bridge.store.Store` stores role bindings, enable state, and
messages in a SQLite queue. Use a separate project name for each isolated
workspace.

```python
from multi_harness_bridge.store import Store

store = Store("bridge.sqlite3")
store.bind("project-a", "worker", "coordinator", "session-c")
store.bind("project-a", "worker", "inbox", "session-i")
store.bind("project-a", "reviewer", "coordinator", "reviewer-coordinator-session")
store.bind("project-a", "reviewer", "inbox", "reviewer-inbox-session")
request = store.send_fanout("project-a", "worker", ["reviewer"], "Please review",
                            "task-17", session="session-c")
delivery = request["deliveries"][0]
job = store.claim("project-a", "worker", "inbox", "session-i", message_id=delivery["id"])
store.finish("project-a", "worker", "inbox", "session-i", job["id"], job["lease"])
```

The two roles belong to a harness. A newly bound harness is enabled once both
roles have sessions. `roles(project, harness)` returns `coordinator` and
`inbox`, each a session string or `None`. Replacing a role is rejected while
that role has an unexpired lease. A session can be bound to only one role and
project for a given harness. Session values are opaque host-provided IDs.
Binding both roles enables a new pair by default. An ordinary binding update
preserves an explicit stop or resume. Taking the inbox restores automatic
pickup once the route has both roles. An ordinary identical bind is idempotent.

Each free-text send requires the current coordinator session and one or more
distinct recipient harnesses with both roles bound in the project. A request
has a shared `request_id` and one stable delivery ID per recipient. Retrying
with the same project, sender, key, body, and recipient set returns those
original IDs. Reusing the request key with a changed body or recipient set
fails. Telegram and PMA messages use their existing route.

A delivery starts at the sender's inbox in stage `outbound`. Finishing that
lease routes the delivery to the destination's `inbox` stage. The destination
inbox finishes its lease to hand off to its `coordinator` stage. The destination
coordinator finishes its lease to acknowledge receipt and move the delivery
to `done`. Each step acts on the same delivery ID. `trace` records the inbox
claims, routing, forwarding, and coordinator acknowledgement.

The destination coordinator can call `reply_free_text(..., reply_to=delivery_id,
body=..., key=..., session=...)` once it has claimed the delivery. It can also
reply after acknowledging receipt. The reply has a new ID, carries the original
`request_id`, and uses the same stages in the reverse direction. An exact reply
retry returns the existing reply ID. An acknowledgement changes the delivery
stage and creates no substantive reply.

A stop blocks new sends by the stopped harness and new claims at its roles.
A stopped recipient's copy waits while other copies can proceed. Already
issued leases can finish as long as the session still owns the role. Leases
expire after 30 seconds. An expired lease can be reclaimed with an incremented
attempt number, and its old token can no longer finish the record.

`pending(...)` reads the next eligible record without leasing it. `status`
counts records at the harness currently responsible for the stage.
`pending_overview(project, harness=None, limit=100)` returns bounded metadata
for unfinished messages. `trace(project, message_id)` returns lifecycle
metadata and correlated reply IDs for one record in the requested project.

The existing `send` API enters the recipient inbox directly. Its keys remain
unique per project and sender. Reusing a key with the same recipient, body,
and reply reference returns the original ID. Its reply references identify
messages on that route in the same project with sender and recipient reversed.
Passing `session` to `send` or `set_enabled` checks that the caller is the
current coordinator. Trusted local operator code can omit it.

Schema v4 upgrades the portable queue in place. It keeps existing bindings,
stops, message IDs, leases, and delivery history. Deploy all portable queue
processes on the same supported schema. Managed native routes can use a separate
authoritative binding provider with the portable queue. The provider supplies
current owners and pickup state for those routes. Use their native controls
for handoff and stop/resume. A retired session loses claim and finish access.
Its held record becomes available to the current owner when the lease expires.

The store uses SQLite WAL mode, full synchronous commits, transactions, and a
schema version guard. At startup, a recognized portable database in another
journal mode is switched back to WAL. Each Store connection has a ten-second
busy timeout. Write operations serialize across threads and processes.
Leases let the bound receiver reclaim expired queue work. The native wake
dispatcher records attempts separately and can retry a stale ID-only notice
when its exact queue stage remains pending and unleased. A current lease is
required to advance a queue stage. Keep the database on a
filesystem that supports SQLite locking semantics.
