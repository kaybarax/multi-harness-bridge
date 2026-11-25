"""Coordinator pull mode: forwarding, listener, non-navigating nudges and the fallback."""
import io
import json
import os
import sqlite3
import time

import pytest

from multi_harness_bridge import config, pickup, wake
from multi_harness_bridge.config import register
from multi_harness_bridge.store import Store


def _store(tmp_path, harnesses=("codex", "claude", "devin")):
    store = Store(tmp_path / "bridge.sqlite3")
    for harness in harnesses:
        for role in ("coordinator", "inbox"):
            store.bind("sample", harness, role, f"{harness}-{role}")
    return store


def _at_coordinator(store, sender, recipient, key="k", body="secret body"):
    message = store.send("sample", sender, recipient, body, key)
    claimed = store.claim("sample", recipient, "inbox", f"{recipient}-inbox", message_id=message["id"])
    store.finish("sample", recipient, "inbox", f"{recipient}-inbox", message["id"], claimed["lease"])
    return message["id"]


# Settings --------------------------------------------------------------------

def test_defaults_are_pull_for_capable_hosts_and_push_elsewhere(tmp_path):
    assert pickup.settings(tmp_path, "sample", "codex")["coordinator_pickup"] == "pull"
    assert pickup.settings(tmp_path, "sample", "devin")["fallback_grace_seconds"] == 180
    assert pickup.mode(tmp_path, "sample", "antigravity") == "push"
    config.set_project_option(tmp_path, "sample", "antigravity", "coordinator_pickup", "pull")
    assert pickup.mode(tmp_path, "sample", "antigravity") == "push"
    config.set_project_option(tmp_path, "sample", "codex", "coordinator_pickup", "push")
    assert pickup.mode(tmp_path, "sample", "codex") == "push"


def test_invalid_numbers_fall_back_to_defaults(tmp_path):
    config.set_project_option(tmp_path, "sample", "codex", "fallback_grace_seconds", "5")
    config.set_project_option(tmp_path, "sample", "codex", "digest_minutes", "abc")
    opts = pickup.settings(tmp_path, "sample", "codex")
    assert opts["fallback_grace_seconds"] == 180 and opts["digest_minutes"] == 30


# Forwarding ------------------------------------------------------------------

def test_forward_pull_moves_both_inbox_stages_without_a_lease(tmp_path):
    store = _store(tmp_path)
    request = store.send_fanout("sample", "codex", ["devin"], "hello", "req", session="codex-coordinator")
    delivery = request["deliveries"][0]["id"]
    assert store.forward_pull("sample", "codex") == [{"id": delivery, "stage": "inbox"}]
    assert store.forward_pull("sample", "devin") == [{"id": delivery, "stage": "coordinator"}]
    trace = store.trace("sample", delivery)
    assert trace["stage"] == "coordinator" and trace["attempts"] == 0
    assert [e["event"] for e in trace["events"]] == ["enqueued", "routed", "forwarded"]
    # A bridge handover is never blocked: no lease is held.
    store.bind("sample", "devin", "inbox", "devin-inbox-2")


def test_forward_pull_respects_stop_binding_and_active_leases(tmp_path):
    store = _store(tmp_path)
    store.set_enabled("sample", "codex", False)
    store.send("sample", "devin", "codex", "x", "a")
    assert store.forward_pull("sample", "codex") == []
    store.set_enabled("sample", "codex", True)
    leased = store.claim("sample", "codex", "inbox", "codex-inbox")
    assert store.forward_pull("sample", "codex") == []
    assert store.finish("sample", "codex", "inbox", "codex-inbox", leased["id"], leased["lease"])
    unbound = Store(tmp_path / "other.sqlite3")
    unbound.bind("sample", "codex", "coordinator", "only")
    unbound.send("sample", "devin", "codex", "x", "b")
    assert unbound.forward_pull("sample", "codex") == []


# Listener --------------------------------------------------------------------

def _listen(tmp_path, monkeypatch, store, cycles, clock, **kwargs):
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    cfg = register("sample", [root], tmp_path)
    monkeypatch.chdir(root)
    out = io.StringIO()
    code = pickup.listen(cfg, store, "sample", "claude", "claude-coordinator", output=out,
                         wait=lambda _: None, clock=clock, max_cycles=cycles, **kwargs)
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


def test_listener_hints_requests_at_once_batches_replies_and_never_prints_bodies(tmp_path, monkeypatch):
    store = _store(tmp_path)
    request = _at_coordinator(store, "codex", "claude", "r1", body="private request")
    original = store.send("sample", "claude", "codex", "ask", "q")
    reply = store.send("sample", "codex", "claude", "private reply", "a", reply_to=original["id"])
    claimed = store.claim("sample", "claude", "inbox", "claude-inbox", message_id=reply["id"])
    store.finish("sample", "claude", "inbox", "claude-inbox", reply["id"], claimed["lease"])
    now = [1000.0]

    def clock():
        now[0] += 10
        return now[0]
    code, lines = _listen(tmp_path, monkeypatch, store, 4, clock)
    assert code == 0
    assert [m["id"] for m in lines[0]["messages"]] == [request]
    assert lines[0]["messages"][0]["kind"] == "request"
    assert [m["id"] for m in lines[1]["messages"]] == [reply["id"]]
    assert lines[1]["messages"][0]["kind"] == "reply"
    assert "private" not in json.dumps(lines)


def test_listener_rehints_unclaimed_mail_after_the_timeout(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _at_coordinator(store, "codex", "claude")
    now = [0.0]

    def clock():
        now[0] += 50
        return now[0]
    _, lines = _listen(tmp_path, monkeypatch, store, 5, clock, rehint_seconds=120)
    assert len(lines) == 2  # t=50 and t=200; t=100,150 are inside the timeout


def test_listener_exits_unbound_on_handover_and_skill_must_not_rearm(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.bind("sample", "claude", "coordinator", "replacement")
    code, lines = _listen(tmp_path, monkeypatch, store, 3, time.time)
    assert code == pickup.LISTENER_EXIT_UNBOUND and lines == []


def test_bridge_listener_prints_only_digest_lines(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _at_coordinator(store, "codex", "claude")
    root = tmp_path / "ws"
    root.mkdir()
    cfg = register("sample", [root], tmp_path)
    monkeypatch.chdir(root)
    marker = tmp_path / "pickup" / "digest-sample-claude.json"
    calls = []

    def wait(_):
        calls.append(1)
        if len(calls) == 1:
            marker.parent.mkdir(exist_ok=True)
            marker.write_text(json.dumps({"seq": 1}))
    out = io.StringIO()
    pickup.listen(cfg, store, "sample", "claude", "claude-inbox", role="inbox", output=out,
                  wait=wait, max_cycles=3)
    assert [json.loads(line) for line in out.getvalue().splitlines()] == [
        {"mhb": "digest", "project": "sample", "harness": "claude", "seq": 1}]


# Dispatcher under pull ----------------------------------------------------------

def test_pull_never_wakes_the_bridge_chat(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.send("sample", "devin", "codex", "x", "k")
    monkeypatch.setattr(wake, "_default_wake", lambda *a, **k: pytest.fail("bridge chat woken"))
    assert wake.wake_one(store, "sample", "codex", "inbox", "codex-inbox", commands={}) is None


def test_first_codex_nudge_queues_without_opening(tmp_path, monkeypatch):
    store = _store(tmp_path)
    message_id = _at_coordinator(store, "devin", "codex")
    runs = []
    monkeypatch.setattr(wake.shutil, "which", lambda name: "/bin/codex")
    monkeypatch.setattr(wake, "_run", lambda argv, timeout=8: runs.append(argv) or type("R", (), {"returncode": 0})())
    result = wake.wake_one(store, "sample", "codex", "coordinator", "codex-coordinator", commands={})
    assert result == {"id": message_id, "status": "sent", "pickup": "pull"}
    assert [argv[1] for argv in runs] == ["queue"]  # no `open codex://...`


class _FakeCodex(pickup.CodexPickup):
    state = "cold"
    opened = []

    def __init__(self):
        pass

    def check(self, session, notification_id, grace_seconds):
        return pickup.ColdCheck(self.state, "fake")

    def fallback(self, session):
        _FakeCodex.opened.append(session)


def _aged_attempt(store, message_id, age):
    with sqlite3.connect(store.path) as db:
        old = time.time() - age
        db.execute("UPDATE mhb_wake_attempts SET updated_at=?,started_at=? WHERE notification_id=?",
                   (old, old, message_id))
        db.execute("UPDATE mhb_wake_sessions SET updated_at=? WHERE notification_id=?", (old, message_id))
        db.execute("UPDATE messages SET updated=? WHERE id=?", (old, message_id))


@pytest.fixture
def fake_codex(monkeypatch):
    _FakeCodex.opened = []
    _FakeCodex.state = "cold"
    monkeypatch.setitem(pickup._ADAPTERS, "codex", _FakeCodex)
    monkeypatch.setattr(wake.shutil, "which", lambda name: "/bin/codex")
    runs = []
    monkeypatch.setattr(wake, "_run", lambda argv, timeout=8: runs.append(argv) or type("R", (), {"returncode": 0})())
    return runs


def test_busy_or_unreadable_codex_chat_is_held_and_never_opened(tmp_path, fake_codex):
    store = _store(tmp_path)
    message_id = _at_coordinator(store, "devin", "codex")
    wake.wake_one(store, "sample", "codex", "coordinator", "codex-coordinator", commands={})
    _aged_attempt(store, message_id, 400)
    for state in ("busy", "unknown"):
        _FakeCodex.state = state
        result = wake.wake_one(store, "sample", "codex", "coordinator", "codex-coordinator", commands={})
        assert result["status"] == "held" and result["reason"] == f"pull-{state}"
    assert _FakeCodex.opened == [] and len(fake_codex) == 1
    events = [e["event"] for e in wake.pickup_events(store, "sample")]
    assert events == ["held-busy", "fallback-unavailable"]


def test_cold_codex_chat_is_opened_once_after_grace_and_claimed_exactly_once(tmp_path, fake_codex):
    store = _store(tmp_path)
    message_id = _at_coordinator(store, "devin", "codex")
    wake.wake_one(store, "sample", "codex", "coordinator", "codex-coordinator", commands={})
    _aged_attempt(store, message_id, 100)  # inside the recovery window: nothing new happens
    wake.wake_one(store, "sample", "codex", "coordinator", "codex-coordinator", commands={})
    assert _FakeCodex.opened == [] and len(fake_codex) == 1
    _aged_attempt(store, message_id, 400)
    result = wake.wake_one(store, "sample", "codex", "coordinator", "codex-coordinator", commands={})
    assert result["fallback"] == "open" and _FakeCodex.opened == ["codex-coordinator"]
    assert len(fake_codex) == 1  # the fallback opens; it does not queue a second hint
    _aged_attempt(store, message_id, 400)
    again = wake.wake_one(store, "sample", "codex", "coordinator", "codex-coordinator", commands={})
    assert again["status"] == "held" and _FakeCodex.opened == ["codex-coordinator"]
    first = store.claim("sample", "codex", "coordinator", "codex-coordinator", message_id=message_id)
    assert first and store.claim("sample", "codex", "coordinator", "codex-coordinator",
                                 message_id=message_id) is None


def test_consumed_hint_is_rehinted_without_opening(tmp_path, fake_codex):
    store = _store(tmp_path)
    message_id = _at_coordinator(store, "devin", "codex")
    wake.wake_one(store, "sample", "codex", "coordinator", "codex-coordinator", commands={})
    _aged_attempt(store, message_id, 400)
    _FakeCodex.state = "consumed"
    result = wake.wake_one(store, "sample", "codex", "coordinator", "codex-coordinator", commands={})
    assert result["status"] == "sent" and _FakeCodex.opened == []
    assert [argv[1] for argv in fake_codex] == ["queue", "queue"]


def test_fallback_never_setting_holds_cold_chat(tmp_path, fake_codex):
    store = _store(tmp_path)
    config.set_project_option(tmp_path, "sample", "codex", "fallback_open", "never")
    message_id = _at_coordinator(store, "devin", "codex")
    wake.wake_one(store, "sample", "codex", "coordinator", "codex-coordinator", commands={})
    _aged_attempt(store, message_id, 400)
    assert wake.wake_one(store, "sample", "codex", "coordinator", "codex-coordinator",
                         commands={})["status"] == "held"
    assert _FakeCodex.opened == []


def test_push_dispatcher_skips_pull_harnesses_and_watcher_pass_skips_push(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    cfg = register("sample", [root], tmp_path)
    store = config.open_store(cfg)
    for harness in ("codex", "devin"):
        for role in ("coordinator", "inbox"):
            store.bind("sample", harness, role, f"{harness}-{role}")
    config.set_project_option(tmp_path, "sample", "devin", "coordinator_pickup", "push")
    monkeypatch.chdir(root)
    monkeypatch.setattr(wake, "wake_one", lambda s, p, h, r, sess: {"status": "seen", "h": h})
    push = {r["harness"] for r in wake.cycle("sample", pickup_mode="push", cfg=cfg, store=store)}
    pull = {r["harness"] for r in wake.cycle("sample", pickup_mode="pull", cfg=cfg, store=store)}
    assert push == {"devin"} and pull == {"codex"}


def test_codex_transcript_check(tmp_path):
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    transcript = home / "sessions" / "t.jsonl"
    transcript.write_text('{"x":"nothing yet"}\n')
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT)")
        db.execute("INSERT INTO threads VALUES('thread',?)", (str(transcript),))
    port = pickup.CodexPickup(codex_home=home)
    assert port.check("thread", "abc", 60).state == "busy"
    os.utime(transcript, (time.time() - 600, time.time() - 600))
    assert port.check("thread", "abc", 60).state == "cold"
    transcript.write_text('{"text":"notification abc pending"}\n')
    assert port.check("thread", "abc", 60).state == "consumed"
    assert port.check("missing", "abc", 60).state == "unknown"


def test_pickup_command_is_the_attachment_point_for_external_role_tools(tmp_path, monkeypatch):
    from multi_harness_bridge import cli
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("MHB_HOME", str(tmp_path / "state"))
    register("sample", [root], tmp_path / "state")
    store = config.open_store(config.load())
    store.bind("sample", "claude", "coordinator", "chat-1")
    store.bind("sample", "claude", "inbox", "chat-2")
    args = cli.build_parser().parse_args(["pickup", "sample", "claude", "coordinator", "--no-start"])
    report = cli.run(args)
    assert report["session"] == "chat-1" and report["coordinator_pickup"] == "pull"
    assert report["listener"]["command"].endswith("listen sample claude coordinator chat-1")
    assert report["watcher"]["status"] == "stopped"
