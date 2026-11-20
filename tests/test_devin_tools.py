import json
import sqlite3
import sys
import time

from multi_harness_bridge.config import Config
from multi_harness_bridge import devin_tools


def test_devin_manifest_registration_preserves_entries_and_is_idempotent(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    (project_root / ".devin").mkdir(parents=True)
    manifest = project_root / ".devin/mcp_config.local.json"
    manifest.write_text(json.dumps({"mcpServers": {"existing": {"command": "keep", "args": []}}}))
    cfg = Config(tmp_path / "bridge-home", {"sample": (project_root,)})
    first = devin_tools.install(cfg, "sample")
    second = devin_tools.install(cfg, "sample")
    data = json.loads(manifest.read_text())
    assert first["status"] == "installed"
    assert second["status"] == "already-current"
    assert data["mcpServers"]["existing"] == {"command": "keep", "args": []}
    assert data["mcpServers"]["mhb_roles_sample"]["command"] == sys.executable


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
                                 "send_message", "set_pickup"}


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
