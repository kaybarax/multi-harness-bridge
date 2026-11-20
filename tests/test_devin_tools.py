import json
import sqlite3
import sys
import time

import pytest

from multi_harness_bridge.config import Config
from multi_harness_bridge import devin_tools


def test_devin_manifest_registration_preserves_entries_and_is_idempotent(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    (project_root / ".devin").mkdir(parents=True)
    manifest = project_root / ".devin/mcp_config.local.json"
    manifest.write_text(json.dumps({"mcpServers": {"existing": {"command": "keep", "args": []}}}))
    dev_root = tmp_path / "dev"
    dev_root.mkdir()
    cfg = Config(tmp_path / "bridge-home", {"sample": (project_root, dev_root)})
    first = devin_tools.install(cfg, "sample")
    second = devin_tools.install(cfg, "sample")
    data = json.loads(manifest.read_text())
    assert first["status"] == "installed"
    assert second["status"] == "already-current"
    assert data["mcpServers"]["existing"] == {"command": "keep", "args": []}
    assert data["mcpServers"]["mhb_roles_sample"]["command"] == sys.executable
    secondary = json.loads((dev_root / ".devin/mcp_config.local.json").read_text())
    assert secondary["mcpServers"]["mhb_roles_sample"] == data["mcpServers"]["mhb_roles_sample"]


def test_devin_tool_wire_verifier_correlates_direct_turn_and_exact_server(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    home = tmp_path / "bridge-home"
    cfg = Config(home, {"sample": (workspace,)})
    monkeypatch.setattr(devin_tools.config, "load", lambda: cfg)
    native = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(native) as db:
        db.execute("CREATE TABLE sessions(id TEXT,working_directory TEXT,workspace_dirs TEXT,title TEXT,hidden INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE message_nodes(session_id TEXT,row_id INTEGER,chat_message TEXT,created_at REAL)")
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,0)", ("native-id", str(workspace), "[]", "chat"))
        db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)", ("native-id", 1,
            json.dumps({"role":"user","content":"take coordinator"}), time.time()))
        call = {"role":"assistant","tool_calls":[{"id":"call-1","name":"mcp_call_tool","arguments":{
            "server_name":"mhb_roles_sample","tool_name":"prepare_role_handoff","arguments":{}}}]}
        db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)", ("native-id", 2, json.dumps(call), time.time()))
    monkeypatch.setenv("MHB_DEVIN_SESSIONS_DB", str(native))
    result = devin_tools._native_call("sample", "prepare_role_handoff", role_command="coordinator")
    assert result["session"] == "acp/devin-cli/native-id"
    assert result["workspace"] == workspace

    with sqlite3.connect(native) as db:
        db.execute("UPDATE message_nodes SET chat_message=? WHERE row_id=2", (json.dumps({**call,"tool_calls":[{
            "id":"call-2","name":"mcp_call_tool","arguments":{"server_name":"wrong","tool_name":"prepare_role_handoff","arguments":{}}}]}),))
    try:
        devin_tools._native_call("sample", "prepare_role_handoff", role_command="coordinator")
    except PermissionError:
        pass
    else:
        raise AssertionError("unrelated native MCP server must not be accepted")


@pytest.mark.parametrize("picker_prefix", ["/", "/devin:", "/agents:"])
def test_devin_native_slash_skill_chip_is_verified_as_direct_user_command(tmp_path, monkeypatch, picker_prefix):
    workspace = tmp_path / "project"
    workspace.mkdir()
    cfg = Config(tmp_path / "bridge-home", {"sample": (workspace,)})
    monkeypatch.setattr(devin_tools.config, "load", lambda: cfg)
    native = tmp_path / "sessions.sqlite3"
    chip = {"role": "user", "content": "", "metadata": {
        "is_user_input": True,
        "extensions": {"chisel/acp-content-blocks": [
            {"type": "text", "text": f"{picker_prefix}take-coordinator 'Devin native title'"}]}}}
    call = {"role": "assistant", "tool_calls": [{"id": "call-1", "name": "mcp_call_tool", "arguments": {
        "server_name": "mhb_roles_sample", "tool_name": "prepare_role_handoff", "arguments": {}}}]}
    with sqlite3.connect(native) as db:
        db.execute("CREATE TABLE sessions(id TEXT,working_directory TEXT,workspace_dirs TEXT,title TEXT,hidden INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE message_nodes(session_id TEXT,row_id INTEGER,chat_message TEXT,created_at REAL)")
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,0)", ("native-id", str(workspace), "[]", "chat"))
        db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)", ("native-id", 1, json.dumps(chip), time.time()))
        db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)", ("native-id", 2, json.dumps(call), time.time()))
    monkeypatch.setenv("MHB_DEVIN_SESSIONS_DB", str(native))
    result = devin_tools._native_call("sample", "prepare_role_handoff", role_command="coordinator")
    assert result["session"] == "acp/devin-cli/native-id"
    assert devin_tools._devin_user_command(chip) == "/take-coordinator 'Devin native title'"


def test_devin_does_not_normalize_other_namespaces_or_non_native_input():
    chip = {"metadata": {"is_user_input": True, "extensions": {
        "chisel/acp-content-blocks": [{"type": "text", "text": "/foreign:take-coordinator"}]}}}
    assert devin_tools._devin_user_command(chip) == "/foreign:take-coordinator"
    assert devin_tools._devin_user_command({"content": "/devin:take-coordinator"}) == "/devin:take-coordinator"
    chip["metadata"]["is_user_input"] = False
    assert devin_tools._devin_user_command(chip) == ""


def test_devin_automatic_tools_require_active_attempt_and_persisted_native_pickup(tmp_path, monkeypatch):
    from contextlib import closing
    from datetime import datetime, timezone
    from multi_harness_bridge import devin_acp, wake
    from multi_harness_bridge.store import Store

    pytest.importorskip("mcp")
    cfg = Config(tmp_path, {"sample": (tmp_path,)})
    monkeypatch.setattr(devin_tools.config, "load", lambda: cfg)
    native = tmp_path / "sessions.sqlite3"
    monkeypatch.setenv("MHB_DEVIN_SESSIONS_DB", str(native))
    session = "acp/devin-cli/devin-coordinator"
    store = Store(cfg.database)
    for harness in ("codex", "devin"):
        store.bind("sample", harness, "coordinator", session if harness == "devin" else "codex-coordinator")
        store.bind("sample", harness, "inbox", f"{harness}-inbox")
    request = store.send_fanout("sample", "codex", ["devin"], "Receipt please", "test", session="codex-coordinator")
    delivery_id = request["deliveries"][0]["id"]
    for harness in ("codex", "devin"):
        item = store.claim("sample", harness, "inbox", f"{harness}-inbox", message_id=delivery_id)
        assert store.finish("sample", harness, "inbox", f"{harness}-inbox", delivery_id, item["lease"])
    with sqlite3.connect(native) as db:
        db.execute("CREATE TABLE sessions(id TEXT,working_directory TEXT,workspace_dirs TEXT,title TEXT,hidden INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE message_nodes(session_id TEXT,row_id INTEGER,chat_message TEXT,created_at REAL)")
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,0)", ("devin-coordinator", str(tmp_path), "[]", "Coordinator"))

    server = devin_tools.make_role_server(cfg, "sample")
    pickup = server._tool_manager.get_tool(devin_acp.PICKUP_TOOL).fn
    finish = server._tool_manager.get_tool(devin_acp.FINISH_TOOL).fn

    def add_native(row_id, payload):
        now = datetime.now(timezone.utc)
        message = {**payload, "metadata": {"created_at": now.isoformat()}}
        with sqlite3.connect(native) as db:
            db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)",
                       ("devin-coordinator", row_id, json.dumps(message), int(now.timestamp())))

    def call(call_id, tool, args):
        return {"role": "assistant", "tool_calls": [{"id": call_id, "name": "mcp_call_tool", "arguments": {
            "server_name": "mhb_roles_sample", "tool_name": tool, "arguments": args}}]}

    with closing(wake._ledger(store.path)) as ledger:
        assert wake._reserve(ledger, {"id": delivery_id}, "sample", "devin", "coordinator", session)[0]
    add_native(1, {"role": "user", "content": devin_acp._wake_prompt("mhb_roles_sample", delivery_id, True)})
    first_call = call("pickup", devin_acp.PICKUP_TOOL, {"notification_id": delivery_id})
    add_native(2, first_call)
    add_native(3, first_call)
    receipt = pickup(delivery_id)
    finish_args = {"notification_id": delivery_id, "lease": receipt["lease"], "reply": "Received"}
    add_native(4, call("too-early", devin_acp.FINISH_TOOL, finish_args))
    with pytest.raises(PermissionError, match="exact notification ID and pickup lease"):
        finish(**finish_args)
    add_native(5, {"role": "tool", "tool_call_id": "too-early", "content": "Rejected"})
    add_native(6, {"role": "tool", "tool_call_id": "pickup", "content": json.dumps(receipt)})
    add_native(7, call("finish", devin_acp.FINISH_TOOL, finish_args))
    result = finish(**finish_args)
    assert result["reply"]["request_id"] == request["request_id"]
    with closing(wake._ledger(store.path)) as ledger, ledger:
        ledger.execute("UPDATE mhb_wake_attempts SET state='sent'")
    with pytest.raises(PermissionError, match="no longer active"):
        finish(**finish_args)


def test_devin_role_server_registers_only_expected_native_tools(tmp_path, monkeypatch):
    import types
    class FakeServer:
        def __init__(self, name, instructions=""):
            self.name, self.instructions, self.tools = name, instructions, {}
        def tool(self, name=None):
            def register(fn):
                self.tools[name or fn.__name__] = fn
                return fn
            return register
    fake = types.ModuleType("mcp.server.fastmcp")
    fake.FastMCP = FakeServer
    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", types.ModuleType("mcp.server"))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fake)
    cfg = Config(tmp_path, {"sample": (tmp_path,)})
    server = devin_tools.make_role_server(cfg, "sample")
    assert server.name == "mhb_roles_sample"
    assert set(server.tools) == {"prepare_role_handoff", "take_coordinator", "take_bridge",
                                 "bridge_status", "pickup_notification", "acknowledge",
                                 "pickup_wake", "finish_wake", "send_message", "send_fanout", "reply_free_text", "set_pickup"}


def test_devin_fanout_tracks_native_caller_across_both_roots(tmp_path, monkeypatch):
    from multi_harness_bridge.store import Store

    pytest.importorskip("mcp")
    docs = tmp_path / "docs"
    dev = tmp_path / "dev"
    docs.mkdir()
    dev.mkdir()
    cfg = Config(tmp_path, {"sample": (docs, dev)})
    monkeypatch.setattr(devin_tools.config, "load", lambda: cfg)
    native = tmp_path / "sessions.sqlite3"
    monkeypatch.setenv("MHB_DEVIN_SESSIONS_DB", str(native))
    store = Store(cfg.database)
    for harness in ("devin", "codex", "claude"):
        for role in ("coordinator", "inbox"):
            store.bind("sample", harness, role, f"{harness}-{role}")
    store.bind("sample", "devin", "coordinator", "acp/devin-cli/old")
    with sqlite3.connect(native) as db:
        db.execute("CREATE TABLE sessions(id TEXT,working_directory TEXT,workspace_dirs TEXT,title TEXT,hidden INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE message_nodes(session_id TEXT,row_id INTEGER,chat_message TEXT,created_at REAL)")
        db.executemany("INSERT INTO sessions VALUES(?,?,?,?,0)", [("old", str(docs), "[]", "Coordinator"),
                                                                 ("new", str(dev), "[]", "Coordinator")])
    server = devin_tools.make_role_server(cfg, "sample")
    fanout = server._tool_manager.get_tool("send_fanout").fn
    args = {"recipients": ["codex", "claude"], "body": "Exact text π", "key": "native-fanout"}

    def insert_call(session, row_id):
        call = {"role": "assistant", "tool_calls": [{"id": str(row_id), "name": "mcp_call_tool", "arguments": {
            "server_name": "mhb_roles_sample", "tool_name": "send_fanout", "arguments": args}}]}
        with sqlite3.connect(native) as db:
            db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)", (session, row_id, json.dumps(call), time.time()))

    insert_call("old", 1)
    with pytest.raises(PermissionError, match="one fresh native caller"):
        fanout(**{**args, "body": "different text"})
    original = fanout(**args)
    store.bind("sample", "devin", "coordinator", "acp/devin-cli/new")
    with pytest.raises((PermissionError, ValueError), match="coordinator"):
        fanout(**args)
    with sqlite3.connect(native) as db:
        db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)",
                   ("old", 2, json.dumps({"role": "tool", "tool_call_id": "1"}), time.time()))
    insert_call("new", 3)
    assert fanout(**args) == original


def test_devin_role_takeover_challenge_binds_exact_native_session(tmp_path, monkeypatch):
    import types
    class FakeServer:
        def __init__(self, name, instructions=""):
            self.name, self.tools = name, {}
        def tool(self, name=None):
            def register(fn):
                self.tools[name or fn.__name__] = fn
                return fn
            return register
    fake = types.ModuleType("mcp.server.fastmcp")
    fake.FastMCP = FakeServer
    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", types.ModuleType("mcp.server"))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fake)
    workspace = tmp_path / "project"
    workspace.mkdir()
    cfg = Config(tmp_path / "bridge-home", {"sample": (workspace,)})
    monkeypatch.setattr(devin_tools.config, "load", lambda: cfg)
    native = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(native) as db:
        db.execute("CREATE TABLE sessions(id TEXT,working_directory TEXT,workspace_dirs TEXT,title TEXT,hidden INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE message_nodes(session_id TEXT,row_id INTEGER,chat_message TEXT,created_at REAL)")
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,0)", ("native-id", str(workspace), "[]", "chat"))
        db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)", ("native-id", 1,
            json.dumps({"role":"user","content":"take coordinator"}), time.time()))
        prepare = {"role":"assistant","tool_calls":[{"id":"prepare-id","name":"mcp_call_tool","arguments":{
            "server_name":"mhb_roles_sample","tool_name":"prepare_role_handoff","arguments":{}}}]}
        db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)", ("native-id", 2, json.dumps(prepare), time.time()))
    monkeypatch.setenv("MHB_DEVIN_SESSIONS_DB", str(native))
    server = devin_tools.make_role_server(cfg, "sample")
    prepared = server.tools["prepare_role_handoff"]()
    challenge = prepared["challenge"]
    with sqlite3.connect(native) as db:
        db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)", ("native-id", 3,
            json.dumps({"role":"tool","tool_call_id":"prepare-id","content":json.dumps(prepared)}), time.time()))
        takeover = {"role":"assistant","tool_calls":[{"id":"take-id","name":"mcp_call_tool","arguments":{
            "server_name":"mhb_roles_sample","tool_name":"take_coordinator","arguments":{"challenge":challenge}}}]}
        db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)", ("native-id", 4, json.dumps(takeover), time.time()))
    result = server.tools["take_coordinator"](challenge)
    assert result["roles"]["coordinator"] == "acp/devin-cli/native-id"
    assert result["role"] == "coordinator"
