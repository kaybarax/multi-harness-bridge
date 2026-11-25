"""Shared watcher: single instance, health, forwarding pass, feed, digest and alerts."""
import json
import multiprocessing
import os
import time

from multi_harness_bridge import config, pickup, watcher
from multi_harness_bridge.config import register


def _project(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    cfg = register("sample", [root], tmp_path / "state")
    store = config.open_store(cfg)
    for harness in ("codex", "claude", "devin"):
        for role in ("coordinator", "inbox"):
            store.bind("sample", harness, role, f"{harness}-{role}")
    monkeypatch.chdir(root)
    return cfg, store


def test_only_one_lock_holder_and_a_leftover_file_never_blocks(tmp_path):
    path = watcher.paths(tmp_path, "sample")["lock"]
    path.parent.mkdir(parents=True)
    path.write_text("left by a dead watcher")
    first, second = watcher.Lock(path), watcher.Lock(path)
    assert first.acquire() and not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def _hold(path, ready, done):
    lock = watcher.Lock(path)
    ready.put(lock.acquire())
    done.wait(5)


def test_simultaneous_starts_produce_one_watcher(tmp_path):
    path = watcher.paths(tmp_path, "sample")["lock"]
    ctx = multiprocessing.get_context("spawn")
    ready, done = ctx.Queue(), ctx.Event()
    procs = [ctx.Process(target=_hold, args=(path, ready, done)) for _ in range(4)]
    for proc in procs:
        proc.start()
    results = [ready.get(timeout=10) for _ in procs]
    done.set()
    for proc in procs:
        proc.join(10)
    assert sorted(results) == [False, False, False, True]


def test_health_alive_hung_and_stopped(tmp_path):
    paths = watcher.paths(tmp_path, "sample")
    assert watcher.health(tmp_path, "sample")["status"] == "stopped"
    paths["heartbeat"].parent.mkdir(parents=True, exist_ok=True)
    paths["heartbeat"].write_text(json.dumps({"pid": os.getpid(), "beat_at": time.time(), "token": "t"}))
    assert watcher.health(tmp_path, "sample")["status"] == "alive"
    paths["heartbeat"].write_text(json.dumps({"pid": os.getpid(), "beat_at": time.time() - 999}))
    assert watcher.health(tmp_path, "sample")["status"] == "hung"
    assert watcher.ensure(tmp_path, "sample", start=lambda *_: None)["action"] == "hung-needs-service-restart"
    # A dead watcher's heartbeat and leftover lock file never block a start.
    paths["heartbeat"].write_text(json.dumps({"pid": 2 ** 22 + 12345, "beat_at": time.time()}))
    paths["lock"].write_text("left behind")
    assert watcher.health(tmp_path, "sample")["status"] == "stopped"
    started = []
    watcher.ensure(tmp_path, "sample", start=lambda argv, root: started.append(argv))
    assert started and started[0][-2:] == ["--project", "sample"]


def test_health_check_never_takes_the_lock(tmp_path):
    lock = watcher.Lock(watcher.paths(tmp_path, "sample")["lock"])
    assert lock.acquire()
    try:
        watcher.health(tmp_path, "sample")
        assert lock.handle is not None
    finally:
        lock.release()


def test_second_watcher_process_exits_zero(tmp_path, monkeypatch):
    cfg, _ = _project(tmp_path, monkeypatch)
    monkeypatch.setenv("MHB_HOME", str(cfg.root))
    lock = watcher.Lock(watcher.paths(cfg.root, "sample")["lock"])
    assert lock.acquire()
    try:
        assert watcher.main(["--project", "sample", "--once"]) == 0
        assert not watcher.paths(cfg.root, "sample")["heartbeat"].exists()
    finally:
        lock.release()


def test_one_pass_forwards_pull_routes_feeds_and_leaves_coordinator_mail(tmp_path, monkeypatch):
    cfg, store = _project(tmp_path, monkeypatch)
    config.set_project_option(cfg.root, "sample", "claude", "coordinator_pickup", "push")
    monkeypatch.setattr(watcher.wake, "cycle", lambda *a, **k: [])
    sent = store.send_fanout("sample", "codex", ["devin", "claude"], "Line one\nmore", "req",
                             session="codex-coordinator")
    logs = []
    state = {}
    watcher.run_pass(cfg, store, "sample", state, log=logs.append)
    watcher.run_pass(cfg, store, "sample", state, log=logs.append)
    stages = {d["recipient"]: store.trace("sample", d["id"])["stage"] for d in sent["deliveries"]}
    assert stages == {"devin": "coordinator", "claude": "inbox"}  # claude is push: its bridge forwards
    assert store.status("sample", "devin")["leased"] == 0
    feed = [json.loads(line) for line in watcher.paths(cfg.root, "sample")["feed"].read_text().splitlines()]
    assert {item["excerpt"] for item in feed} == {"Line one"}
    assert os.stat(watcher.paths(cfg.root, "sample")["feed"]).st_mode & 0o077 == 0


def test_digest_waits_for_news_then_marks_ready_and_show_marks_seen(tmp_path, monkeypatch):
    cfg, store = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(watcher.wake, "cycle", lambda *a, **k: [])
    now = [1000.0]
    state = {}
    feed = watcher.paths(cfg.root, "sample")["feed"]
    assert watcher.schedule_digests(cfg.root, "sample", ["codex"], feed, state, clock=lambda: now[0]) == []
    now[0] += 31 * 60
    assert watcher.schedule_digests(cfg.root, "sample", ["codex"], feed, state, clock=lambda: now[0]) == []
    feed.parent.mkdir(parents=True, exist_ok=True)
    feed.write_text(json.dumps({"at": now[0] + 1, "sender": "devin", "recipient": "codex",
                                "excerpt": "hi"}) + "\n")
    now[0] += 31 * 60
    ready = watcher.schedule_digests(cfg.root, "sample", ["codex"], feed, state, clock=lambda: now[0])
    assert ready == [{"harness": "codex", "seq": 1, "count": 1}]
    watcher._save_state(watcher.paths(cfg.root, "sample")["state"], state)
    shown = watcher.take_digest(cfg.root, "sample", "codex", clock=lambda: now[0])
    assert shown["count"] == 1 and shown["footer"] == "Reply 'stop digests' to pause them."
    assert watcher.take_digest(cfg.root, "sample", "codex", clock=lambda: now[0] + 1)["count"] == 0


def test_digest_off_is_stored_and_offered_back(tmp_path, monkeypatch):
    cfg, _ = _project(tmp_path, monkeypatch)
    assert watcher.digest_command(cfg.root, "sample", "codex", "off")["digest"] == "off"
    assert pickup.settings(cfg.root, "sample", "codex")["digest"] == "off"
    shown = watcher.digest_command(cfg.root, "sample", "codex", "show")
    assert shown["footer"].startswith("Digests are off.")


def test_missing_listener_alerts_bridge_first_once_per_message_then_escalates(tmp_path, monkeypatch):
    cfg, store = _project(tmp_path, monkeypatch)
    message = store.send("sample", "codex", "claude", "x", "k")
    store.forward_pull("sample", "claude")
    import sqlite3
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE messages SET updated=? WHERE id=?", (time.time() - 999, message["id"]))
    calls = []
    monkeypatch.setattr(watcher, "_alert_command", lambda: ["alert", "{project}", "{text}"])
    monkeypatch.setattr(watcher.subprocess, "run", lambda argv, **_: calls.append(argv))
    state, logs = {}, []
    for _ in range(2):
        watcher._check_listeners(cfg, store, "sample", ["claude"], state, logs.append)
    assert [entry["event"] for entry in logs] == ["fallback-report"]  # once per message, not per pass
    assert calls == []  # the bridge chat hears first
    feed = watcher.read_feed(watcher.paths(cfg.root, "sample")["feed"], 0)
    assert feed[-1]["kind"] == "alert" and "Fix: retype take coordinator" in feed[-1]["excerpt"]
    marker = json.loads(watcher.digest_path(cfg.root, "sample", "claude").read_text())
    assert marker["alert"] is True
    watcher.escalate_alerts(cfg.root, "sample", state, logs.append, clock=lambda: time.time() + 60)
    assert calls == []
    watcher.escalate_alerts(cfg.root, "sample", state, logs.append, clock=lambda: time.time() + 16 * 60)
    watcher.escalate_alerts(cfg.root, "sample", state, logs.append, clock=lambda: time.time() + 30 * 60)
    assert len(calls) == 1 and "still waiting" in calls[0][2]
    claimed = store.claim("sample", "claude", "coordinator", "claude-coordinator")
    store.finish("sample", "claude", "coordinator", "claude-coordinator", claimed["id"], claimed["lease"])
    watcher._clear_resolved_alerts(store, "sample", state)
    assert state["alerts"] == {}


def test_dispatcher_problems_become_one_alert_per_message_naming_chat_and_fix(tmp_path, monkeypatch):
    cfg, store = _project(tmp_path, monkeypatch)
    from multi_harness_bridge import wake
    message_id = "11111111-2222-4333-8444-555555555555"
    route = ("sample", "codex", "coordinator", "codex-coordinator")
    with __import__("contextlib").closing(wake._ledger(store.path)) as db, db:
        wake._pickup_event(db, route, message_id, "held-paused", "{}")
        wake._pickup_event(db, route, message_id, "fallback-undelivered", "{}")

    class Named(pickup.CodexPickup):
        def __init__(self):
            pass

        def chat_name(self, session):
            return "Team Coordinator"
    monkeypatch.setitem(pickup._ADAPTERS, "codex", Named)
    state, logs = {}, []
    watcher._alert_pickup_events(cfg, store, "sample", state, logs.append)
    watcher._alert_pickup_events(cfg, store, "sample", state, logs.append)
    assert len(state["alerts"]) == 1
    text = next(iter(state["alerts"].values()))["text"]
    assert 'click Resume in the codex coordinator chat "Team Coordinator"' in text
    outside = next(iter(state["alerts"].values()))["external_text"]
    assert "Team Coordinator" not in outside and "click Resume in the codex coordinator chat" in outside
