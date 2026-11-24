from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3

import pytest

from multi_harness_bridge import wake
from multi_harness_bridge.store import SCHEMA_VERSION, Store


def team(tmp_path):
    store = Store(tmp_path / "queue.db")
    for harness in ("claude", "codex", "devin", "antigravity"):
        for role in ("inbox", "coordinator"):
            store.bind("example-project", harness, role, f"{harness}-{role}")
    return store


def advance(store, message_id, harness, role, stage):
    session = f"{harness}-{role}"
    item = store.claim("example-project", harness, role, session, message_id=message_id)
    assert item["stage"] == stage
    assert store.finish("example-project", harness, role, session, message_id, item["lease"])
    assert not store.finish("example-project", harness, role, session, message_id, item["lease"])
    return item


@pytest.mark.parametrize(
    ("origin", "destinations"),
    [("devin", ["codex", "antigravity"]), ("codex", ["devin", "antigravity"]),
     ("antigravity", ["codex", "devin"]), ("claude", ["codex", "devin"])],
)
def test_each_delivery_and_reply_visits_both_native_inboxes(tmp_path, monkeypatch, origin, destinations):
    store = team(tmp_path)
    wake_calls = []

    def record_wake(harness, values, **kwargs):
        wake_calls.append((harness, values["role"], values["session"], values["notification_id"]))
        assert "arbitrary 🚪 free text" not in values["body"]

    monkeypatch.setattr(wake, "_default_wake", record_wake)
    monkeypatch.setenv("MHB_CLAUDE_DETACHED_WAKE", "1")
    monkeypatch.setattr("multi_harness_bridge.ag_wake.verify", lambda *_: None)
    monkeypatch.setattr("multi_harness_bridge.ag_sidecar.agentapi", lambda _: "/host/agentapi")

    message = "arbitrary 🚪 free text\nwith a second line"
    origin_session = f"{origin}-coordinator"
    request = store.send_fanout("example-project", origin, destinations, message, "work", session=origin_session)
    assert len(request["deliveries"]) == 2
    events = ["enqueued", "outbound-claimed", "routed", "inbox-claimed",
              "forwarded", "coordinator-claimed", "acknowledged"]

    def follow_route(message_id, sender, recipient, body, reply_to):
        assert store.pending("example-project", recipient, "inbox", f"{recipient}-inbox") is None
        assert store.pending("example-project", recipient, "coordinator", f"{recipient}-coordinator") is None
        assert store.pending("example-project", sender, "coordinator", f"{sender}-coordinator") is None
        for harness, role, stage in [(sender, "inbox", "outbound"), (recipient, "inbox", "inbox"),
                                     (recipient, "coordinator", "coordinator")]:
            session = f"{harness}-{role}"
            result = wake.wake_one(store, "example-project", harness, role, session, commands={})
            assert (result["id"], result["status"]) == (message_id, "sent")
            if harness == "antigravity" and "mechanism" in result:
                assert (result["mechanism"], result["session"]) == ("agentapi", session)
            assert wake.wake_one(store, "example-project", harness, role, session, commands={})["status"] == "sent"
            item = advance(store, message_id, harness, role, stage)
            assert (item["sender"], item["recipient"], item["body"]) == (sender, recipient, body)
            assert (item["request_id"], item["reply_to"]) == (request["request_id"], reply_to)
        assert [event["event"] for event in store.trace("example-project", message_id)["events"]] == events

    for delivery in request["deliveries"]:
        peer, delivery_id = delivery["recipient"], delivery["id"]
        with pytest.raises(ValueError, match="claim"):
            store.reply_free_text("example-project", peer, delivery_id, "received", "reply", session=f"{peer}-coordinator")
        follow_route(delivery_id, origin, peer, message, None)
        assert store.trace("example-project", delivery_id)["replies"] == []
    for delivery in request["deliveries"]:
        peer, delivery_id = delivery["recipient"], delivery["id"]
        reply = store.reply_free_text("example-project", peer, delivery_id, "received", "reply", session=f"{peer}-coordinator")
        assert reply["request_id"] == request["request_id"]
        assert store.trace("example-project", delivery_id)["replies"] == [reply["id"]]
        follow_route(reply["id"], peer, origin, "received", delivery_id)
    assert len(wake_calls) == len(set(wake_calls)) == 12


def test_partial_delivery_and_concurrent_retry_do_not_redeliver(tmp_path):
    store = team(tmp_path)
    store.set_enabled("example-project", "antigravity", False)
    store.set_enabled("elsewhere", "devin", True)
    args = ("example-project", "devin", ["codex", "antigravity"], "dry run", "same")

    def send():
        return store.send_fanout(*args, session="devin-coordinator")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: send(), range(12)))
    assert results == [results[0]] * 12
    deliveries = {item["recipient"]: item["id"] for item in results[0]["deliveries"]}
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: store.claim("example-project", "devin", "inbox", "devin-inbox"), range(8)))
    assert {item["id"] for item in claims if item} == set(deliveries.values())
    assert sum(item is not None for item in claims) == 2
    for item in claims:
        if item:
            assert store.finish("example-project", "devin", "inbox", "devin-inbox", item["id"], item["lease"])
    advance(store, deliveries["codex"], "codex", "inbox", "inbox")
    advance(store, deliveries["codex"], "codex", "coordinator", "coordinator")
    assert store.claim("example-project", "antigravity", "inbox", "antigravity-inbox") is None
    assert store.trace("example-project", deliveries["antigravity"])["stage"] == "inbox"
    assert store.send_fanout("example-project", "devin", ["antigravity", "codex"], "dry run", "same",
                             session="devin-coordinator") == results[0]
    assert store.pending("example-project", "codex", "inbox", "codex-inbox") is None
    with pytest.raises(ValueError, match="reused"):
        store.send_fanout("example-project", "devin", ["antigravity"], "dry run", "same",
                         session="devin-coordinator")
    store.set_enabled("example-project", "antigravity", True)
    advance(store, deliveries["antigravity"], "antigravity", "inbox", "inbox")
    advance(store, deliveries["antigravity"], "antigravity", "coordinator", "coordinator")
    assert store.trace("elsewhere", deliveries["codex"]) is None


def test_fence_retired_session_and_recover_origin_inbox_lease(tmp_path):
    store = team(tmp_path)
    request = store.send_fanout("example-project", "codex", ["devin"], "body", "key", session="codex-coordinator")
    delivery_id = request["deliveries"][0]["id"]
    claim = store.claim("example-project", "codex", "inbox", "codex-inbox")
    with pytest.raises(ValueError, match="active lease"):
        store.bind("example-project", "codex", "inbox", "new-inbox")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE messages SET deadline=0 WHERE id=?", (delivery_id,))
    store.bind("example-project", "codex", "inbox", "new-inbox")
    store.bind("example-project", "codex", "coordinator", "new-coordinator")
    assert store.claim("example-project", "codex", "inbox", "codex-inbox") is None
    with pytest.raises(ValueError, match="current coordinator"):
        store.send_fanout("example-project", "codex", ["devin"], "body", "key", session="codex-coordinator")
    reclaimed = store.claim("example-project", "codex", "inbox", "new-inbox")
    assert (reclaimed["id"], reclaimed["attempt"]) == (delivery_id, 2)
    assert not store.finish("example-project", "codex", "inbox", "codex-inbox", delivery_id, claim["lease"])
    store.set_enabled("example-project", "codex", False)
    assert store.finish("example-project", "codex", "inbox", "new-inbox", delivery_id, reclaimed["lease"])
    assert store.pending("example-project", "devin", "inbox", "devin-inbox")["id"] == delivery_id


def test_free_text_requires_bound_coordinator_and_isolated_complete_destination(tmp_path):
    store = team(tmp_path)
    store.bind("elsewhere", "worker", "coordinator", "worker-coordinator")
    store.bind("elsewhere", "worker", "inbox", "worker-inbox")
    with pytest.raises(ValueError, match="this project"):
        store.send_fanout("example-project", "codex", ["devin", "worker"], "body", "key", session="codex-coordinator")
    assert store.pending_overview("example-project") == []
    for recipients in (["codex"], ["devin", "devin"], ["telegram"], ["pma"]):
        with pytest.raises(ValueError):
            store.send_fanout("example-project", "codex", recipients, "body", "key", session="codex-coordinator")
    with pytest.raises(ValueError, match="current coordinator"):
        store.send_fanout("example-project", "codex", ["devin"], "body", "key", session="codex-inbox")


def test_reply_is_distinct_from_receipt_and_idempotent(tmp_path):
    store = team(tmp_path)
    request = store.send_fanout("example-project", "codex", ["devin"], "body", "key", session="codex-coordinator")
    delivery_id = request["deliveries"][0]["id"]
    advance(store, delivery_id, "codex", "inbox", "outbound")
    advance(store, delivery_id, "devin", "inbox", "inbox")
    claim = store.claim("example-project", "devin", "coordinator", "devin-coordinator")
    with pytest.raises(ValueError, match="delivery to this harness"):
        store.reply_free_text("example-project", "antigravity", delivery_id, "received", "r", session="antigravity-coordinator")
    with pytest.raises(ValueError, match="reversed sender"):
        store.send("example-project", "devin", "codex", "bypass", "b", reply_to=delivery_id, session="devin-coordinator")

    def respond(_):
        return store.reply_free_text("example-project", "devin", delivery_id, "received", "r", session="devin-coordinator")

    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(respond, range(12)))
    assert replies == [replies[0]] * 12
    assert store.finish("example-project", "devin", "coordinator", "devin-coordinator", delivery_id, claim["lease"])
    with pytest.raises(ValueError, match="already has a reply"):
        store.reply_free_text("example-project", "devin", delivery_id, "different", "r", session="devin-coordinator")
    assert store.trace("example-project", delivery_id)["replies"] == [replies[0]["id"]]


def test_migrate_v3_with_active_lease_history_and_stop(tmp_path):
    import time

    path = tmp_path / "legacy-queue.db"
    original_deadline = time.time() + 120
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE bindings (
                project TEXT NOT NULL, harness TEXT NOT NULL, role TEXT NOT NULL,
                session TEXT NOT NULL, PRIMARY KEY(project,harness,role),
                UNIQUE(project,harness,session));
            CREATE TABLE control (
                project TEXT NOT NULL, harness TEXT NOT NULL, enabled INTEGER NOT NULL,
                explicit INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(project,harness));
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, project TEXT NOT NULL,
                sender TEXT NOT NULL, recipient TEXT NOT NULL,
                body TEXT NOT NULL, key TEXT NOT NULL, reply_to TEXT,
                stage TEXT NOT NULL CHECK(stage IN ('inbox','coordinator','done')),
                lease TEXT, lease_role TEXT, lease_session TEXT,
                deadline REAL, attempts INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(project,sender,key));
            CREATE INDEX message_ready ON messages(project,recipient,stage,created);
            CREATE INDEX replies ON messages(project,reply_to);
            CREATE TABLE delivery_events (
                message_id TEXT NOT NULL, at REAL NOT NULL, event TEXT NOT NULL,
                attempt INTEGER NOT NULL, PRIMARY KEY(message_id,event,attempt));
            CREATE TRIGGER record_enqueue AFTER INSERT ON messages BEGIN
                INSERT INTO delivery_events VALUES(NEW.id,NEW.created,'enqueued',0);
            END;
            CREATE TRIGGER record_delivery AFTER UPDATE ON messages
                WHEN OLD.attempts<>NEW.attempts BEGIN
                INSERT INTO delivery_events VALUES(NEW.id,NEW.updated,'inbox-claimed',NEW.attempts);
            END;
            PRAGMA user_version=3;
            INSERT INTO control VALUES('example-project','codex',0,1);
            INSERT INTO bindings VALUES('example-project','codex','inbox','codex-inbox');
            INSERT INTO bindings VALUES('example-project','codex','coordinator','codex-coordinator');
            INSERT INTO messages (id,project,sender,recipient,body,key,stage,created,updated)
                VALUES('legacy','example-project','devin','codex','old body','old key','inbox',1,1);
            INSERT INTO messages (id,project,sender,recipient,body,key,stage,created,updated)
                VALUES('unrelated','other','devin','codex','unrelated','old key','done',2,2);
        """)
        db.execute("""UPDATE messages SET lease='unexpired',lease_role='inbox',lease_session='codex-inbox',
                   deadline=?,attempts=1,updated=3 WHERE id='legacy'""", (original_deadline,))
    with ThreadPoolExecutor(max_workers=6) as pool:
        stores = list(pool.map(lambda _: Store(path), range(6)))
    store = stores[0]
    assert store.status("example-project", "codex") == {
        "enabled": False, "outbound": 0, "inbox": 1, "coordinator": 0, "done": 0, "leased": 1,
    }
    assert store.trace("example-project", "legacy")["deadline"] == original_deadline
    assert store.roles("example-project", "codex") == {"inbox": "codex-inbox", "coordinator": "codex-coordinator"}
    assert store.send("example-project", "devin", "codex", "old body", "old key") == {"id": "legacy"}
    assert store.finish("example-project", "codex", "inbox", "codex-inbox", "legacy", "unexpired")
    assert [event["event"] for event in store.trace("example-project", "legacy")["events"]] == [
        "enqueued", "inbox-claimed", "forwarded",
    ]
    assert store.trace("other", "unrelated")["stage"] == "done"
    assert not store.status("example-project", "codex")["enabled"]


def test_external_bindings_are_authoritative_and_fence_held_leases(tmp_path):
    from contextlib import contextmanager

    class Provider:
        guarded = False
        owners = {
            "codex": {"inbox": "current-codex-inbox", "coordinator": "current-codex-coordinator"},
            "devin": {"inbox": "current-devin-inbox", "coordinator": "current-devin-coordinator"},
        }
        stopped = set()

        def handles(self, project, harness):
            return project == "example-project" and harness in self.owners

        @contextmanager
        def guard(self, project):
            assert not self.guarded
            self.guarded = True
            try:
                yield
            finally:
                self.guarded = False

        def roles(self, project, harness):
            assert self.guarded
            return self.owners[harness]

        def enabled(self, project, harness):
            assert self.guarded
            return harness not in self.stopped

    provider = Provider()
    store = Store(tmp_path / "queue.db", bindings_provider=provider)
    with pytest.raises(ValueError, match="native role picker"):
        store.bind("example-project", "codex", "inbox", "shadow")
    with pytest.raises(ValueError, match="native stop"):
        store.set_enabled("example-project", "codex", False)
    assert store.bind("other", "codex", "coordinator", "unrelated")["coordinator"] == "unrelated"
    args = "example-project", "codex", ["devin"], "message", "key"
    with pytest.raises(ValueError, match="current coordinator"):
        store.send_fanout(*args, session="retired")
    request = store.send_fanout(*args, session="current-codex-coordinator")
    claim = store.claim("example-project", "codex", "inbox", "current-codex-inbox")
    provider.owners["codex"]["inbox"] = "replacement"
    assert not store.finish("example-project", "codex", "inbox", "current-codex-inbox", claim["id"], claim["lease"])
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE messages SET deadline=0 WHERE id=?", (claim["id"],))
    replacement = store.claim("example-project", "codex", "inbox", "replacement")
    assert (replacement["id"], replacement["request_id"], replacement["stage"]) == (
        claim["id"], request["request_id"], "outbound",
    )
    provider.stopped.add("devin")
    assert store.finish("example-project", "codex", "inbox", "replacement", replacement["id"], replacement["lease"])
    assert not store.status("example-project", "devin")["enabled"]
    assert store.pending("example-project", "devin", "inbox", "current-devin-inbox") is None
    provider.stopped.remove("devin")
    assert store.pending("example-project", "devin", "inbox", "current-devin-inbox")["id"] == claim["id"]


def test_concurrent_first_openers_initialize_one_queue(tmp_path):
    from threading import Barrier

    path = tmp_path / "new.db"
    together = Barrier(12)

    def open_and_send(peer):
        together.wait(timeout=10)
        store = Store(path)
        return store.send("example-project", "codex", "devin", "body", "one")

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(open_and_send, range(12)))
    assert results == [results[0]] * 12
    assert [row["id"] for row in Store(path).pending_overview("example-project")] == [results[0]["id"]]


@pytest.mark.parametrize("version", [0, SCHEMA_VERSION])
def test_foreign_mailbox_is_refused_without_changing_journal_or_records(tmp_path, version):
    path = tmp_path / "foreign.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE messages (id TEXT PRIMARY KEY, sender TEXT, recipient TEXT, kind TEXT, body TEXT);
            INSERT INTO messages VALUES('queued','devin_local','codex','message','legacy work');
        """)
        db.execute(f"PRAGMA user_version={version}")
    with pytest.raises(ValueError, match="different mailbox format"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (version,)
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert db.execute("SELECT id,kind,body FROM messages").fetchone() == ("queued", "message", "legacy work")


def test_existing_portable_mailbox_restores_wal_and_preserves_active_lease(tmp_path):
    store = team(tmp_path)
    request = store.send_fanout("example-project", "codex", ["devin"], "body", "key", session="codex-coordinator")
    delivery_id = request["deliveries"][0]["id"]
    claim = store.claim("example-project", "codex", "inbox", "codex-inbox", message_id=delivery_id)
    original = store.trace("example-project", delivery_id)
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)

    reopened = Store(store.path)
    with closing(reopened._connect()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
    assert reopened.trace("example-project", delivery_id) == original
    assert reopened.finish("example-project", "codex", "inbox", "codex-inbox", delivery_id, claim["lease"])
