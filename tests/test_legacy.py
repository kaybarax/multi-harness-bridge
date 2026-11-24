from contextlib import closing
import json
import sqlite3
from uuid import uuid4

import pytest

from multi_harness_bridge import config, legacy


def write_db(path, schema, writes=()):
    with closing(sqlite3.connect(path)) as db, db:
        if schema:
            db.executescript(schema)
        for statement, values in writes:
            db.execute(statement, values)


def legacy_fixture(tmp_path):
    docs, dev, installation = (tmp_path / name for name in ("docs", "dev", "legacy"))
    state = installation / "state"
    watcher = state / "watcher"
    for folder in (docs, dev, watcher):
        folder.mkdir(parents=True)
    cfg = config.register("example-project", (docs, dev), tmp_path / "state")
    config.atomic_json(cfg.root / "legacy.json", {
        "version": 1, "project": "example-project", "installation": str(installation),
    })
    (state / "project-bindings.lock").touch()
    roles = {host: [str(uuid4()), str(uuid4())] for host in ("codex", "claude", "antigravity")}
    roles["devin"] = ["devin-coordinator", "devin-inbox"]
    for harness in ("codex", "claude"):
        (watcher / f"{harness}.thread").write_text(roles[harness][0])
        (watcher / f"{harness}.receiver").write_text(roles[harness][1])
    (watcher / "enabled").touch()
    write_db(state / "mailbox.sqlite3", """
        CREATE TABLE messages(seq INTEGER PRIMARY KEY, body TEXT);
        INSERT INTO messages VALUES(1,'untouched legacy payload');
        CREATE TABLE project_delivery_control(slot,enabled);
        INSERT INTO project_delivery_control VALUES(1,1);
        CREATE TABLE harness_bindings(actor,coordinator,receiver);
        """, (
            ("INSERT INTO harness_bindings VALUES('devin_local',?,?)", roles["devin"]),
            ("INSERT INTO harness_bindings VALUES('antigravity_desktop',?,?)", roles["antigravity"]),
        ))
    write_db(state / "devin-local-relay.sqlite3", """
        CREATE TABLE coordinator_binding(slot,session);
        CREATE TABLE inbox_binding(slot,session);
        CREATE TABLE transport_control(slot,enabled);
        INSERT INTO transport_control VALUES(1,1);
        """, (
            ("INSERT INTO coordinator_binding VALUES(1,?)", roles["devin"][:1]),
            ("INSERT INTO inbox_binding VALUES(1,?)", roles["devin"][1:]),
        ))
    write_db(state / "antigravity-desktop.sqlite3", """
        CREATE TABLE desktop_roles(role,conversation);
        CREATE TABLE desktop_pickup_control(slot,enabled);
        INSERT INTO desktop_pickup_control VALUES(1,1);
        """, (
            ("INSERT INTO desktop_roles VALUES('coordinator',?)", roles["antigravity"][:1]),
            ("INSERT INTO desktop_roles VALUES('inbox',?)", roles["antigravity"][1:]),
        ))
    return cfg, state, roles


def test_portable_devin_defers_to_either_legacy_dispatcher_without_spending_an_attempt(tmp_path, monkeypatch):
    import fcntl
    from multi_harness_bridge import legacy, wake

    cfg, state, roles = legacy_fixture(tmp_path)
    store = config.open_store(cfg)
    project = "example-project"
    route = (project, "devin", "inbox", f"acp/devin-cli/{roles['devin'][1]}")
    item = store.send(project, "codex", "devin", "portable message", "stable-id")
    launched = []

    def launch(_harness, values, *, before_prompt):
        assert before_prompt()
        for name in legacy.DEVIN_WAKE_LOCKS:
            with (state / name).open("rb") as contender, pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        launched.append(values["notification_id"])

    monkeypatch.setattr(wake, "_default_wake", launch)
    for name in legacy.DEVIN_WAKE_LOCKS:
        with (state / name).open("a") as legacy_dispatcher:
            fcntl.flock(legacy_dispatcher, fcntl.LOCK_EX)
            with pytest.raises(wake.WakeDeferred, match="legacy Devin dispatcher"):
                wake.wake_one(store, *route, commands={})
            attempts, = wake.list_attempts(store, project)
            assert (attempts["attempts"], attempts["state"]) == (0, "retryable")
    assert wake.wake_one(store, *route, commands={}) == {"id": item["id"], "status": "sent"}
    assert launched == [item["id"]]
    with sqlite3.connect(state / "mailbox.sqlite3") as db:
        assert db.execute("SELECT body FROM messages").fetchall() == [("untouched legacy payload",)]


def test_store_uses_legacy_authority_and_keeps_mailboxes_separate(tmp_path):
    cfg, state, roles = legacy_fixture(tmp_path)
    store = config.open_store(cfg)
    coordinator, inbox = roles["codex"]
    legacy_mailbox = state / "mailbox.sqlite3"
    before = legacy_mailbox.read_bytes()
    request = store.send_fanout("example-project", "codex", ["devin", "antigravity"],
                                "synthetic message", "split", session=coordinator)
    item = store.claim("example-project", "codex", "inbox", inbox)
    assert item["request_id"] == request["request_id"]
    assert store.finish("example-project", "codex", "inbox", inbox, item["id"], item["lease"])
    new_coordinator = str(uuid4())
    (state / "watcher/codex.thread").write_text(new_coordinator)
    with pytest.raises(ValueError, match="current coordinator"):
        store.send_fanout("example-project", "codex", ["devin"], "retired", "retired", session=coordinator)
    assert store.send_fanout("example-project", "codex", ["devin", "antigravity"],
                             "synthetic message", "split", session=new_coordinator)["request_id"] == request["request_id"]
    assert legacy_mailbox.read_bytes() == before
    with pytest.raises(ValueError, match="native"):
        store.set_enabled("example-project", "codex", False, session=new_coordinator)


def test_legacy_controls_and_role_disagreement_are_observed_at_call_time(tmp_path):
    cfg, state, roles = legacy_fixture(tmp_path)
    store = config.open_store(cfg)
    provider = legacy.load(cfg)
    assert provider.enabled("example-project", "claude")
    (state / "watcher/claude-only").touch()
    assert not store.status("example-project", "codex")["enabled"]
    assert store.status("example-project", "claude")["enabled"]
    (state / "watcher/claude.thread").unlink()
    (state.parent / "receiver-health.json").write_text(json.dumps({"coordinator_session": "local_" + roles["claude"][0]}))
    assert store.roles("example-project", "claude")["coordinator"] == roles["claude"][0]
    native_devin = state / "devin-local-relay.sqlite3"
    write_db(native_devin, None, (("UPDATE transport_control SET enabled=?", (0,)),))
    assert not store.status("example-project", "devin")["enabled"]
    write_db(native_devin, None, (("UPDATE inbox_binding SET session=?", ("new-devin-inbox",)),))
    with pytest.raises(RuntimeError, match="disagree"):
        store.roles("example-project", "devin")
    write_db(state / "mailbox.sqlite3", None, (
        ("UPDATE harness_bindings SET receiver=? WHERE actor='devin_local'", ("new-devin-inbox",)),
    ))
    assert store.roles("example-project", "devin")["inbox"] == "acp/devin-cli/new-devin-inbox"
    assert not store.status("example-project", "devin")["enabled"]
    (state / "role-handoff.pending.json").touch()
    with pytest.raises(RuntimeError, match="recovery"):
        store.roles("example-project", "devin")


def test_devin_wake_resolves_the_bound_sessions_actual_workspace(tmp_path, monkeypatch):
    from multi_harness_bridge import wake

    docs, dev, outside = (tmp_path / name for name in ("docs", "dev", "other"))
    for root in (docs, dev, outside):
        root.mkdir()
    cfg = config.register("example-project", (docs, dev), tmp_path / "state")
    native_db = tmp_path / "native.sqlite3"
    write_db(native_db, "CREATE TABLE sessions(id,working_directory,hidden)", (
        ("INSERT INTO sessions VALUES(?,?,0)", ("coordinator", str(dev))),
        ("INSERT INTO sessions VALUES(?,?,0)", ("foreign", str(outside))),
        ("INSERT INTO sessions VALUES(?,?,1)", ("retired", str(docs))),
    ))
    monkeypatch.setenv("MHB_DEVIN_SESSIONS_DB", str(native_db))
    assert wake._devin_workspace(cfg, "example-project", "acp/devin-cli/coordinator") == dev
    for name in ("foreign", "retired", "missing"):
        with pytest.raises(wake.WakeDeferred, match="no verified working directory"):
            wake._devin_workspace(cfg, "example-project", f"acp/devin-cli/{name}")


def test_legacy_handoff_refreshes_both_manifests_after_rotation(tmp_path):
    from types import SimpleNamespace

    from multi_harness_bridge import legacy_devin, reconcile

    cfg, state, roles = legacy_fixture(tmp_path)
    installation = state.parent
    roots = cfg.projects["example-project"]
    legacy_python = str(installation / ".venv/bin/python")
    route_script = str(installation / "devin_local_route_mcp.py")
    for root in roots:
        path = root / reconcile.DEVIN_MANIFEST
        path.parent.mkdir()
        config.atomic_json(path, {"mcpServers": {
            "devin-example-project-role-handoff": {
                "command": legacy_python, "args": [route_script, "role-handoff", "example-project"],
            },
            "devin-example-project-route-inbox": {
                "command": legacy_python, "args": [route_script, "inbox", "example-project", roles["devin"][1]],
            },
            "unrelated": {"enabled": False},
        }})
    assert len(reconcile.reconcile_devin(cfg, "example-project", apply=True)) == 2

    def stock_refresh(*args):
        raise AssertionError("The compatibility handler must replace the single-root refresh")

    module = SimpleNamespace(_refresh_role_manifest=stock_refresh)

    def simulate_native_handoff(project):
        new_inbox = "acp/devin-cli/new-inbox"
        write_db(state / "devin-local-relay.sqlite3", None, (
            ("UPDATE inbox_binding SET session=? WHERE slot=1", (new_inbox,)),
        ))
        write_db(state / "mailbox.sqlite3", None, (
            ("UPDATE harness_bindings SET receiver=? WHERE actor='devin_local'", (new_inbox,)),
        ))
        assert module._refresh_role_manifest(SimpleNamespace(name=project, workspace=roots[1]), None)
        for root in roots:
            servers = json.loads((root / reconcile.DEVIN_MANIFEST).read_text())["mcpServers"]
            assert servers["devin-example-project-route-inbox"]["args"][-1] == new_inbox
            assert servers["devin-example-project-role-handoff"]["args"][-1] == "serve-handoff"
            assert servers["unrelated"] == {"enabled": False}
        assert not reconcile.reconcile_devin(cfg, "example-project")

    module.serve_role_handoff = simulate_native_handoff
    legacy_devin.serve_handoff(cfg, "example-project", route_module=module)
    assert module._refresh_role_manifest is stock_refresh
