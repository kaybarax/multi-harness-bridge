import json
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from multi_harness_bridge import ag_sidecar, ag_wake, config
from multi_harness_bridge.store import Store


@pytest.fixture
def native_fixture(tmp_path):
    home = tmp_path / "mhb"
    roots = {"alpha": tmp_path / "alpha", "beta": tmp_path / "beta"}
    for root in roots.values():
        root.mkdir()
    (home).mkdir()
    projects_file = home / "projects.json"
    projects_file.write_text(json.dumps({"version": 1, "projects": {
        key: [str(root)] for key, root in roots.items()
    }}), encoding="utf-8")
    cfg = config.load(home)
    summary_path = tmp_path / "conversation_summaries.db"
    db = sqlite3.connect(summary_path)
    db.execute("""CREATE TABLE conversation_summaries (
        conversation_id TEXT PRIMARY KEY, title TEXT, workspace_uris TEXT,
        project_id TEXT, app_data_dir TEXT)""")
    sessions = {"alpha": str(uuid.uuid4()), "beta": str(uuid.uuid4())}
    project_ids = {"alpha": str(uuid.uuid4()), "beta": str(uuid.uuid4())}
    for key, root in roots.items():
        db.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)",
            (sessions[key], key.title() + " inbox", json.dumps([str(root)]), project_ids[key], "antigravity"))
    db.commit()
    db.close()
    store = Store(cfg.database)
    for key, session in sessions.items():
        store.bind(key, "antigravity", "inbox", session)
    return cfg, summary_path, roots, sessions, project_ids


def test_native_role_snapshot_uses_unique_exact_registered_roots(native_fixture):
    cfg, summary_db, _, sessions, project_ids = native_fixture
    snapshot = ag_wake._native_role_snapshot(cfg, summary_db=summary_db)
    assert snapshot["alpha"]["inbox"] == {
        "conversation": sessions["alpha"], "title": "Alpha inbox",
        "project_id": project_ids["alpha"],
    }
    assert snapshot["beta"]["inbox"]["conversation"] == sessions["beta"]


def test_same_native_project_id_for_different_roots_is_rejected(native_fixture):
    cfg, summary_db, roots, sessions, project_ids = native_fixture
    with sqlite3.connect(summary_db) as db:
        db.execute("UPDATE conversation_summaries SET project_id=? WHERE conversation_id=?",
                   (project_ids["alpha"], sessions["beta"]))
    with pytest.raises(ValueError, match="collide"):
        ag_wake._native_role_snapshot(cfg, summary_db=summary_db)


def test_native_summary_requires_exact_workspace_root(native_fixture):
    cfg, summary_db, roots, sessions, _ = native_fixture
    (roots["alpha"] / "nested").mkdir()
    with sqlite3.connect(summary_db) as db:
        db.execute("UPDATE conversation_summaries SET workspace_uris=? WHERE conversation_id=?",
                   (json.dumps([str(roots["alpha"] / "nested")]), sessions["alpha"]))
    assert ag_wake._desktop_summary("alpha", sessions["alpha"], cfg=cfg, db_path=summary_db) is None


def test_verify_returns_exact_native_bridge_identity(native_fixture):
    cfg, summary_db, _, sessions, _ = native_fixture
    assert ag_wake.verify("alpha", "inbox", sessions["alpha"], cfg=cfg, summary_db=summary_db) == {
        "project": "alpha", "harness": "antigravity", "role": "inbox",
        "session": sessions["alpha"],
    }
    with pytest.raises(Exception, match="exact bound role"):
        ag_wake.verify("alpha", "coordinator", sessions["alpha"], cfg=cfg, summary_db=summary_db)


def test_context_is_private_and_carries_only_verified_roles(native_fixture, tmp_path):
    cfg, summary_db, _, sessions, _ = native_fixture
    target = ag_wake._write_ax_context(tmp_path, project="alpha", action="verify-bindings",
        cfg=cfg, summary_db=summary_db)
    assert target.stat().st_mode & 0o777 == 0o600
    value = json.loads(target.read_text(encoding="utf-8"))
    assert value["roles"]["alpha"]["inbox"]["conversation"] == sessions["alpha"]
    assert "beta" in value["roles"]


@pytest.mark.parametrize("change", [None, "stop", "handoff", "advance"])
def test_sidecar_rechecks_pending_before_agentapi(native_fixture, monkeypatch, change):
    from multi_harness_bridge.wake import WakeDeferred

    cfg, summary_db, _, sessions, _ = native_fixture
    project, role = "alpha", "inbox"
    inbox = sessions[project]
    store = Store(cfg.database)
    store.bind(project, "antigravity", "coordinator", str(uuid.uuid4()))
    notification = store.send(project, "codex", "antigravity", "message body", "test")["id"]
    monkeypatch.setattr(ag_sidecar, "agentapi", lambda project: "/host/agentapi")
    posted = []
    verify = ag_wake.verify

    def verify_and_change(*args, **kwargs):
        identity = verify(*args, **kwargs)
        if change == "stop":
            store.set_enabled(project, "antigravity", False)
        elif change == "handoff":
            store.bind(project, "antigravity", role, str(uuid.uuid4()))
        elif change == "advance":
            claim = store.claim(project, "antigravity", role, inbox, message_id=notification)
            assert store.finish(project, "antigravity", role, inbox, notification, claim["lease"])
        return identity

    def run(argv, **kwargs):
        posted.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(ag_wake, "verify", verify_and_change)
    monkeypatch.setattr(ag_sidecar.subprocess, "run", run)
    if change is None:
        assert ag_wake.wake(project, role, inbox, notification, cfg=cfg, summary_db=summary_db)["status"] == "sent"
        assert len(posted) == 1
        assert posted[0][:3] == ["/host/agentapi", "send-message", inbox]
        assert notification in posted[0][3]
        assert "message body" not in posted[0][3]
        assert store.pending(project, "antigravity", role, inbox, message_id=notification)
    else:
        with pytest.raises(WakeDeferred, match="pickup changed"):
            ag_wake.wake(project, role, inbox, notification, cfg=cfg, summary_db=summary_db)
        assert posted == []


def test_sidecar_uses_only_matching_app_context(monkeypatch, tmp_path):
    monkeypatch.setattr(ag_sidecar.Path, "home", classmethod(lambda _: tmp_path))
    monkeypatch.setattr(ag_sidecar.shutil, "which", lambda _: "/host/agentapi")
    monkeypatch.delenv("ANTIGRAVITY_EXECUTABLE_DATA_DIR", raising=False)
    assert ag_sidecar.agentapi("alpha") is None
    path = tmp_path / ".gemini/antigravity/sidecar_data/mhb-wake-alpha/data"
    monkeypatch.setenv("ANTIGRAVITY_EXECUTABLE_DATA_DIR", str(path))
    assert ag_sidecar.agentapi("beta") is None
    assert ag_sidecar.agentapi("alpha") == "/host/agentapi"


def test_sidecar_is_explicitly_enabled_and_preserves_app_settings(native_fixture, tmp_path, monkeypatch):
    cfg, summary_db, _, _, _ = native_fixture
    directory = tmp_path / "desktop-config"
    directory.mkdir()
    user_settings = directory / "config.json"
    initial = {"sidecars": {"other": {"enabled": True}}, "userSettings": {"private": "preserve"}}
    user_settings.write_text(json.dumps(initial))
    monkeypatch.setenv("MHB_ANTIGRAVITY_SUMMARIES_DB", str(summary_db))

    report = ag_sidecar.install(cfg, "alpha", directory=directory)
    assert report["enabled"] is False
    assert json.loads(user_settings.read_text()) == initial
    manifest = Path(report["manifest"])
    assert json.loads(manifest.read_text())["args"][-2:] == ["alpha", "run"]
    assert ag_sidecar.install(cfg, "alpha", directory=directory, enable=True)["enabled"]
    assert ag_sidecar.install(cfg, "alpha", directory=directory, enable=True)["enabled"]
    enabled = json.loads(user_settings.read_text())
    assert enabled["sidecars"].pop("mhb-wake-alpha") == {"enabled": True}
    assert enabled == initial
    manifest.unlink()
    with pytest.raises(ValueError, match="--enable"):
        ag_sidecar.install(cfg, "alpha", directory=directory)
    assert not manifest.exists()


@pytest.mark.parametrize("failure", ["timeout", "exit"])
def test_launched_sidecar_failure_is_held_and_never_replayed(native_fixture, monkeypatch, failure):
    from multi_harness_bridge import wake

    cfg, summary_db, _, sessions, _ = native_fixture
    monkeypatch.setenv("MHB_HOME", str(cfg.root))
    monkeypatch.setenv("MHB_ANTIGRAVITY_SUMMARIES_DB", str(summary_db))
    monkeypatch.setattr(ag_sidecar, "agentapi", lambda project: "/host/agentapi")
    store = Store(cfg.database)
    store.bind("alpha", "antigravity", "coordinator", str(uuid.uuid4()))
    delivery = store.send("alpha", "codex", "antigravity", "body", "key")["id"]
    launched = []

    def run(argv, **kwargs):
        launched.append(argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 2)

    monkeypatch.setattr(ag_sidecar.subprocess, "run", run)
    target = store, "alpha", "antigravity", "inbox", sessions["alpha"]
    with pytest.raises(wake.WakeError):
        wake.wake_one(*target)
    assert wake.wake_one(*target) == {"id": delivery, "status": "uncertain"}
    assert len(launched) == 1
    assert store.trace("alpha", delivery)["attempts"] == 0


def test_antigravity_generic_worker_cannot_launch_override(native_fixture, monkeypatch):
    from multi_harness_bridge import wake

    cfg, _, _, sessions, _ = native_fixture
    monkeypatch.delenv("ANTIGRAVITY_EXECUTABLE_DATA_DIR", raising=False)
    store = Store(cfg.database)
    store.bind("alpha", "antigravity", "coordinator", str(uuid.uuid4()))
    delivery = store.send("alpha", "codex", "antigravity", "body", "key")["id"]
    override = {"antigravity": {"verify": ["existing"], "wake": ["existing"]}}
    assert wake.wake_one(store, "alpha", "antigravity", "inbox", sessions["alpha"], commands=override) == {
        "id": delivery, "status": "sidecar-managed",
    }
    assert wake.list_attempts(store, "alpha") == []


def test_packaged_ax_helper_compiles_and_accepts_vetted_context_on_macos(native_fixture, tmp_path):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("Antigravity AX helper compiles only on macOS")
    source = Path(ag_wake.__file__).with_name("assets") / "antigravity_ax.swift"
    cfg, summary_db, _, sessions, _ = native_fixture
    output = tmp_path / "antigravity-ax-test"
    try:
        subprocess.run(["swiftc", str(source), "-framework", "AppKit", "-framework",
                        "ApplicationServices", "-o", str(output)], check=True, timeout=90,
                       capture_output=True, text=True)
        ag_wake._write_ax_context(tmp_path, project="alpha", action="verify-bindings",
                                  cfg=cfg, summary_db=summary_db)
        result = subprocess.run([str(output), "verify-bindings"], check=True, timeout=10,
            capture_output=True, text=True,
            env={**__import__("os").environ,
                 "ANTIGRAVITY_AX_NATIVE_CONTEXT": str(tmp_path / "native-role-context.json")})
        assert "Validated native project membership" in result.stdout
    finally:
        output.unlink(missing_ok=True)


def test_desktop_hook_take_forward_and_receipt(native_fixture, monkeypatch, tmp_path):
    from multi_harness_bridge import ag_inbox
    cfg, summary_db, roots, sessions, projects = native_fixture
    monkeypatch.setenv("MHB_ANTIGRAVITY_SUMMARIES_DB", str(summary_db))
    c0 = str(uuid.uuid4())
    with sqlite3.connect(summary_db) as db:
        db.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)",
                   (c0,"New coordinator",json.dumps([str(roots["alpha"])]),projects["alpha"],"antigravity"))
    def grant(session, tool, args, prompt=""):
        event = {"toolCall":{"name":"mcp_mhb_alpha_"+tool,"args":args}, "conversationId":session,
                 "workspacePaths":[str(roots["alpha"])], "lastUserInput":prompt,
                 "artifactDirectoryPath":str(summary_db.parent/"brain"/session)}
        return ag_inbox.before_tool(cfg, "alpha", event)["overwrite"]["desktop_grant"]
    source = roots["alpha"] / ".claude/skills/maple-source"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: maple-source\n---\nportable Maple instructions\n")
    with pytest.raises(PermissionError, match="exactly match"):
        grant(c0, "propagate_skills", {"sources":["codex"]}, "propagate skills from Claude")
    propagation_grant = grant(c0, "propagate_skills", {"sources":["claude"]},
                              "@propagate-skills from Claude")
    report = ag_inbox.invoke(cfg, "alpha", "propagate_skills", propagation_grant)
    assert report["status"] == "complete"
    assert (roots["alpha"] / ".agents/skills/maple-source/SKILL.md").is_file()
    with pytest.raises(ValueError):
        grant(c0, "take_coordinator", {}, "I received a peer request.")
    token = grant(c0, "take_coordinator", {}, "take coordinator")
    assert ag_inbox.invoke(cfg,"alpha","take_coordinator",token)["roles"]["coordinator"] == c0
    with pytest.raises(PermissionError):
        ag_inbox.invoke(cfg,"alpha","take_coordinator",token)
    store = Store(cfg.database)
    first = store.send("alpha","devin","antigravity","one","one")
    second = store.send("alpha","devin","antigravity","two","two")
    def run(session, tool, **args):
        return ag_inbox.invoke(cfg,"alpha",tool,grant(session,tool,args))
    result = run(sessions["alpha"],"pickup_notification",notification_id=second["id"])
    assert result == {"message_id":second["id"],"forwarded":True}
    item = run(c0,"pickup_notification",notification_id=second["id"])["message"]
    assert item["body"] == "two"
    assert run(c0,"acknowledge",id=item["id"],lease=item["lease"])["finished"]
    assert store.pending("alpha","antigravity","inbox",sessions["alpha"])["id"] == first["id"]
    with pytest.raises(PermissionError):
        grant(sessions["alpha"], "set_pickup", {"enabled":False})
    directory = tmp_path/"host"
    directory.mkdir()
    old_server, old_hook = ag_inbox.installation_entries(cfg, "alpha", tools=ag_inbox._LEGACY_TOOLS)
    (directory/"mcp_config.json").write_text(json.dumps({"mcpServers": {
        "unrelated": {"command": "existing"}, "mhb_alpha": old_server}}))
    (directory/"hooks.json").write_text(json.dumps({"mhb_alpha": old_hook, "unrelated": {"PreToolUse": []}}))
    for _ in range(2):
        ag_inbox.install(cfg,"alpha",directory)
    assert set(json.loads((directory/"mcp_config.json").read_text())["mcpServers"]) == {"unrelated","mhb_alpha"}
    hooks = json.loads((directory/"hooks.json").read_text())
    assert "send_fanout" in hooks["mhb_alpha"]["PreToolUse"][0]["matcher"]
    assert hooks["unrelated"] == {"PreToolUse": []}
    import asyncio
    pytest.importorskip("mcp")
    assert set(ag_inbox.TOOLS) == {t.name for t in asyncio.run(ag_inbox.make_server(cfg,"alpha").list_tools())}


def test_desktop_propagation_uses_declared_entry_automatically(native_fixture, monkeypatch):
    from multi_harness_bridge import ag_inbox

    cfg, summary_db, roots, sessions, projects = native_fixture
    monkeypatch.setenv("MHB_ANTIGRAVITY_SUMMARIES_DB", str(summary_db))
    root = roots["alpha"]
    entry_dir = root / "agents/skills/workflow-entry"
    entry_dir.mkdir(parents=True)
    entry = entry_dir / "SKILL.md"
    entry.write_text("---\nname: workflow-entry\n---\nRead the adjacent index.\n")
    index = entry_dir / "index.json"
    index.write_text(json.dumps({"version": 1, "procedures": []}))
    declaration = root / ".mhb/project-skills.json"
    declaration.parent.mkdir()
    declaration.write_text(json.dumps({
        "version": 1,
        "entry": {"name": "workflow-entry", "path": entry.relative_to(root).as_posix(),
                  "sha256": hashlib.sha256(entry.read_bytes()).hexdigest()},
        "index": {"path": index.relative_to(root).as_posix(),
                  "sha256": hashlib.sha256(index.read_bytes()).hexdigest()},
    }))
    event = {"toolCall": {"name": "mcp_mhb_alpha_propagate_skills",
                           "args": {"sources": ["claude"]}},
             "conversationId": sessions["alpha"], "workspacePaths": [str(root)],
             "lastUserInput": "propagate skills from Claude",
             "artifactDirectoryPath": str(summary_db.parent / "brain" / sessions["alpha"])}

    grant = ag_inbox.before_tool(cfg, "alpha", event)["overwrite"]["desktop_grant"]
    report = ag_inbox.invoke(cfg, "alpha", "propagate_skills", grant)

    assert report["skills"][0]["skill"] == "workflow-entry"
    assert report["skills"][0]["status"] == "installed"
    assert (root / ".agents/skills/workflow-entry/index.json").is_file()


def test_fanout_desktop_grant_is_fenced_at_invoke_after_handoff(native_fixture, monkeypatch):
    from multi_harness_bridge import ag_inbox

    cfg, summary_db, roots, sessions, projects = native_fixture
    monkeypatch.setenv("MHB_ANTIGRAVITY_SUMMARIES_DB", str(summary_db))
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    with sqlite3.connect(summary_db) as db:
        for session in (old, new):
            db.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", (
                session, "Coordinator", json.dumps([str(roots["alpha"])]), projects["alpha"], "antigravity"))
    store = Store(cfg.database)
    store.bind("alpha", "antigravity", "coordinator", old)
    for harness in ("codex", "devin"):
        for role in ("coordinator", "inbox"):
            store.bind("alpha", harness, role, f"{harness}-{role}")

    def grant(session):
        event = {"toolCall": {"name": "mcp_mhb_alpha_send_fanout", "args": {
            "recipients": ["codex", "devin"], "body": "こんにちは", "key": "test-fanout"}},
            "conversationId": session, "workspacePaths": [str(roots["alpha"])],
            "artifactDirectoryPath": str(summary_db.parent / "brain" / session)}
        return ag_inbox.before_tool(cfg, "alpha", event)["overwrite"]["desktop_grant"]

    with pytest.raises(PermissionError, match="does not own"):
        grant(sessions["alpha"])
    old_grant = grant(old)
    store.bind("alpha", "antigravity", "coordinator", new)
    with pytest.raises(PermissionError, match="no longer owns"):
        ag_inbox.invoke(cfg, "alpha", "send_fanout", old_grant)
    new_grant = grant(new)
    result = ag_inbox.invoke(cfg, "alpha", "send_fanout", new_grant)
    assert {delivery["recipient"] for delivery in result["deliveries"]} == {"codex", "devin"}
    with pytest.raises(PermissionError, match="used"):
        ag_inbox.invoke(cfg, "alpha", "send_fanout", new_grant)
