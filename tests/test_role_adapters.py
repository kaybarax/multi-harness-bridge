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
    with pytest.raises(ValueError):
        adapters._parse("Please take coordinator", "coordinator")
    with pytest.raises(ValueError):
        adapters._parse("take bridge", "coordinator")


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
            {"type": "input_text", "text": "[$take-coordinator](/Users/kevin/workspace/tools/coordinator-bridge/skills/take-coordinator/SKILL.md) 'Picked title'"}]}},
        {"type": "response_item", "payload": {"role": "user",
            "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["skills.selected_skill_instructions"]},
            "content": [{"type": "input_text", "text": "Instructions for the selected skill"}]}})) + "\n")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,cwd TEXT,attached_thread_parent_id TEXT)")
        db.execute("INSERT INTO threads VALUES(?,?,?,NULL)", (session, str(rollout), str(workspace)))
    monkeypatch.setattr(adapters.config, "load", lambda: Config(tmp_path, {"sample": (workspace,)}))
    invocation = adapters.codex_invocation("coordinator", thread_id=session, home=home, cwd=workspace)
    assert invocation.title == "Picked title"
    assert invocation.proof == "codex-current-user-turn"


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
    assert adapters.claude_invocation("coordinator", hook).proof == "claude-user-prompt-submit"
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
