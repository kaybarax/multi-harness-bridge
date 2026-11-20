"""Idempotent registration of the project-scoped Devin Local role server."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
from contextlib import closing

from . import config
from .store import Store


def server_name(project):
    return "mhb_roles_" + config.identifier(project)


def install(cfg=None, project=None):
    cfg = cfg or config.load()
    project = cfg.require(project)
    root = cfg.projects[project][0]
    directory = root / ".devin"
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "mcp_config.local.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    except json.JSONDecodeError as exc:
        raise ValueError("Devin local MCP manifest is invalid JSON; preserving it") from exc
    if not isinstance(value, dict):
        raise ValueError("Devin local MCP manifest must be a JSON object")
    servers = value.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("Devin mcpServers must be a JSON object")
    name = server_name(project)
    entry = {"command": sys.executable,
             "args": ["-m", "multi_harness_bridge.devin_tools", "--home", str(cfg.root), project, "serve-roles"]}
    existing = servers.get(name)
    if existing not in (None, entry):
        raise ValueError(f"Conflicting Devin MCP entry for {name}; preserving existing configuration")
    if existing == entry:
        return {"host": "devin", "status": "already-current", "server": name, "config": str(manifest)}
    servers[name] = entry
    config.atomic_json(manifest, value)
    return {"host": "devin", "status": "installed", "server": name, "config": str(manifest)}


def _native_db():
    return Path(os.environ.get("MHB_DEVIN_SESSIONS_DB", Path.home()/".local/share/devin/cli/sessions.db"))


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text(item) for item in value)))
    if isinstance(value, dict):
        for key in ("text", "content", "output", "result"):
            if key in value:
                return _text(value[key])
    return ""


def _native_call(project, tool, expected_args=None, *, role_command=None, freshness=180):
    """Resolve exactly one fresh, uncompleted native ACP call for this server."""
    db_path = _native_db()
    cutoff = time.time() - freshness
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as db:
            db.row_factory = sqlite3.Row
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"sessions", "message_nodes"} <= tables:
                raise ValueError("Unsupported Devin native session database")
            rows = db.execute("SELECT session_id,row_id,chat_message FROM message_nodes WHERE created_at>=? ORDER BY row_id ASC", (cutoff,)).fetchall()
            sessions = {row["id"]: row for row in db.execute("SELECT id,working_directory,workspace_dirs,title FROM sessions WHERE hidden=0")}
    except sqlite3.Error as exc:
        raise ValueError(f"Devin native ACP evidence unavailable ({type(exc).__name__})") from None
    by_session = {}
    for row in rows:
        try:
            message = json.loads(row["chat_message"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(message, dict):
            by_session.setdefault(row["session_id"], []).append((row["row_id"], message))
    expected_server = server_name(project)
    matches = []
    for sid, messages in by_session.items():
        session = sessions.get(sid)
        if not session:
            continue
        completed = {item.get("tool_call_id") for _, item in messages if item.get("role") == "tool"}
        for index, (row_id, message) in enumerate(messages):
            if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
                continue
            for call in message["tool_calls"]:
                if not isinstance(call, dict) or call.get("name") != "mcp_call_tool":
                    continue
                call_id, args = call.get("id"), call.get("arguments")
                if not isinstance(call_id, str) or call_id in completed or not isinstance(args, dict):
                    continue
                if args.get("server_name") != expected_server or args.get("tool_name") != tool:
                    continue
                actual = args.get("arguments") or {}
                if not isinstance(actual, dict):
                    continue
                expected = expected_args or {}
                if any(actual.get(key) != value for key, value in expected.items()):
                    continue
                prior = [item for prior_id, item in messages[:index] if prior_id < row_id]
                user = next((_text(item.get("content")) for item in reversed(prior) if item.get("role") == "user"), None)
                if role_command:
                    from .role_adapters import _parse
                    try:
                        title = _parse(user, role_command)
                    except ValueError:
                        continue
                else:
                    title = None
                workspace = Path(session["working_directory"]).expanduser().resolve(strict=True)
                try:
                    if config.load().resolve(workspace) != project:
                        continue
                except ValueError:
                    continue
                session_id = sid if sid.startswith("acp/devin-cli/") else f"acp/devin-cli/{sid}"
                matches.append({"session": session_id, "workspace": workspace, "title": title,
                                "actual_args": actual, "call_id": call_id})
    # A matching call must be unique across all fresh native sessions.
    if len(matches) != 1:
        raise PermissionError("Devin tool call did not resolve to one fresh native caller")
    return matches[0]


def make_role_server(cfg, project):
    from mcp.server.fastmcp import FastMCP
    server = FastMCP(server_name(project), instructions=(
        "Project role and mailbox operations are bound to the current Devin Local ACP chat. "
        "Takeover requires an exact direct user request, prepare_role_handoff, then the matching take tool in the same turn."
    ))
    cfg.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = Store(cfg.database)
    role_values = {"coordinator": "coordinator", "bridge": "inbox"}

    def current(tool, args=None):
        return _native_call(project, tool, args)

    @server.tool()
    def prepare_role_handoff() -> dict:
        """Create a short-lived one-use challenge before role takeover."""
        # Resolve this exact native call and ensure a direct user asked for a role change.
        role = None
        call = None
        for candidate in ("coordinator", "bridge"):
            try:
                call = _native_call(project, "prepare_role_handoff", role_command=candidate)
                role = candidate
                break
            except PermissionError:
                continue
        if call is None or role is None:
            raise PermissionError("Current direct user prompt must explicitly invoke take coordinator or take bridge")
        nonce = secrets.token_urlsafe(32)
        with closing(sqlite3.connect(cfg.database)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS devin_role_challenges(challenge_hash TEXT PRIMARY KEY,project TEXT,session TEXT,workspace TEXT,role TEXT,expires REAL,used INTEGER DEFAULT 0)")
            db.execute("DELETE FROM devin_role_challenges WHERE expires<=? OR used=1", (time.time(),))
            db.execute("INSERT INTO devin_role_challenges VALUES(?,?,?,?,?,?,0)",
                       (hashlib.sha256(nonce.encode()).hexdigest(), project,
                        call["session"], str(call["workspace"]), role, time.time()+120))
        return {"challenge": nonce, "expires_in_seconds": 120}

    def take(role, challenge):
        if not isinstance(challenge, str) or len(challenge) != 43:
            raise PermissionError("A fresh role handoff challenge is required")
        tool = "take_coordinator" if role == "coordinator" else "take_bridge"
        call = _native_call(project, tool, {"challenge": challenge}, role_command=role)
        digest = hashlib.sha256(challenge.encode()).hexdigest()
        with closing(sqlite3.connect(cfg.database)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session,workspace,role,expires,used FROM devin_role_challenges WHERE challenge_hash=? AND project=?", (digest,project)).fetchone()
            if not row or row[0] != call["session"] or row[1] != str(call["workspace"]) or row[2] != role or row[3] <= time.time() or row[4]:
                raise PermissionError("Role challenge is expired, used, or belongs to another native session")
            db.execute("UPDATE devin_role_challenges SET used=1,role=? WHERE challenge_hash=?", (role,digest))
        try:
            roles = store.bind(project,"devin",role_values[role],call["session"], take=True)
        except Exception:
            with closing(sqlite3.connect(cfg.database)) as db, db:
                db.execute("UPDATE devin_role_challenges SET used=0 WHERE challenge_hash=? AND expires>?",
                           (digest, time.time()))
            raise
        title = call.get("title")
        result = {"project":project,"harness":"devin","role":role_values[role],"session":call["session"],"roles":roles,
                  "status":store.status(project,"devin"),"pending":store.pending_overview(project,"devin",20)}
        if title:
            from .devin_acp import rename
            try:
                result["title"] = {"status":"verified","value":rename(call["session"],call["workspace"],title)}
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                result["title"] = {"status":"failed","reason":type(exc).__name__}
        return result

    @server.tool()
    def take_coordinator(challenge: str) -> dict:
        """Bind this native Devin Local chat as project Coordinator."""
        return take("coordinator",challenge)

    @server.tool()
    def take_bridge(challenge: str) -> dict:
        """Bind this native Devin Local chat as project Inbox."""
        return take("bridge",challenge)

    @server.tool()
    def bridge_status() -> dict:
        call=current("bridge_status")
        roles=store.roles(project,"devin")
        role=next((r for r,v in roles.items() if v==call["session"]),None)
        if role is None: raise PermissionError("Current native chat is not bound to this project")
        return {"roles":roles,"status":store.status(project,"devin"),"role":role}

    @server.tool()
    def pickup_notification(notification_id: str | None = None) -> dict:
        args={} if notification_id is None else {"notification_id":notification_id}
        call=current("pickup_notification",args)
        roles=store.roles(project,"devin")
        role=next((r for r,v in roles.items() if v==call["session"]),None)
        if role is None: raise PermissionError("Current native chat is not bound to this project")
        item=store.claim(project,"devin",role,call["session"],message_id=notification_id)
        if not item:return {"message":None}
        return {"message":item}

    @server.tool()
    def acknowledge(id: str, lease: str) -> dict:
        call=current("acknowledge",{"id":id,"lease":lease})
        role=next((r for r,v in store.roles(project,"devin").items() if v==call["session"]),None)
        if role is None: raise PermissionError("Current native chat is not bound to this project")
        return {"finished":store.finish(project,"devin",role,call["session"],id,lease)}

    @server.tool()
    def send_message(recipient: str, body: str, key: str, reply_to: str | None = None) -> dict:
        args={"recipient":recipient,"body":body,"key":key}
        if reply_to is not None:args["reply_to"]=reply_to
        call=current("send_message",args)
        return store.send(project,"devin",recipient,body,key,reply_to=reply_to,session=call["session"])

    @server.tool()
    def set_pickup(enabled: bool) -> dict:
        call=current("set_pickup",{"enabled":enabled})
        return {"enabled":store.set_enabled(project,"devin",enabled,session=call["session"])}

    return server


def serve_roles(cfg, project):
    make_role_server(cfg, cfg.require(project)).run(transport="stdio")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    parser.add_argument("project")
    parser.add_argument("action", choices=("serve-roles",))
    args = parser.parse_args(argv)
    if args.home:
        os.environ["MHB_HOME"] = str(args.home)
    cfg = config.load()
    cfg.require(args.project)
    serve_roles(cfg, args.project)


if __name__ == "__main__":
    main()
