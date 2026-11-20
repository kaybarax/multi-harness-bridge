# Durable project queue

`multi_harness_bridge.store.Store` stores role bindings, enable state, and
messages in one SQLite database. Use a separate project name for each isolated
workspace; message keys are unique per project and sender. Reusing a key with
the same recipient, body, and reply reference returns the original ID. Reusing
it with changed content is an error. A reply reference must identify a message
in the same project with sender and recipient reversed.

```python
from multi_harness_bridge.store import Store

store = Store("bridge.sqlite3")
store.bind("project-a", "worker", "coordinator", "session-c")
store.bind("project-a", "worker", "inbox", "session-i")
message = store.send("project-a", "worker", "reviewer", "Please review", "task-17")
job = store.claim("project-a", "reviewer", "inbox", "reviewer-inbox-session")
store.finish("project-a", "reviewer", "inbox", "reviewer-inbox-session",
             job["id"], job["lease"])
```

The two roles belong to a harness. A newly bound harness is enabled once both
roles have sessions. `roles(project, harness)` returns `coordinator` and
`inbox`, each a session string or `None`. Replacing a role is rejected while
that role has an unexpired lease. A session can be bound to only one role and
project for a given harness. Session values are opaque host-provided IDs.
Binding both roles enables a new pair by default. An explicit stop or resume
persists across role changes; repeating an identical bind is idempotent.

Messages first enter the recipient harness's `inbox` stage. Finishing an inbox
lease durably forwards the message to the recipient's coordinator stage.
Finishing a coordinator lease acknowledges it by moving it to `done`. A claim
returns `id`, a random `lease`, and message fields including `body`; finishing
with a stale lease returns `False`. Leases expire after 30 seconds and become
claimable again, with an incremented `attempt` value. `pending(...)` reads the
next eligible record without leasing it and returns `None` when none is ready.

`set_enabled(project, harness, False)` stops both outbound sends from and
inbound claims to that harness. Existing leases can still finish, preserving
in-flight work. Pass `session=...` to `send` or `set_enabled` to require that
the caller is the harness's currently bound coordinator. Omitting it supports
trusted local operator code. `status(project, harness)` reports enable state
and inbox, coordinator, completed, and active lease counts. For handoff and
diagnostics, `pending_overview(project, harness=None, limit=100)` returns up to
1,000 unfinished message records with IDs, stage, and timestamps;
`trace(project, message_id)` returns one project's lifecycle metadata or
`None`. These methods are read-only and project-scoped.

The store uses SQLite WAL mode, full synchronous commits, transactions, and a
schema version guard. Write operations serialize across threads and processes.
Leases retry expired queue work. The native wake adapter separately records
successful and uncertain external delivery attempts; an uncertain external
send is never repeated automatically, while the queue record remains
claimable and can be reconciled by its assistant. Keep the database on a
filesystem that supports SQLite locking semantics.
