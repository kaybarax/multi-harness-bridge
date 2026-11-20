from concurrent.futures import ThreadPoolExecutor

import pytest

from multi_harness_bridge.store import Store


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
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "explicit" in {r[1] for r in db.execute("PRAGMA table_info(control)")}
        assert "updated" in {r[1] for r in db.execute("PRAGMA table_info(messages)")}
