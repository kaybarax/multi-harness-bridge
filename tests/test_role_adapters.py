import json
import sqlite3
import io
import os
import stat
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from multi_harness_bridge import role_adapters as adapters
from multi_harness_bridge.config import Config
from multi_harness_bridge.store import Store


def test_parse_requires_exact_person_command_and_preserves_title():
    assert adapters._parse('take coordinator "A useful title"', "coordinator") == "A useful title"
    assert adapters._parse("take bridge", "inbox") is None
    assert adapters._parse_details("take coordinator -orc", "coordinator") == (None, True)
    assert adapters._parse_details("take coordinator -orc 'A title'", "coordinator") == ("A title", True)
    assert adapters._parse_details('take coordinator -orc\t"A title"', "coordinator") == ("A title", True)
    assert adapters._parse_details('take coordinator "A title" -orc', "coordinator") == ("A title", True)
    assert adapters._parse_details('take coordinator "-orc"', "coordinator") == ("-orc", False)
    with pytest.raises(ValueError):
        adapters._parse("Please take coordinator", "coordinator")
    with pytest.raises(ValueError):
        adapters._parse("take bridge", "coordinator")
    with pytest.raises(ValueError, match="only valid for coordinator"):
        adapters._parse_details("take bridge -orc", "inbox")


def test_bind_uses_verified_context_and_store(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(adapters.config, "load", lambda: Config(tmp_path, {"sample": (workspace,)}))
    session = "39b3a1ac-6a0d-46c2-a3b6-5404eb4c4e61"
    invocation = adapters.Invocation("sample", "codex", session, "coordinator", workspace,
                                     proof="native-thread-and-current-user-turn")
    result = adapters.bind(Store(tmp_path / "bridge.sqlite3"), invocation)
    assert result["coordinator"] == session
    assert result["inbox"] is None
    assert result["orchestrator"]["status"] == "never_designated"


def test_codex_picker_mention_uses_user_command_and_skips_selected_skill_instructions(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    home = tmp_path / "codex"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    session = "39b3a1ac-6a0d-46c2-a3b6-5404eb4c4e61"
    rollout = sessions / "thread.jsonl"
    rollout.write_text("\n".join(json.dumps(item) for item in (
        {"type": "response_item", "payload": {"role": "user", "content": [
            {"type": "input_text", "text": "[$take-coordinator](/example/workspace/skills/take-coordinator/SKILL.md) 'Picked title'"}]}},
        {"type": "response_item", "payload": {"role": "user",
            "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["skills.selected_skill_instructions"]},
            "content": [{"type": "input_text", "text": "Instructions for the selected skill"}]}})) + "\n")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,cwd TEXT,attached_thread_parent_id TEXT)")
        db.execute("INSERT INTO threads VALUES(?,?,?,NULL)", (session, str(rollout), str(workspace)))
    monkeypatch.setattr(adapters.config, "load", lambda: Config(tmp_path, {"sample": (workspace,)}))
    invocation = adapters.codex_invocation("coordinator", thread_id=session, home=home, cwd=workspace)
    assert invocation.title == "Picked title"
    assert invocation.orchestrator is False
    assert invocation.proof == "codex-current-user-turn"
    rollout.write_text(rollout.read_text().replace("'Picked title'", "-orc 'Picked title'"))
    flagged = adapters.codex_invocation("coordinator", thread_id=session, home=home, cwd=workspace)
    assert flagged.title == "Picked title"
    assert flagged.orchestrator is True
    assert flagged.invocation_key == adapters.codex_invocation(
        "coordinator", thread_id=session, home=home, cwd=workspace).invocation_key


def test_claude_requires_native_hook_context(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    session = "39b3a1ac-6a0d-46c2-a3b6-5404eb4c4e61"
    transcript = workspace / f"{session}.jsonl"
    transcript.write_text(json.dumps({"sessionId": session}) + "\n")
    monkeypatch.setattr(adapters.config, "load", lambda: Config(tmp_path, {"sample": (workspace,)}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", SimpleNamespace(project_key_for_directory=lambda path: "fixture"))
    transcript_dir = tmp_path / ".claude/projects/fixture"
    transcript_dir.mkdir(parents=True)
    moved_transcript = transcript_dir / transcript.name
    transcript.rename(moved_transcript)
    transcript = moved_transcript
    hook = {"hook_event_name": "UserPromptSubmit", "session_id": session,
            "prompt": "take coordinator", "cwd": str(workspace), "transcript_path": str(transcript)}
    plain = adapters.claude_invocation("coordinator", hook)
    assert plain.proof == "claude-user-prompt-submit"
    assert plain.orchestrator is False
    hook["prompt"] = 'take coordinator "-orc" -orc'
    flagged = adapters.claude_invocation("coordinator", hook)
    assert flagged.title == "-orc"
    assert flagged.orchestrator is True
    assert flagged.invocation_key == adapters.claude_invocation("coordinator", hook).invocation_key
    with pytest.raises(ValueError, match="current native caller"):
        adapters.claude_invocation("coordinator", hook, expected_session="other-session")
    hook["hook_event_name"] = "Stop"
    with pytest.raises(ValueError, match="UserPromptSubmit"):
        adapters.claude_invocation("coordinator", hook)


def test_antigravity_resolves_native_summary_database(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    db_path = tmp_path / "summaries.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE conversation_summaries(conversation_id TEXT,title TEXT,workspace_uris TEXT,project_id TEXT,app_data_dir TEXT)")
        db.execute("INSERT INTO conversation_summaries VALUES(?,?,?,?,?)", ("c-123", "native title", json.dumps([str(workspace)]), "pid", "antigravity"))
    monkeypatch.setattr(adapters.config, "load", lambda: Config(tmp_path, {"sample": (workspace,)}))
    invocation = adapters.antigravity_native_invocation("coordinator", {"conversationId": "c-123", "lastUserInput": "take coordinator"}, summaries_db=db_path)
    assert invocation.project == "sample"
    assert invocation.session == "c-123"
    assert invocation.proof == "antigravity-local-native-summary"


def test_devin_native_invocation_preserves_orchestrator_proof_and_title(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    native = tmp_path / "devin.sqlite3"
    with sqlite3.connect(native) as db:
        db.execute("CREATE TABLE sessions(id TEXT,working_directory TEXT,workspace_dirs TEXT,last_activity_at REAL,hidden INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE message_nodes(session_id TEXT,row_id INTEGER,chat_message TEXT)")
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,0)", ("native", str(workspace), "[]", __import__("time").time()))
        db.execute("INSERT INTO message_nodes VALUES(?,?,?)", ("native", 1,
            json.dumps({"role":"user", "content":"take coordinator -orc 'Board lead'"})))
    monkeypatch.setattr(adapters.config, "load", lambda: Config(tmp_path, {"sample": (workspace,)}))
    invocation = adapters.devin_native_invocation("coordinator", native_db=native)
    assert invocation.orchestrator is True
    assert invocation.title == "Board lead"
    assert invocation.proof == "devin-fresh-native-user-message"
    assert invocation.invocation_key


def test_devin_adapter_requires_stable_turn_identity_for_orchestrator(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(adapters.config, "load", lambda: Config(tmp_path, {"sample": (workspace,)}))
    args = {"role": "coordinator", "session_id": "acp/devin-cli/native",
            "workspace": workspace, "direct_user_prompt": "take coordinator -orc"}
    with pytest.raises(ValueError, match="stable native user-turn identity"):
        adapters.devin_invocation(**args)
    invocation = adapters.devin_invocation(**args, native_turn_id="user-row-8")
    assert invocation.orchestrator is True
    assert invocation.invocation_key


def test_antigravity_invocation_requires_native_event_for_orchestrator(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(adapters.config, "load", lambda: Config(tmp_path, {"sample": (workspace,)}))
    from multi_harness_bridge import ag_wake
    ag_home = tmp_path / "antigravity"
    monkeypatch.setattr(ag_wake, "_summary_path", lambda: ag_home / "conversation_summaries.db")
    session = "eec53df3-e61e-4b61-a208-aa633a2de99a"
    brain = ag_home / "brain" / session / ".system_generated/logs"
    brain.mkdir(parents=True)
    (brain / "transcript.jsonl").write_text(json.dumps({
        "step_index": 7, "source": "USER_EXPLICIT", "type": "USER_INPUT",
        "status": "DONE", "created_at": "2026-11-24T00:00:00Z",
        "content": "take coordinator -orc \"Board lead\"",
    }) + "\n")
    native = lambda conversation: {"conversationId": conversation, "workspace": str(workspace)}
    invocation = adapters.antigravity_invocation("coordinator", {
        "conversationId": session,
        "lastUserInput": "take coordinator -orc \"Board lead\"",
    }, resolve_native_conversation=native)
    assert invocation.orchestrator is True
    assert invocation.title == "Board lead"
    assert invocation.proof == "antigravity-native-event-and-conversation"
    assert invocation.invocation_key == f"antigravity:{session}:7"


def test_claude_hook_install_preserves_other_settings_and_is_idempotent(tmp_path):
    workspace = tmp_path / "project"
    settings_dir = workspace / ".claude"
    settings_dir.mkdir(parents=True)
    settings = settings_dir / "settings.local.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {"Stop": [{"hooks": []}]}}))
    first = adapters.install_claude_hook(workspace)
    second = adapters.install_claude_hook(workspace)
    value = json.loads(settings.read_text())
    assert first["status"] == "installed"
    assert second["status"] == "already-current"
    assert value["permissions"]["allow"] == ["Read"]
    assert value["hooks"]["Stop"] == [{"hooks": []}]
    assert len(value["hooks"]["UserPromptSubmit"]) == 1


def test_claude_hook_context_is_partitioned_by_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MHB_HOME", str(tmp_path / "bridge-home"))
    session = "39b3a1ac-6a0d-46c2-a3b6-5404eb4c4e61"
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", SimpleNamespace(project_key_for_directory=lambda path: "fixture"))
    transcript_dir = tmp_path / ".claude/projects/fixture"
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / f"{session}.jsonl"
    transcript.write_text(json.dumps({"sessionId": session}) + "\n")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name":"UserPromptSubmit",
        "session_id":session,"cwd":str(workspace),"transcript_path":str(transcript),
        "prompt":"take coordinator"})))
    assert adapters.claude_hook_main() == 0
    path = tmp_path / "bridge-home/native-context" / ("claude-" + __import__("hashlib").sha256(session.encode()).hexdigest()[:32] + ".json")
    assert json.loads(path.read_text())["session_id"] == session
    assert path.stat().st_mode & 0o777 == 0o600


def test_claude_hook_explicit_project_home_overrides_ambient_environment(tmp_path, monkeypatch):
    import shlex

    from multi_harness_bridge import config

    cfg_root = tmp_path / "private bridge state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config.register("example-project", (workspace,), cfg_root)
    ambient = tmp_path / "different-home"
    monkeypatch.setenv("MHB_HOME", str(ambient))
    monkeypatch.setattr(adapters, "_native_claude_hook_payload", lambda payload: True)
    settings = workspace / ".claude/settings.local.json"
    settings.parent.mkdir()
    old = {"matcher": "*", "hooks": [
        {"type": "command", "command": "multi-harness-claude-hook", "timeout": 5},
    ]}
    unrelated = {"matcher": "custom", "hooks": [{"type": "command", "command": "/other/hook"}]}
    settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [old, unrelated]}}))
    assert adapters.install_claude_hook(workspace, state_home=cfg_root)["status"] == "installed"
    active, generated = json.loads(settings.read_text())["hooks"]["UserPromptSubmit"]
    assert active == unrelated
    argv = shlex.split(generated["hooks"][0]["command"])
    assert argv[1:] == ["--home", str(cfg_root)]
    assert adapters.install_claude_hook(workspace, state_home=cfg_root)["status"] == "already-current"

    session = "39b3a1ac-6a0d-46c2-a3b6-5404eb4c4e61"
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": session, "cwd": str(workspace)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert adapters.claude_hook_cli(argv[1:]) == 0
    assert len(list((cfg_root / "native-context").glob("*.json"))) == 1
    assert not ambient.exists()
    payload["cwd"] = str(tmp_path)
    payload["session_id"] = "0963485c-9244-411d-b693-0ec90461ef64"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert adapters.claude_hook_cli(argv[1:]) == 0
    assert len(list((cfg_root / "native-context").glob("*.json"))) == 1


def test_claude_hook_ignores_foreign_host_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("MHB_HOME", str(tmp_path / "bridge-home"))
    session = "39b3a1ac-6a0d-46c2-a3b6-5404eb4c4e61"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name":"UserPromptSubmit",
        "session_id":session,"cwd":str(tmp_path),"transcript_path":str(tmp_path/f"{session}.jsonl"),
        "prompt":"take coordinator"})))
    assert adapters.claude_hook_main() == 0
    assert not (tmp_path / "bridge-home/native-context").exists()


def test_codex_title_uses_native_app_server_and_reads_back(tmp_path, monkeypatch):
    session = "39b3a1ac-6a0d-46c2-a3b6-5404eb4c4e61"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    with sqlite3.connect(codex_home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,name TEXT)")
        db.execute("INSERT INTO threads VALUES(?,?)", (session, "old"))
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    fake = binary_dir / "codex"
    fake.write_text("#!/usr/bin/env python3\nimport json,os,sqlite3,sys\n"
                    "for line in sys.stdin:\n"
                    " m=json.loads(line)\n"
                    " if m.get('id')==2:\n"
                    "  p=os.path.join(os.environ['CODEX_HOME'],'state_5.sqlite')\n"
                    "  with sqlite3.connect(p) as d:d.execute('UPDATE threads SET name=? WHERE id=?',(m['params']['name'],m['params']['threadId']))\n"
                    " if 'id' in m: print(json.dumps({'id':m['id'],'result':{}}),flush=True)\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    invocation = adapters.Invocation("sample", "codex", session, "coordinator", tmp_path,
                                     title="Verified name", proof="fixture")
    assert adapters.apply_native_title(invocation) == {"status":"verified", "title":"Verified name"}
