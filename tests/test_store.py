from concurrent.futures import ThreadPoolExecutor

import pytest

from multi_harness_bridge.store import SCHEMA_VERSION, Store


def setup(path):
    store = Store(path)
    store.bind("alpha", "one", "coordinator", "c1")
    store.bind("alpha", "one", "inbox", "i1")
    return store


def test_project_isolation_and_deduplication(tmp_path):
    store = setup(tmp_path / "queue.db")
    first = store.send("alpha", "two", "one", "hello", "same")
    assert store.send("alpha", "two", "one", "hello", "same") == first
    with pytest.raises(ValueError, match="reused"):
        store.send("alpha", "two", "one", "changed body", "same")
    with pytest.raises(ValueError, match="reused"):
        store.send("alpha", "two", "other", "hello", "same")
    other = store.send("beta", "two", "one", "hello", "same")
    assert other["id"] != first["id"]
    assert store.claim("beta", "one", "inbox", "i1") is None
    assert store.claim("alpha", "one", "coordinator", "c1") is None
    job = store.claim("alpha", "one", "inbox", "i1")
    assert job["id"] == first["id"] and job["body"] == "hello"


def test_inbox_forwarding_and_coordinator_ack_are_durable(tmp_path):
    store = setup(tmp_path / "queue.db")
    message = store.send("alpha", "two", "one", "payload", "k")
    inbox_job = store.claim("alpha", "one", "inbox", "i1")
    assert store.finish(
        "alpha", "one", "inbox", "i1", message["id"], inbox_job["lease"]
    )
    assert store.claim("alpha", "one", "inbox", "i1") is None
    coordinator_job = store.claim("alpha", "one", "coordinator", "c1")
    assert coordinator_job["body"] == "payload"
    assert store.finish(
        "alpha", "one", "coordinator", "c1", message["id"], coordinator_job["lease"]
    )
    assert store.status("alpha", "one")["done"] == 1
    assert [e["event"] for e in store.trace("alpha",message["id"])["events"]] == [
        "enqueued","inbox-claimed","forwarded","coordinator-claimed","acknowledged"]


def test_concurrent_claims_expiry_and_retirement_guard(tmp_path):
    store = setup(tmp_path / "queue.db")
    for n in range(12):
        store.send("alpha", "two", "one", str(n), str(n))
    with ThreadPoolExecutor(max_workers=12) as pool:
        claims = list(
            pool.map(lambda _: store.claim("alpha", "one", "inbox", "i1"), range(12))
        )
    assert len({job["id"] for job in claims}) == 12
    first = claims[0]
    with pytest.raises(ValueError, match="active lease"):
        store.bind("alpha", "one", "inbox", "i2")
    with store._connect() as db:
        db.execute("UPDATE messages SET deadline=0 WHERE id=?", (first["id"],))
    reclaimed = store.claim("alpha", "one", "inbox", "i1")
    assert reclaimed["id"] == first["id"] and reclaimed["lease"] != first["lease"]
    assert not store.finish("alpha", "one", "inbox", "i1", first["id"], first["lease"])
    assert store.finish(
        "alpha", "one", "inbox", "i1", reclaimed["id"], reclaimed["lease"]
    )


def test_stop_blocks_new_inbound_and_outbound_but_allows_inflight_finish(tmp_path):
    store = setup(tmp_path / "queue.db")
    message = store.send("alpha", "two", "one", "payload", "k")
    job = store.claim("alpha", "one", "inbox", "i1")
    store.set_enabled("alpha", "one", False)
    assert store.claim("alpha", "one", "inbox", "i1") is None
    assert store.pending("alpha", "one", "inbox", "i1") is None
    with pytest.raises(ValueError, match="disabled"):
        store.send("alpha", "one", "two", "blocked", "out")
    assert store.finish("alpha", "one", "inbox", "i1", message["id"], job["lease"])
    assert store.status("alpha", "one")["enabled"] is False


def test_binding_defaults_enabled_only_when_complete(tmp_path):
    store = Store(tmp_path / "queue.db")
    store.bind("p", "h", "coordinator", "c")
    assert not store.status("p", "h")["enabled"]
    store.bind("p", "h", "inbox", "i")
    assert store.status("p", "h")["enabled"]


def test_explicit_stop_survives_binding_and_idempotent_bind_with_lease(tmp_path):
    store = Store(tmp_path / "queue.db")
    store.set_enabled("p", "h", False)
    store.bind("p", "h", "coordinator", "c")
    store.bind("p", "h", "inbox", "i")
    assert not store.status("p", "h")["enabled"]
    store.set_enabled("p", "h", True, session="c")
    message = store.send("p", "other", "h", "body", "key")
    job = store.claim("p", "h", "inbox", "i")
    assert store.bind("p", "h", "inbox", "i")["inbox"] == "i"
    assert store.finish("p", "h", "inbox", "i", message["id"], job["lease"])


def test_session_scope_reply_validation_trace_and_overview(tmp_path):
    store = setup(tmp_path / "queue.db")
    with pytest.raises(ValueError, match="already bound"):
        store.bind("beta", "one", "coordinator", "c1")
    original = store.send("p", "a", "b", "question", "q")
    reply = store.send("p", "b", "a", "answer", "a1", reply_to=original["id"])
    with pytest.raises(ValueError, match="reversed sender"):
        store.send("p", "other", "a", "bad", "bad", reply_to=original["id"])
    with pytest.raises(ValueError, match="reversed sender"):
        store.send("other-project", "b", "a", "answer", "a2", reply_to=original["id"])
    assert store.trace("p", original["id"])["stage"] == "inbox"
    assert store.trace("elsewhere", original["id"]) is None
    overview = store.pending_overview("p", limit=1)
    assert len(overview) == 1 and overview[0]["id"] == original["id"]
    assert overview[0]["created"] <= overview[0]["updated"]
    assert store.trace("p", reply["id"])["stage"] == "inbox"


def test_future_schema_is_rejected(tmp_path):
    import sqlite3

    path = tmp_path / "future.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="newer than supported"):
        Store(path)


def test_previous_schema_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE bindings (
                project TEXT NOT NULL, harness TEXT NOT NULL,
                role TEXT NOT NULL, session TEXT NOT NULL,
                PRIMARY KEY(project,harness,role), UNIQUE(project,harness,session));
            CREATE TABLE control (
                project TEXT NOT NULL, harness TEXT NOT NULL,
                enabled INTEGER NOT NULL, PRIMARY KEY(project,harness));
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, sender TEXT NOT NULL,
                recipient TEXT NOT NULL, body TEXT NOT NULL, key TEXT NOT NULL,
                reply_to TEXT, stage TEXT NOT NULL, lease TEXT, lease_role TEXT,
                lease_session TEXT, deadline REAL, attempts INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, UNIQUE(project,sender,key));
            PRAGMA user_version=1;
        """)
    Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "explicit" in {r[1] for r in db.execute("PRAGMA table_info(control)")}
        assert "updated" in {r[1] for r in db.execute("PRAGMA table_info(messages)")}


def test_orchestrator_stale_is_sticky_and_retries_cannot_re_elect(tmp_path):
    store = Store(tmp_path / "queue.db")
    for harness in ("codex", "claude"):
        store.bind("p", harness, "coordinator", f"{harness}-c")
        store.bind("p", harness, "inbox", f"{harness}-i")

    elected = store.take_coordinator("p", "codex", "codex-c", designate=True,
                                      request_id="turn-a")
    assert elected["orchestrator"]["status"] == "active"
    assert store.project_status("p")["orchestrator"]["harness"] == "codex"
    assert store.status("p", "codex")["outbound"] == 1
    store.take_coordinator("p", "claude", "claude-next", request_id="claude-plain")
    assert store.project_status("p")["orchestrator"]["status"] == "active"
    assert store.project_status("p")["orchestrator"]["harness"] == "codex"

    store.take_coordinator("p", "codex", "codex-next", request_id="turn-b")
    assert store.project_status("p")["orchestrator"]["status"] == "stale"
    with store._connect() as db:
        stale_notices = db.execute(
            "SELECT COUNT(*) FROM messages WHERE project='p' AND request_id IS NOT NULL "
            "AND body LIKE '%owner is stale%'"
        ).fetchone()[0]
    assert stale_notices == 1
    # Rebinding the former owner chat without an explicit election stays stale.
    store.take_coordinator("p", "codex", "codex-c", request_id="turn-c")
    assert store.project_status("p")["orchestrator"]["status"] == "stale"
    # A flagged A→B replacement records a direct transfer without a duplicate
    # stale notice for the old owner.
    store.take_coordinator("p", "codex", "codex-c", designate=True,
                           request_id="turn-c-orc")
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE project='p' AND request_id IS NOT NULL "
            "AND body LIKE '%owner is stale%'"
        ).fetchone()[0] == 1

    # A retry of the old explicit invocation cannot reclaim after another election.
    store.take_coordinator("p", "claude", "claude-next", designate=True,
                           request_id="turn-d")
    store.take_coordinator("p", "codex", "codex-c", designate=True,
                           request_id="turn-a")
    assert store.project_status("p")["orchestrator"]["harness"] == "claude"

    before = store.roles("p", "codex")
    cleared = store.clear_orchestrator("p")
    assert cleared["orchestrator"]["status"] == "cleared"
    assert store.project_status("p")["orchestrator"]["status"] == "cleared"
    assert store.roles("p", "codex") == before
    events = store.project_status("p")["orchestrator_events"]
    assert [event["event"] for event in events] == [
        "designated", "stale", "designated", "designated", "cleared"]


def test_orchestrator_side_tables_preserve_v4_and_track_old_client_rebind(tmp_path):
    import sqlite3

    path = tmp_path / "queue.db"
    store = Store(path)
    store.bind("p", "codex", "coordinator", "a")
    store.bind("p", "codex", "inbox", "i")
    store.take_coordinator("p", "codex", "a", designate=True)
    with sqlite3.connect(path) as db:
        # Simulate an old v4 process updating only the pre-existing bindings table.
        db.execute("UPDATE bindings SET session='b' WHERE project='p' AND harness='codex' AND role='coordinator'")
        db.execute("UPDATE bindings SET session='a' WHERE project='p' AND harness='codex' AND role='coordinator'")
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
    assert store.project_status("p")["orchestrator"]["status"] == "stale"


def test_orchestrator_transfer_serializes_and_failed_binding_rolls_back(tmp_path):
    store = Store(tmp_path / "queue.db")
    for harness in ("codex", "claude"):
        store.bind("p", harness, "coordinator", f"{harness}-c")
        store.bind("p", harness, "inbox", f"{harness}-i")
    store.take_coordinator("p", "codex", "codex-c", designate=True)
    with pytest.raises(ValueError, match="already bound"):
        store.take_coordinator("p", "claude", "claude-i", designate=True)
    assert store.project_status("p")["orchestrator"]["harness"] == "codex"
    assert store.roles("p", "claude") == {"coordinator": "claude-c", "inbox": "claude-i"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda harness: store.take_coordinator("p", harness, f"{harness}-c",
                                                    designate=True, request_id=f"transfer-{harness}"),
            ("claude", "codex"),
        ))
    status = store.project_status("p")
    assert status["orchestrator"]["status"] == "active"
    assert status["orchestrator"]["harness"] in {"codex", "claude"}
    assert len([item for item in results if item["orchestrator"]["status"] == "active"]) == 2


def test_project_status_persists_external_owner_staleness(tmp_path):
    from contextlib import contextmanager

    class Provider:
        current = "a"

        def handles(self, project, harness):
            return project == "p" and harness == "codex"

        @contextmanager
        def guard(self, project, *, exclusive=False):
            yield

        def roles(self, project, harness):
            return {"coordinator": self.current, "inbox": "i"}

        def enabled(self, project, harness):
            return True

    provider = Provider()
    store = Store(tmp_path / "queue.db", bindings_provider=provider)
    with pytest.raises(ValueError, match="current native coordinator"):
        store.take_coordinator("p", "codex", "not-current", designate=True,
                               request_id="rejected-election")
    assert provider.roles("p", "codex") == {"coordinator": "a", "inbox": "i"}
    assert store.project_status("p")["orchestrator"]["status"] == "never_designated"
    store.take_coordinator("p", "codex", "a", designate=True, request_id="election")
    provider.current = "b"
    assert store.project_status("p")["orchestrator"]["status"] == "stale"
    provider.current = "a"
    assert store.project_status("p")["orchestrator"]["status"] == "stale"


@pytest.mark.parametrize("harness", ["codex", "claude", "devin", "antigravity"])
def test_inbox_takeover_resumes_only_verified_route(tmp_path, harness):
    store = Store(tmp_path / "queue.db")
    store.set_enabled("alpha", harness, False)
    store.set_enabled("beta", harness, False)
    store.bind("alpha", harness, "inbox", "first", take=True)
    assert not store.status("alpha", harness)["enabled"]
    store.bind("alpha", harness, "coordinator", "co", take=True)
    assert store.status("alpha", harness)["enabled"]
    store.set_enabled("alpha", harness, False)
    store.bind("alpha", harness, "coordinator", "co2", take=True)
    with pytest.raises(ValueError):
        store.bind("alpha", harness, "inbox", "co2", take=True)
    assert not store.status("alpha", harness)["enabled"]
    store.bind("alpha", harness, "inbox", "next", take=True)
    assert store.status("alpha", harness)["enabled"]
    assert not store.status("beta", harness)["enabled"]
    store.set_enabled("alpha", harness, False)
    store.bind("alpha", harness, "inbox", "next", take=True)
    assert store.status("alpha", harness)["enabled"]


def test_plain_takeover_preserves_mail_and_other_projects_designation(tmp_path):
    store = Store(tmp_path / "queue.db")
    for project in ("orchard", "harbor"):
        for harness in ("codex", "claude", "devin", "antigravity"):
            store.bind(project, harness, "inbox", f"{project}-{harness}-inbox")
            store.take_coordinator(project, harness, f"{project}-{harness}")
        assert store.project_status(project)["orchestrator_events"] == []
        assert all(store.status(project, harness)["outbound"] == 0
                   for harness in store.project_status(project)["coordinators"])

    store.take_coordinator("orchard", "claude", "orchard-claude", designate=True)
    message = store.send("harbor", "devin", "codex", "Review ready", "review")
    store.take_coordinator("harbor", "codex", "harbor-next")
    for role, session in (("inbox", "harbor-codex-inbox"), ("coordinator", "harbor-next")):
        item = store.claim("harbor", "codex", role, session)
        assert item["id"] == message["id"]
        assert store.finish("harbor", "codex", role, session, item["id"], item["lease"])
    assert store.orchestrator_status("harbor")["status"] == "never_designated"
    assert store.orchestrator_status("orchard")["session"] == "orchard-claude"
