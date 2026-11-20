"""Fail-closed adapters for native, person-invoked role changes.

Host adapters mint :class:`Invocation` only from native current-turn evidence.
The common operation never accepts a target session ID from a skill/tool input.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import select
import shlex
import shutil
import subprocess
import sys
import time
import uuid

from . import config

HARNESS_NAMES = {"codex", "claude", "devin", "antigravity"}
_COMMAND = re.compile(r'(?:[/\$]?take[- ](coordinator|bridge))(?:[ \t]+(.*))?', re.I)


@dataclass(frozen=True)
class Invocation:
    project: str
    harness: str
    session: str
    role: str
    workspace: Path
    title: str | None = None
    proof: str = ""


def _parse(prompt: str, role: str):
    if role is None:
        return None
    if role not in {"coordinator", "inbox", "bridge"} or not isinstance(prompt, str):
        raise ValueError("A direct native role command is required")
    match = _COMMAND.fullmatch(prompt.strip())
    expected = "bridge" if role in {"inbox", "bridge"} else "coordinator"
    if not match or match[1].lower() != expected:
        raise ValueError("The current user turn must explicitly invoke this role")
    value = match[2]
    if value is None:
        return None
    pairs = {'"': '"', "'": "'", "“": "”", "‘": "’"}
    if len(value) < 3 or value[0] not in pairs or value[-1] != pairs[value[0]] or not value[1:-1].strip() or len(value[1:-1]) > 240:
        raise ValueError("Optional title must be enclosed in matching quotes and contain 1–240 characters")
    return value[1:-1]


def _resolve(workspace, hint=None):
    cfg = config.load()
    path = Path(workspace).expanduser().resolve(strict=True)
    project = cfg.resolve(path, hint)
    return project, path


def bind(store, invocation: Invocation):
    """Apply one verified invocation; adapters must be the only constructors."""
    if invocation.harness not in HARNESS_NAMES or invocation.role not in {"coordinator", "inbox"} or not invocation.proof:
        raise ValueError("Native role evidence is incomplete")
    project, workspace = _resolve(invocation.workspace, invocation.project)
    if project != invocation.project:
        raise ValueError("Native workspace does not resolve to the requested project")
    # All supported native UUID hosts use UUID session IDs. Devin's native ACP
    # identifier remains opaque but must have its documented provider prefix.
    session = invocation.session
    if invocation.harness == "devin":
        if not isinstance(session, str) or not session.startswith("acp/devin-cli/") or len(session) > 256:
            raise ValueError("Invalid native Devin session identity")
    else:
        try:
            session = str(uuid.UUID(session))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid native chat session identity") from None
    store.bind(project, invocation.harness, invocation.role, session, take=True)
    return {"project": project, "harness": invocation.harness, "role": invocation.role,
            "session": session, **store.roles(project, invocation.harness)}


def apply_native_title(invocation: Invocation, *, codex_home=None):
    """Apply an optional title through a host API and read it back exactly."""
    if not invocation.title:
        return {"status": "unchanged"}
    if invocation.harness == "claude":
        try:
            from claude_agent_sdk import get_session_info, rename_session
            rename_session(invocation.session, invocation.title, directory=str(invocation.workspace))
            info = get_session_info(invocation.session, directory=str(invocation.workspace))
        except (ImportError, OSError, RuntimeError) as exc:
            return {"status": "unsupported-or-failed", "reason": type(exc).__name__}
        if info is not None and info.session_id == invocation.session and info.custom_title == invocation.title:
            return {"status": "verified", "title": invocation.title}
        return {"status": "verification-failed"}
    if invocation.harness == "devin":
        try:
            from .devin_acp import rename
            result = rename(invocation.session, invocation.workspace, invocation.title)
            return {"status": "verified", "title": result}
        except (ImportError, OSError, RuntimeError, ValueError, TimeoutError) as exc:
            return {"status": "unsupported-or-failed", "reason": type(exc).__name__}
    if invocation.harness == "antigravity":
        try:
            from .ag_wake import rename
            result = rename(invocation.project, invocation.role, invocation.session, invocation.title)
            return {"status": "verified", "title": result}
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            return {"status": "unsupported-or-failed", "reason": type(exc).__name__}
    if invocation.harness != "codex":
        return {"status": "unsupported-native-title-api"}
    home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    command = ["codex", "app-server", "--stdio"]
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
        output_buffer = bytearray()
        def send(value):
            process.stdin.write((json.dumps(value) + "\n").encode("utf-8"))
            process.stdin.flush()
        def wait_response(request_id):
            nonlocal output_buffer
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if b"\n" not in output_buffer:
                    ready, _, _ = select.select([process.stdout.fileno()], [], [], max(0, deadline-time.monotonic()))
                    if not ready:
                        break
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        break
                    output_buffer.extend(data)
                while b"\n" in output_buffer:
                    line, _, rest = output_buffer.partition(b"\n")
                    output_buffer = bytearray(rest)
                    message = json.loads(line.decode("utf-8"))
                    if message.get("id") == request_id:
                        if message.get("error"):
                            raise RuntimeError("Codex app-server rejected title update")
                        return message.get("result")
            raise RuntimeError("Codex app-server did not confirm title update")
        send({"method":"initialize","id":1,"params":{"clientInfo":{"name":"multi-harness-bridge","version":"0.1"}}})
        wait_response(1)
        send({"method":"initialized","params":{}})
        send({"method":"thread/name/set","id":2,"params":{"threadId":invocation.session,"name":invocation.title}})
        wait_response(2)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        return {"status": "unsupported-or-failed", "reason": type(exc).__name__}
    finally:
        try:
            process.terminate()
            process.wait(timeout=2)
        except (UnboundLocalError, OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except (UnboundLocalError, OSError):
                pass
    try:
        with sqlite3.connect(f"file:{home / 'state_5.sqlite'}?mode=ro", uri=True) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
            if not {"id", "name"} <= columns:
                return {"status": "verification-failed"}
            row = db.execute("SELECT name FROM threads WHERE id=?", (invocation.session,)).fetchone()
    except sqlite3.Error:
        return {"status": "verification-failed"}
    return {"status": "verified", "title": invocation.title} if row and row[0] == invocation.title else {"status": "verification-failed"}


def _codex_skill_text(text):
    """Normalize a standalone Codex picker mention, preserving an optional title."""
    match = re.fullmatch(
        r'\[\$(take-(?:bridge|coordinator))\]\((?:[^\n]+[/\\])?\1[/\\]SKILL\.md\)([ \t]+.*)?',
        text.strip(),
    )
    return match[1] + (match[2] or "") if match else text


def codex_invocation(role=None, *, thread_id=None, home=None, cwd=None, project_hint=None):
    """Verify the current Codex top-level thread and latest direct user command."""
    thread_id = thread_id or os.environ.get("CODEX_THREAD_ID")
    codex_home = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    if not isinstance(thread_id, str):
        raise ValueError("Codex current thread identity is unavailable")
    try:
        thread_id = str(uuid.UUID(thread_id))
        db = codex_home / "state_5.sqlite"
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
            required = {"id", "rollout_path", "cwd", "attached_thread_parent_id"}
            if not required <= columns:
                raise ValueError("Unsupported Codex native state schema")
            row = conn.execute("SELECT rollout_path,cwd,attached_thread_parent_id FROM threads WHERE id=?", (thread_id,)).fetchone()
        if not row or not row[0] or not row[1] or row[2] is not None:
            raise ValueError("Codex thread is not a loaded top-level conversation")
        rollout = Path(row[0]).resolve(strict=True)
        native_root = (codex_home / "sessions").resolve(strict=True)
        workspace = Path(row[1]).resolve(strict=True)
        actual = Path(cwd or os.getcwd()).resolve(strict=True)
        if not rollout.is_relative_to(native_root) or workspace != actual:
            raise ValueError("Codex session or workspace does not match the current native conversation")
        latest = ""
        with rollout.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = item.get("payload", {})
                if item.get("type") == "response_item" and isinstance(payload, dict) and payload.get("role") == "user":
                    metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
                    if metadata.get("content_item_kinds") == ["skills.selected_skill_instructions"]:
                        continue
                    content = payload.get("content", "")
                    if isinstance(content, str):
                        latest = content
                    elif isinstance(content, list):
                        latest = "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
        title = _parse(_codex_skill_text(latest), role)
        project, _ = _resolve(workspace, project_hint)
        return Invocation(project, "codex", thread_id, role or "", workspace, title, "codex-current-user-turn")
    except (OSError, sqlite3.Error, KeyError, TypeError) as exc:
        raise ValueError(f"Codex native invocation could not be verified ({type(exc).__name__})") from None


def claude_invocation(role, hook_context, *, project_hint=None, cwd=None, expected_session=None):
    """Verify an actual Claude Code UserPromptSubmit hook payload."""
    if not isinstance(hook_context, dict) or hook_context.get("hook_event_name") != "UserPromptSubmit":
        raise ValueError("A Claude Code UserPromptSubmit hook payload is required")
    session, prompt = hook_context.get("session_id"), hook_context.get("prompt")
    if expected_session is not None and session != expected_session:
        raise ValueError("Claude hook session does not match the current native caller")
    workspace = Path(hook_context.get("cwd", "")).expanduser().resolve(strict=True)
    if cwd is not None and Path(cwd).resolve(strict=True) != workspace:
        raise ValueError("Claude hook workspace differs from current directory")
    transcript = Path(hook_context.get("transcript_path", "")).expanduser().resolve(strict=True)
    if not transcript.is_file() or transcript.stem != session:
        raise ValueError("Claude transcript does not match the native session")
    try:
        with transcript.open(encoding="utf-8") as stream:
            confirmed = any(json.loads(line).get("sessionId") == session for line in stream if line.strip())
    except (OSError, json.JSONDecodeError, AttributeError):
        confirmed = False
    if not confirmed:
        raise ValueError("Claude transcript content does not confirm the hook session")
    config_root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser().resolve()
    try:
        from claude_agent_sdk import project_key_for_directory
        expected_parent = config_root / "projects" / project_key_for_directory(workspace)
    except (ImportError, AttributeError):
        raise ValueError("Claude native project path verifier is unavailable") from None
    if transcript.parent != expected_parent or not transcript.is_relative_to(config_root / "projects"):
        raise ValueError("Claude transcript is not in the native project session directory")
    _parse(prompt, role)
    project, _ = _resolve(workspace, project_hint)
    return Invocation(project, "claude", session, role or "", workspace, _parse(prompt, role), "claude-user-prompt-submit")


def claude_hook_main(state_home=None):
    """Claude UserPromptSubmit hook: persist short-lived native caller context."""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit":
            return 0
        # User-level hooks can be inherited by other hosts. Ignore payloads
        # without Claude Code's native transcript identity; the CLI re-verifies
        # the same transcript/session before using this short-lived context.
        if not _native_claude_hook_payload(payload):
            return 0
        cfg = config.load(state_home)
        if state_home is not None:
            try:
                cfg.resolve(payload["cwd"])
            except ValueError:
                return 0
        identity = hashlib.sha256(payload["session_id"].encode("utf-8")).hexdigest()[:32]
        path = cfg.root / "native-context" / f"claude-{identity}.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        config.atomic_json(path, {**payload, "captured_at": time.time()})
        os.chmod(path, 0o600)
        return 0
    except (OSError, ValueError, json.JSONDecodeError):
        return 2


def _native_claude_hook_payload(payload):
    if (payload.get("hook_event_name") != "UserPromptSubmit"
            or not isinstance(payload.get("session_id"), str)
            or not isinstance(payload.get("prompt"), str)):
        return False
    try:
        uuid.UUID(payload["session_id"])
        from claude_agent_sdk import project_key_for_directory
        workspace = Path(payload.get("cwd", "")).expanduser().resolve(strict=True)
        transcript = Path(payload.get("transcript_path", "")).expanduser().resolve(strict=True)
        config_root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser().resolve()
        projects = (config_root / "projects").resolve(strict=True)
        expected_parent = projects / project_key_for_directory(workspace)
        if (not transcript.is_file() or not transcript.is_relative_to(projects)
                or transcript.parent != expected_parent
                or transcript.name != payload["session_id"] + ".jsonl"):
            return False
        with transcript.open(encoding="utf-8") as stream:
            return any(json.loads(line).get("sessionId") == payload["session_id"]
                       for line in stream if line.strip())
    except (ImportError, AttributeError, OSError, ValueError, json.JSONDecodeError):
        return False


def claude_hook_cli(argv=None):
    parser = argparse.ArgumentParser(description=claude_hook_main.__doc__)
    parser.add_argument("--home", type=Path)
    return claude_hook_main(parser.parse_args(argv).home)


def _claude_hook_entry(command):
    return {"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": 5}]}


def _is_previous_claude_hook(entry, state_home):
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list) or len(hooks) != 1 or not isinstance(hooks[0], dict):
        return False
    command = hooks[0].get("command")
    if not isinstance(command, str) or entry != _claude_hook_entry(command):
        return False
    try:
        argv = [item.strip('"') for item in shlex.split(command, posix=os.name != "nt")]
    except ValueError:
        return False
    name = "multi-harness-claude-hook" + (".exe" if os.name == "nt" else "")
    if not argv or Path(argv[0]).name != name:
        return False
    return len(argv) == 1 or (len(argv) == 3 and argv[1] == "--home"
                              and Path(argv[2]).resolve() == state_home)


def install_claude_hook(workspace: Path, *, state_home=None):
    """Register the native context hook in project-local Claude settings."""
    settings = workspace / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(settings.read_text()) if settings.exists() else {}
    except json.JSONDecodeError as exc:
        raise ValueError("Claude local settings contain invalid JSON; preserving them") from exc
    if not isinstance(data, dict):
        raise ValueError("Claude local settings root must be a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Claude hooks settings must be an object")
    events = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(events, list):
        raise ValueError("Claude UserPromptSubmit hooks must be a list")
    state_home = config.load(state_home).root
    name = "multi-harness-claude-hook" + (".exe" if os.name == "nt" else "")
    executable = Path(sys.executable).with_name(name)
    argv = [str(executable), "--home", str(state_home)]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    desired = _claude_hook_entry(command)
    replacements = [event for event in events if not _is_previous_claude_hook(event, state_home)]
    replacements.append(desired)
    current = events == replacements
    if not current:
        hooks["UserPromptSubmit"] = replacements
        config.atomic_json(settings, data)
    return {"host": "claude", "status": "already-current" if current else "installed",
            "settings": str(settings)}


def devin_invocation(role, *, session_id, workspace, direct_user_prompt, project_hint=None):
    """Consume fields from a trusted Devin ACP invocation adapter, never MCP args."""
    title = _parse(direct_user_prompt, role)
    root = Path(workspace).expanduser().resolve(strict=True)
    project, _ = _resolve(root, project_hint)
    return Invocation(project, "devin", session_id, role, root, title, "deven-acp-direct-user-turn")


def devin_native_invocation(role=None, *, native_db=None, project_hint=None, max_age_seconds=180):
    """Resolve a fresh direct user takeover from Devin's local ACP session DB."""
    db_path = Path(native_db or os.environ.get("MHB_DEVIN_SESSIONS_DB", Path.home() / ".local/share/devin/cli/sessions.db"))
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as db:
            db.row_factory = sqlite3.Row
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"sessions", "message_nodes"} <= names:
                raise ValueError("Unsupported Devin native session schema")
            sessions = db.execute("SELECT id,working_directory,workspace_dirs,last_activity_at FROM sessions WHERE hidden=0").fetchall()
            rows = db.execute("SELECT session_id,row_id,chat_message FROM message_nodes ORDER BY row_id").fetchall()
    except (sqlite3.Error, OSError) as exc:
        raise ValueError(f"Devin native session evidence is unavailable ({type(exc).__name__})") from None
    by_session = {}
    for row in rows:
        try:
            message = json.loads(row["chat_message"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(message, dict):
            by_session.setdefault(row["session_id"], []).append(message)
    candidates = []
    for session in sessions:
        sid = session["id"]
        if not isinstance(sid, str) or not sid:
            continue
        try:
            activity = float(session["last_activity_at"])
            if abs(time.time() - activity) > max_age_seconds:
                continue
            workspace = Path(session["working_directory"]).expanduser().resolve(strict=True)
            roots = {workspace}
            for item in json.loads(session["workspace_dirs"] or "[]"):
                roots.add(Path(item).expanduser().resolve(strict=True))
        except (TypeError, ValueError, OSError, json.JSONDecodeError):
            continue
        try:
            project, _ = _resolve(workspace, project_hint)
        except ValueError:
            continue
        registered = config.load().projects[project]
        if not roots.intersection(registered):
            continue
        latest_user = next((m for m in reversed(by_session.get(sid, [])) if m.get("role") == "user"), None)
        if latest_user is None:
            continue
        prompt = latest_user.get("content", "")
        if isinstance(prompt, list):
            prompt = "\n".join(part.get("text", "") for part in prompt if isinstance(part, dict) and isinstance(part.get("text"), str))
        try:
            title = _parse(prompt, role)
        except ValueError:
            continue
        session_id = sid if sid.startswith("acp/devin-cli/") else f"acp/devin-cli/{sid}"
        candidates.append(Invocation(project, "devin", session_id, role or "", workspace, title,
                                     "devin-fresh-native-user-message"))
    unique = {(x.project, x.session): x for x in candidates}
    if len(unique) != 1:
        raise ValueError("Devin native invocation did not resolve to one fresh project session")
    return next(iter(unique.values()))


def antigravity_invocation(role, event, *, resolve_native_conversation, project_hint=None):
    """Check Desktop event against the host's native conversation resolver."""
    if not isinstance(event, dict):
        raise ValueError("Antigravity native event is required")
    title = _parse((event.get("lastUserInput") or "").removeprefix("@"), role)
    conversation = event.get("conversationId")
    if not isinstance(conversation, str) or not conversation:
        raise ValueError("Antigravity current conversation identity is unavailable")
    native = resolve_native_conversation(conversation)
    if not isinstance(native, dict) or native.get("conversationId") != conversation:
        raise ValueError("Antigravity event does not match the current native conversation")
    workspace = Path(native.get("workspace", "")).expanduser().resolve(strict=True)
    project, _ = _resolve(workspace, project_hint)
    return Invocation(project, "antigravity", conversation, role, workspace, title, "antigravity-native-event-and-conversation")


def antigravity_native_invocation(role, event, *, summaries_db=None, project_hint=None):
    """Resolve an Antigravity conversation from its local native summary DB."""
    if not isinstance(event, dict):
        raise ValueError("Antigravity native event is required")
    title = _parse((event.get("lastUserInput") or "").removeprefix("@"), role)
    conversation = event.get("conversationId")
    if not isinstance(conversation, str) or not conversation:
        raise ValueError("Antigravity current conversation identity is unavailable")
    if summaries_db is None and not os.environ.get("MHB_ANTIGRAVITY_SUMMARIES_DB"):
        try:
            from . import ag_wake
            cfg = config.load()
            matches = [(name, ag_wake._desktop_summary(name, conversation, cfg=cfg)) for name in cfg.projects]
            matches = [(name, summary) for name, summary in matches if summary]
            native = matches[0][1] if len(matches) == 1 else None
        except (ImportError, AttributeError):
            native = None
        if native is None:
            raise ValueError("Antigravity native conversation summary could not be verified")
        roots = {Path(value).expanduser().resolve(strict=True) for value in native.get("roots", set())}
        event_workspace = event.get("workspace")
        if isinstance(event_workspace, str):
            workspace = Path(event_workspace).expanduser().resolve(strict=True)
            if workspace not in roots:
                raise ValueError("Antigravity event workspace is not in native project metadata")
        elif len(roots) == 1:
            workspace = next(iter(roots))
        else:
            raise ValueError("Antigravity event must identify one of the native project workspaces")
        project, _ = _resolve(workspace, project_hint)
        if project != matches[0][0]:
            raise ValueError("Antigravity native project mapping does not match the workspace")
        return Invocation(project, "antigravity", conversation, role or "", workspace, title,
                          "antigravity-local-native-summary")
    db_path = Path(summaries_db or os.environ.get("MHB_ANTIGRAVITY_SUMMARIES_DB", ""))
    if not db_path.is_file():
        raise ValueError("Antigravity native conversation summary database is unavailable")
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(conversation_summaries)")}
            required = {"conversation_id", "workspace_uris", "project_id", "app_data_dir"}
            if not required <= columns:
                raise ValueError("Unsupported Antigravity native summary schema")
            row = db.execute("SELECT workspace_uris,project_id,app_data_dir FROM conversation_summaries WHERE conversation_id=?", (conversation,)).fetchone()
        if not row or row[1] is None or row[2] not in {"antigravity", "Antigravity"}:
            raise ValueError("Antigravity conversation is not confirmed by native state")
        roots_raw = json.loads(row[0])
        if not isinstance(roots_raw, list) or not roots_raw:
            raise ValueError("Antigravity native workspace metadata is invalid")
        roots = {Path(value).expanduser().resolve(strict=True) for value in roots_raw if isinstance(value, str)}
        if len(roots) != len(roots_raw):
            raise ValueError("Antigravity native workspace metadata is invalid")
        if len(roots) > 1:
            raise ValueError("Antigravity conversation has ambiguous workspace roots")
        workspace = next(iter(roots))
        project, _ = _resolve(workspace, project_hint)
        return Invocation(project, "antigravity", conversation, role or "", workspace, title, "antigravity-local-native-summary")
    except (sqlite3.Error, OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Antigravity native invocation could not be verified ({type(exc).__name__})") from None
