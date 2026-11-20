import json
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from multi_harness_bridge import ag_wake, config
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
    target = ag_wake._write_ax_context(tmp_path, project="alpha", action="wake", role="inbox",
        session=sessions["alpha"], notification_id=str(uuid.uuid4()), cfg=cfg, summary_db=summary_db)
    assert target.stat().st_mode & 0o777 == 0o600
    value = json.loads(target.read_text(encoding="utf-8"))
    assert value["roles"]["alpha"]["inbox"]["conversation"] == sessions["alpha"]
    assert "beta" in value["roles"]


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
    (directory/"mcp_config.json").write_text('{"mcpServers":{"unrelated":{"command":"existing"}}}')
    for _ in range(2):
        ag_inbox.install(cfg,"alpha",directory)
    assert set(json.loads((directory/"mcp_config.json").read_text())["mcpServers"]) == {"unrelated","mhb_alpha"}
    assert "PreToolUse" in json.loads((directory/"hooks.json").read_text())["mhb_alpha"]
    import asyncio
    pytest.importorskip("mcp")
    assert set(ag_inbox.TOOLS) == {t.name for t in asyncio.run(ag_inbox.make_server(cfg,"alpha").list_tools())}
