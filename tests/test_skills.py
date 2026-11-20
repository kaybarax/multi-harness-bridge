import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from multi_harness_bridge import skills
from multi_harness_bridge.config import Config


def put_skill(root: Path, name: str, text="portable instructions"):
    path = root / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\n---\n\n{text}\n")
    return path


def test_conflicting_sources_are_skipped(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    (workspace / ".agents/skills").mkdir(parents=True)
    (workspace / ".claude/skills").mkdir(parents=True)
    put_skill(workspace / ".agents/skills", "same", "one")
    put_skill(workspace / ".claude/skills", "same", "two")
    monkeypatch.setattr(skills.config, "load", lambda: Config(tmp_path, {"sample": (workspace,)}))

    result = skills.install("sample", ["codex", "claude"], "devin")

    assert result["status"] == "complete-with-conflicts"
    assert result["conflicts"][0]["status"] == "conflict-sources"
    assert not (workspace / ".agents/skills" / skills.MANIFEST).exists()


def test_codex_install_is_idempotent_and_preserves_local_edits(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    source = put_skill(workspace / ".claude/skills", "sample")
    (workspace / ".agents/skills").mkdir(parents=True)
    monkeypatch.setattr(skills.config, "load", lambda: Config(tmp_path, {"sample": (workspace,)}))

    first = skills.install("sample", ["claude"], "codex")
    second = skills.install("sample", ["claude"], "codex")
    destination = workspace / ".agents/skills/sample"
    assert first["skills"][0]["status"] == "installed"
    assert second["skills"][0]["status"] == "already-current"
    (destination / "SKILL.md").write_text("user edit")
    (source / "SKILL.md").write_text("---\nname: sample\n---\nupdated\n")
    third = skills.install("sample", ["claude"], "codex")
    assert third["skills"][0]["status"] == "conflict-local-edits"
    assert (destination / "SKILL.md").read_text() == "user edit"


def test_portability_blocks_foreign_host_mentions(tmp_path):
    skill = put_skill(tmp_path, "x", "Use the claude-only native terminal command")
    assert skills._blockers(skill, "codex")


def test_bootstrap_installs_packaged_skills_and_is_idempotent(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(skills.config, "load", lambda: Config(tmp_path, {"sample": (workspace,)}))
    first = skills.bootstrap("sample", "codex")
    second = skills.bootstrap("sample", "codex")
    assert first["status"] == second["status"] == "complete"
    assert all(item["status"] == "installed" for item in first["skills"])
    assert all(item["status"] == "already-current" for item in second["skills"])
    manifest = json.loads((workspace / ".agents/skills" / skills.MANIFEST).read_text())
    assert set(manifest) == {p.name for p in skills._skills(skills.BUILTIN_ROOT)}


def test_harness_identity_must_be_adapter_supplied(monkeypatch):
    monkeypatch.delenv("MULTI_HARNESS_BRIDGE_HARNESS", raising=False)
    for marker in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "DEVIN_SESSION_ID",
                   "DEVIN_LOCAL_SESSION_ID", "MHB_ANTIGRAVITY_EVENT"):
        monkeypatch.delenv(marker, raising=False)
    with pytest.raises(ValueError, match="trusted native harness identity"):
        skills.invoking_harness()


def test_mailbox_uses_bound_native_session_and_private_lease(tmp_path, monkeypatch):
    from multi_harness_bridge import role_adapters
    from multi_harness_bridge.store import Store

    workspace = tmp_path / "project"
    workspace.mkdir()
    db = tmp_path / "bridge.sqlite3"
    cfg = Config(tmp_path, {"sample": (workspace,)})
    monkeypatch.setattr(skills.config, "load", lambda: cfg)
    monkeypatch.setenv("MULTI_HARNESS_BRIDGE_HARNESS", "codex")
    session = "39b3a1ac-6a0d-46c2-a3b6-5404eb4c4e61"
    monkeypatch.setattr(skills, "_native_invocation", lambda host, hint=None: role_adapters.Invocation(
        "sample", "codex", session, "", workspace, proof="fixture-native"))
    store = Store(db)
    store.bind("sample", "codex", "coordinator", session)
    store.bind("sample", "codex", "inbox", "f158aa0f-aaf9-41d3-a589-fbb43793a1ea")
    original = store.send("sample", "other", "codex", "queued work", "stable-id")["id"]
    inbound = store.claim("sample", "codex", "inbox", "f158aa0f-aaf9-41d3-a589-fbb43793a1ea")
    store.finish("sample", "codex", "inbox", "f158aa0f-aaf9-41d3-a589-fbb43793a1ea", inbound["id"], inbound["lease"])
    args = SimpleNamespace(project=None, mailbox="claim", message_id=None, recipient=None,
                           body=None, key=None, reply_to=None)
    result = skills._mailbox(args, "codex", cfg)
    assert result["message"]["body"] == "queued work"
    assert "lease" not in result["message"]
    args.mailbox = "finish"
    args.message_id = result["message"]["id"]
    assert skills._mailbox(args, "codex", cfg)["finished"] is True
    args.mailbox = "send"
    args.recipient, args.body, args.key, args.reply_to = "other", "reply", "reply-key", original
    sent = skills._mailbox(args, "codex", cfg)
    with store._connect() as connection:
        row = connection.execute("SELECT sender,recipient,key,reply_to FROM messages WHERE id=?", (sent["id"],)).fetchone()
    assert tuple(row) == ("codex", "other", "reply-key", original)
