import json
import hashlib
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


def test_caller_supplied_harness_environment_is_not_identity_evidence(monkeypatch):
    monkeypatch.setenv("MULTI_HARNESS_BRIDGE_HARNESS", "codex")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)

    with pytest.raises(ValueError, match="not confirmed"):
        skills.invoking_harness()


def test_cli_auto_selects_declared_entry_from_verified_active_workspace(tmp_path, monkeypatch, capsys):
    from multi_harness_bridge import role_adapters

    docs, dev = tmp_path / "docs", tmp_path / "dev"
    docs.mkdir()
    dev.mkdir()
    entry_dir = dev / "agents/skills/workflow-entry"
    entry_dir.mkdir(parents=True)
    entry = entry_dir / "SKILL.md"
    entry.write_text("---\nname: workflow-entry\n---\nRead index.json.\n")
    procedure = entry_dir / "review.md"
    procedure.write_text("Review the current work.\n")
    index = entry_dir / "index.json"
    index.write_text(json.dumps({"version": 1, "procedures": [{
        "name": "review", "path": procedure.relative_to(dev).as_posix(),
        "sha256": hashlib.sha256(procedure.read_bytes()).hexdigest(),
    }]}))
    (dev / ".mhb").mkdir()
    (dev / ".mhb/project-skills.json").write_text(json.dumps({
        "version": 1,
        "entry": {"name": "workflow-entry", "path": entry.relative_to(dev).as_posix(),
                  "sha256": hashlib.sha256(entry.read_bytes()).hexdigest()},
        "index": {"path": index.relative_to(dev).as_posix(),
                  "sha256": hashlib.sha256(index.read_bytes()).hexdigest()},
    }))
    cfg = Config(tmp_path / "state", {"sample": (docs, dev)})
    monkeypatch.setattr(skills.config, "load", lambda: cfg)
    monkeypatch.setattr(skills, "invoking_harness", lambda: "codex")
    monkeypatch.setenv("CODEX_THREAD_ID", "native-thread")
    monkeypatch.setattr(skills, "_native_invocation", lambda host, hint=None: role_adapters.Invocation(
        "sample", "codex", "native-thread", "", dev, proof="fixture-native"))
    monkeypatch.chdir(docs)

    skills.main(["--from", "claude"])
    result = json.loads(capsys.readouterr().out)

    assert result["destination"] == str(dev / ".agents/skills/workflow-entry")
    assert (dev / ".agents/skills/workflow-entry/SKILL.md").is_file()
    assert not (docs / ".agents/skills/workflow-entry").exists()


def test_cli_workspace_hint_cannot_override_native_workspace(tmp_path, monkeypatch):
    from multi_harness_bridge import role_adapters

    docs, dev = tmp_path / "docs", tmp_path / "dev"
    docs.mkdir()
    dev.mkdir()
    cfg = Config(tmp_path / "state", {"sample": (docs, dev)})
    monkeypatch.setattr(skills.config, "load", lambda: cfg)
    monkeypatch.setattr(skills, "invoking_harness", lambda: "codex")
    monkeypatch.setenv("CODEX_THREAD_ID", "native-thread")
    monkeypatch.setattr(skills, "_native_invocation", lambda host, hint=None: role_adapters.Invocation(
        "sample", "codex", "native-thread", "", dev, proof="fixture-native"))
    monkeypatch.chdir(dev)

    with pytest.raises(SystemExit):
        skills.main(["--from", "claude", "--workspace", str(docs)])


def test_devin_workspace_comes_from_verified_native_session_adapter(tmp_path, monkeypatch):
    from multi_harness_bridge import role_adapters

    docs, dev = tmp_path / "docs", tmp_path / "dev"
    docs.mkdir()
    dev.mkdir()
    cfg = Config(tmp_path / "state", {"sample": (docs, dev)})
    monkeypatch.setattr(role_adapters, "devin_native_invocation", lambda role, project_hint=None:
                        role_adapters.Invocation("sample", "devin", "acp/devin-cli/session", role or "",
                                                 dev, proof="fixture-native"))
    monkeypatch.chdir(docs)

    project, active = skills._active_project_workspace(cfg, "devin")

    assert project == "sample"
    assert active == dev
    with pytest.raises(ValueError, match="active native workspace"):
        skills._active_project_workspace(cfg, "devin", workspace_hint=docs)


def test_explicit_bootstrap_target_keeps_shell_operator_path(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    cfg = Config(tmp_path / "state", {"sample": (root,)})
    monkeypatch.setattr(skills.config, "load", lambda: cfg)
    monkeypatch.setattr(skills, "install_native", lambda project, host, cfg: {"host": host, "status": "ok"})
    for marker in ("MULTI_HARNESS_BRIDGE_HARNESS", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID",
                   "CLAUDECODE", "DEVIN_SESSION_ID", "DEVIN_LOCAL_SESSION_ID", "MHB_ANTIGRAVITY_EVENT"):
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.chdir(root)

    skills.main(["--bootstrap", "--to", "devin"])
    result = json.loads(capsys.readouterr().out)

    assert result["destination"].endswith("/.agents/skills")
    assert result["native"] == {"host": "devin", "status": "ok"}


def test_mailbox_uses_bound_native_session_and_exact_lease(tmp_path, monkeypatch):
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
    args = SimpleNamespace(project=None, mailbox="claim", message_id=None, lease=None, recipient=None,
                           body=None, body_file=None, key=None, reply_to=None)
    result = skills._mailbox(args, "codex", cfg)
    assert result["message"]["body"] == "queued work"
    args.mailbox = "finish"
    args.message_id = result["message"]["id"]
    args.lease = result["message"]["lease"]
    assert skills._mailbox(args, "codex", cfg)["finished"] is True
    args.mailbox = "send"
    args.recipient, args.body, args.key, args.reply_to = ["other"], "reply", "reply-key", original
    sent = skills._mailbox(args, "codex", cfg)
    with store._connect() as connection:
        row = connection.execute("SELECT sender,recipient,key,reply_to FROM messages WHERE id=?", (sent["id"],)).fetchone()
    assert tuple(row) == ("codex", "other", "reply-key", original)


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_native_mailbox_finish_rejects_stale_attempt(tmp_path, monkeypatch, capsys, host):
    from multi_harness_bridge import role_adapters
    from multi_harness_bridge.store import Store

    cfg = Config(tmp_path, {"sample": (tmp_path,)})
    session = f"{host}-inbox"
    monkeypatch.setattr(skills.config, "load", lambda: cfg)
    monkeypatch.setattr(skills, "invoking_harness", lambda: host)
    monkeypatch.setattr(skills, "_native_invocation", lambda host, hint: role_adapters.Invocation(
        "sample", host, session, "", tmp_path, proof="fixture-native"))
    store = Store(cfg.database)
    for harness in (host, "devin"):
        for role in ("inbox", "coordinator"):
            store.bind("sample", harness, role, f"{harness}-{role}")
    request = store.send_fanout("sample", host, ["devin"], "dry run", "request", session=f"{host}-coordinator")
    message_id = request["deliveries"][0]["id"]

    def claim():
        skills.main(["--mailbox", "claim", "--message-id", message_id])
        return json.loads(capsys.readouterr().out)["message"]

    def finish(lease):
        skills.main(["--mailbox", "finish", "--message-id", message_id, f"--lease={lease}"])
        return json.loads(capsys.readouterr().out)["finished"]

    first = claim()
    with store._connect() as db:
        db.execute("UPDATE messages SET deadline=0 WHERE id=?", (message_id,))
    second = claim()
    assert second["attempt"] == first["attempt"] + 1
    assert second["lease"] != first["lease"]
    with pytest.raises(SystemExit):
        skills.main(["--mailbox", "finish", "--message-id", message_id])
    assert "--lease from the claim result is required" in capsys.readouterr().err
    assert finish(first["lease"]) is False
    assert finish(second["lease"]) is True
    assert store.trace("sample", message_id)["stage"] == "inbox"


def test_native_mailbox_can_finish_a_claim_cached_before_upgrade(tmp_path, monkeypatch):
    import hashlib

    from multi_harness_bridge import role_adapters
    from multi_harness_bridge.store import Store

    cfg = Config(tmp_path, {"sample": (tmp_path,)})
    session = "codex-inbox"
    monkeypatch.setattr(skills, "_native_invocation", lambda host, hint: role_adapters.Invocation(
        "sample", host, session, "", tmp_path, proof="fixture-native"))
    store = Store(cfg.database)
    store.bind("sample", "codex", "coordinator", "codex-coordinator")
    store.bind("sample", "codex", "inbox", session)
    message_id = store.send("sample", "devin", "codex", "existing work", "request")["id"]
    item = store.claim("sample", "codex", "inbox", session, message_id=message_id)
    scope = "\0".join(("sample", "codex", "inbox", session, message_id))
    previous_claim = cfg.root / "leases" / (hashlib.sha256(scope.encode()).hexdigest() + ".json")
    previous_claim.parent.mkdir()
    previous_claim.write_text(json.dumps({"id": message_id, "lease": item["lease"]}))
    args = SimpleNamespace(project=None, mailbox="finish", message_id=message_id, lease=None)
    assert skills._mailbox(args, "codex", cfg)["finished"] is True
    assert not previous_claim.exists()


def test_native_cli_multiline_fanout_and_reply(tmp_path, monkeypatch, capsys):
    import io
    import sys

    from multi_harness_bridge import role_adapters
    from multi_harness_bridge.store import Store

    cfg = Config(tmp_path, {"sample": (tmp_path,)})
    monkeypatch.setattr(skills.config, "load", lambda: cfg)
    monkeypatch.setattr(skills, "invoking_harness", lambda: "codex")
    monkeypatch.setattr(skills, "_native_invocation", lambda host, hint: role_adapters.Invocation(
        "sample", host, f"{host}-coordinator", "", tmp_path, proof="fixture-native"))
    store = Store(cfg.database)
    for harness in ("codex", "claude", "devin"):
        for role in ("inbox", "coordinator"):
            store.bind("sample", harness, role, f"{harness}-{role}")
    body = "Exact ‘quoted’ text\r\n$literal\n"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(body.encode("utf-8"))))
    skills.main(["--mailbox", "send-fanout", "--recipient", "claude", "--recipient", "devin",
                 "--body-file", "-", "--key", "request-key"])
    request = json.loads(capsys.readouterr().out)
    assert request["project"] == "sample"
    delivery = next(item for item in request["deliveries"] if item["recipient"] == "claude")
    for harness, role in (("codex", "inbox"), ("claude", "inbox"), ("claude", "coordinator")):
        session = f"{harness}-{role}"
        item = store.claim("sample", harness, role, session, message_id=delivery["id"])
        assert item["body"] == body
        assert store.finish("sample", harness, role, session, item["id"], item["lease"])
    monkeypatch.setattr(skills, "invoking_harness", lambda: "claude")
    skills.main(["--mailbox", "reply", "--reply-to", delivery["id"], "--body", "Received",
                 "--key", "reply-key"])
    reply = json.loads(capsys.readouterr().out)
    assert reply["request_id"] == request["request_id"]


def _relay_fixture(tmp_path, monkeypatch, host="codex", option=True):
    """Bound codex pair with one inbox-stage delivery addressed to the harness."""
    from multi_harness_bridge import config, role_adapters
    from multi_harness_bridge.store import Store

    workspace = tmp_path / "project"
    workspace.mkdir()
    cfg = Config(tmp_path, {"sample": (workspace,)})
    monkeypatch.setattr(skills.config, "load", lambda: cfg)
    monkeypatch.setenv("MULTI_HARNESS_BRIDGE_HARNESS", host)
    inbox_session = f"{host}-inbox-session"
    coordinator_session = f"{host}-coordinator-session"
    monkeypatch.setattr(skills, "_native_invocation", lambda host_, hint=None: role_adapters.Invocation(
        "sample", host, inbox_session, "", workspace, proof="fixture-native"))
    store = Store(cfg.database)
    store.bind("sample", host, "coordinator", coordinator_session)
    store.bind("sample", host, "inbox", inbox_session)
    if option:
        config.set_project_option(cfg.root, "sample", "codex", "coordinator_wake", "agent-relay")
    return cfg, store, inbox_session, coordinator_session


def _claim_and_finish(cfg, store, message_id, host="codex"):
    args = SimpleNamespace(project=None, mailbox="claim", message_id=message_id, lease=None,
                           recipient=None, body=None, body_file=None, key=None, reply_to=None)
    claimed = skills._mailbox(args, host, cfg)
    args.mailbox = "finish"
    args.message_id = claimed["message"]["id"]
    args.lease = claimed["message"]["lease"]
    return skills._mailbox(args, host, cfg)


def test_codex_inbox_finish_emits_wake_relay_without_confirming(tmp_path, monkeypatch):
    from multi_harness_bridge import wake
    from multi_harness_bridge.wake import WAKE_PROMPT, WAKE_RELAY_PROMPT

    cfg, store, _, coordinator_session = _relay_fixture(tmp_path, monkeypatch)
    message_id = store.send("sample", "devin", "codex", "queued work", "stable-id")["id"]
    result = _claim_and_finish(cfg, store, message_id)
    assert result["finished"] is True
    # The relay uses the reviewer-safe prompt variant, not the queue wording.
    assert result["wake_relay"] == {"tool": "send_message_to_thread",
                                    "threadId": coordinator_session,
                                    "prompt": WAKE_RELAY_PROMPT.format(notification_id=message_id)}
    assert result["wake_relay"]["prompt"] != WAKE_PROMPT.format(notification_id=message_id)
    assert "bridge-mailbox" in result["wake_relay"]["prompt"]
    assert store.trace("sample", message_id)["stage"] == "coordinator"
    # Finish alone must not suppress the queue fallback; the dispatcher still
    # holds the fresh wake in the relay grace window.
    monkeypatch.setattr(wake, "_default_wake",
                        lambda *_, **__: pytest.fail("fallback dispatched during grace"))
    assert wake.wake_one(store, "sample", "codex", "coordinator",
                         coordinator_session, commands={}) == {
        "status": "relay-pending", "id": message_id}


def _relay_confirm(cfg, store, message_id, host="codex"):
    args = SimpleNamespace(project=None, mailbox="relay-confirm", message_id=message_id,
                           lease=None, recipient=None, body=None, body_file=None,
                           key=None, reply_to=None)
    return skills._mailbox(args, host, cfg)


def test_relay_confirm_records_sent_and_suppresses_fallback(tmp_path, monkeypatch):
    import sqlite3

    from multi_harness_bridge import wake

    cfg, store, _, coordinator_session = _relay_fixture(tmp_path, monkeypatch)
    message_id = store.send("sample", "devin", "codex", "queued work", "stable-id")["id"]
    _claim_and_finish(cfg, store, message_id)
    result = _relay_confirm(cfg, store, message_id)
    assert result == {"project": "sample", "role": "inbox",
                      "message_id": message_id, "relay_confirmed": True}
    with sqlite3.connect(cfg.database) as db:
        row = db.execute("SELECT state FROM mhb_wake_attempts WHERE notification_id=?",
                         (message_id,)).fetchone()
    assert row == ("sent",)
    monkeypatch.setattr(wake, "_default_wake",
                        lambda *_, **__: pytest.fail("fallback dispatched after confirm"))
    assert wake.wake_one(store, "sample", "codex", "coordinator",
                         coordinator_session, commands={}) == {"id": message_id, "status": "sent"}
    # Confirming twice is idempotent.
    assert _relay_confirm(cfg, store, message_id)["relay_confirmed"] is True


def test_relay_confirm_rejects_wrong_stage_missing_and_unbound(tmp_path, monkeypatch):
    from multi_harness_bridge import role_adapters, wake

    cfg, store, inbox_session, coordinator_session = _relay_fixture(tmp_path, monkeypatch)
    message_id = store.send("sample", "devin", "codex", "queued work", "stable-id")["id"]
    # Still at inbox stage: not awaiting the coordinator.
    with pytest.raises(ValueError, match="not awaiting"):
        _relay_confirm(cfg, store, message_id)
    # Unknown message id.
    with pytest.raises(ValueError, match="not awaiting"):
        _relay_confirm(cfg, store, "99999999-9999-4999-8999-999999999999")
    # A session with no binding cannot confirm.
    monkeypatch.setattr(skills, "_native_invocation", lambda host, hint=None: role_adapters.Invocation(
        "sample", "codex", "unbound-session", "", tmp_path / "project", proof="fixture-native"))
    with pytest.raises(ValueError, match="not bound"):
        _relay_confirm(cfg, store, message_id)
    monkeypatch.setattr(skills, "_native_invocation", lambda host, hint=None: role_adapters.Invocation(
        "sample", "codex", inbox_session, "", tmp_path / "project", proof="fixture-native"))
    _claim_and_finish(cfg, store, message_id)
    # Every rejected confirm left the fallback wake eligible in the grace window.
    monkeypatch.setattr(wake, "_default_wake",
                        lambda *_, **__: pytest.fail("fallback dispatched during grace"))
    assert wake.wake_one(store, "sample", "codex", "coordinator",
                         coordinator_session, commands={}) == {
        "status": "relay-pending", "id": message_id}


def test_relay_confirm_rejects_non_codex_host(tmp_path, monkeypatch):
    cfg, store, _, _ = _relay_fixture(tmp_path, monkeypatch, host="claude")
    message_id = store.send("sample", "devin", "claude", "queued work", "stable-id")["id"]
    _claim_and_finish(cfg, store, message_id, host="claude")
    with pytest.raises(ValueError, match="only valid for the codex harness"):
        _relay_confirm(cfg, store, message_id, host="claude")


def test_mailbox_finish_omits_wake_relay_when_option_off(tmp_path, monkeypatch):
    cfg, store, _, _ = _relay_fixture(tmp_path, monkeypatch, option=False)
    message_id = store.send("sample", "devin", "codex", "queued work", "stable-id")["id"]
    result = _claim_and_finish(cfg, store, message_id)
    assert result["finished"] is True and "wake_relay" not in result


def test_mailbox_finish_omits_wake_relay_for_non_codex_host(tmp_path, monkeypatch):
    cfg, store, _, _ = _relay_fixture(tmp_path, monkeypatch, host="claude")
    message_id = store.send("sample", "devin", "claude", "queued work", "stable-id")["id"]
    result = _claim_and_finish(cfg, store, message_id, host="claude")
    assert result["finished"] is True and "wake_relay" not in result


def test_mailbox_finish_outbound_stage_does_not_relay(tmp_path, monkeypatch):
    cfg, store, _, coordinator_session = _relay_fixture(tmp_path, monkeypatch)
    store.bind("sample", "devin", "coordinator", "devin-coordinator")
    store.bind("sample", "devin", "inbox", "devin-inbox")
    request = store.send_fanout("sample", "codex", ["devin"], "fanout body", "req-key",
                                session=coordinator_session)
    delivery_id = request["deliveries"][0]["id"]
    result = _claim_and_finish(cfg, store, delivery_id)
    # The codex inbox finish moves outbound -> inbox; the coordinator is not next.
    assert result["finished"] is True and "wake_relay" not in result
    assert store.trace("sample", delivery_id)["stage"] == "inbox"
