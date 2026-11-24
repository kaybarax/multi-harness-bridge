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


def server_name(project):
    return "mhb_roles_" + config.identifier(project)


def installation_entry(cfg, project, *, executable=None):
    return {"command": str(executable or sys.executable),
            "args": ["-m", "multi_harness_bridge.devin_tools", "--home", str(cfg.root), project, "serve-roles"]}


def install(cfg=None, project=None):
    cfg = cfg or config.load()
    project = cfg.require(project)
    name = server_name(project)
    entry = installation_entry(cfg, project)
    paths = [root / ".devin/mcp_config.local.json" for root in cfg.projects[project]]
    changes = {}
    with config.registry_writer(cfg.root):
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid Devin MCP JSON in {path}") from exc
            if not isinstance(value, dict) or not isinstance(value.setdefault("mcpServers", {}), dict):
                raise ValueError(f"Invalid Devin MCP configuration in {path}")
            servers = value["mcpServers"]
            if name in servers and servers[name] != entry:
                raise ValueError(f"Conflicting Devin MCP entry for {name} in {path}")
            if name not in servers:
                servers[name] = entry
                changes[path] = value
        for path, value in changes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            config.atomic_json(path, value)
    return {"host": "devin", "status": "installed" if changes else "already-current", "server": name,
            "config": str(paths[0]), "configs": [str(path) for path in paths]}


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


def _devin_user_command(message):
    """Use the native composer chips when Devin expands a slash skill into prose."""
    metadata = message.get("metadata") or {}
    blocks = (metadata.get("extensions") or {}).get("chisel/acp-content-blocks")
    if blocks is None:
        return _text(message.get("content"))
    if metadata.get("is_user_input") is not True or not isinstance(blocks, list):
        return ""
    if any(not isinstance(block, dict) or block.get("type") != "text"
           or not isinstance(block.get("text"), str) for block in blocks):
        return ""
    command = "".join(block["text"] for block in blocks).lstrip()
    namespace, separator, skill = command.partition(":")
    if separator and namespace in {"/devin", "/agents"}:
        return "/" + skill
    return command


def _native_call(project, tool, expected_args=None, *, role_command=None, freshness=180):
    """Resolve exactly one fresh, uncompleted native ACP call for this server."""
    from .devin_acp import _native_message_time

    db_path = _native_db()
    cutoff = time.time() - freshness
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as db:
            db.row_factory = sqlite3.Row
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"sessions", "message_nodes"} <= tables:
                raise ValueError("Unsupported Devin native session database")
            rows = db.execute("SELECT session_id,row_id,chat_message,created_at FROM message_nodes WHERE created_at>=? ORDER BY row_id ASC", (cutoff,)).fetchall()
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
            at = _native_message_time(message, row["created_at"])
            by_session.setdefault(row["session_id"], []).append((row["row_id"], at, message))
    expected_server = server_name(project)
    matches = {}
    conflicts = set()
    for sid, messages in by_session.items():
        session = sessions.get(sid)
        if not session:
            continue
        completed = {item.get("tool_call_id") for _, _, item in messages if item.get("role") == "tool"}
        for index, (row_id, _, message) in enumerate(messages):
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
                user_turn = next(((prior_id, at, _devin_user_command(item))
                                  for prior_id, at, item in reversed(messages[:index])
                                  if prior_id < row_id and item.get("role") == "user"), (None, None, None))
                user_row_id, user_at, user = user_turn
                if role_command:
                    from .role_adapters import _parse_details
                    try:
                        title, orchestrator = _parse_details(user, role_command)
                    except ValueError:
                        continue
                else:
                    title = None
                    orchestrator = False
                try:
                    workspace = Path(session["working_directory"]).expanduser().resolve(strict=True)
                    if config.load().resolve(workspace) != project:
                        continue
                except (OSError, ValueError):
                    continue
                session_id = sid if sid.startswith("acp/devin-cli/") else f"acp/devin-cli/{sid}"
                request_id = hashlib.sha256(
                    f"devin\0{project}\0{session_id}\0{user_row_id}".encode("utf-8")
                ).hexdigest()
                key = (session_id, call_id)
                candidate = {"session": session_id, "workspace": workspace, "title": title,
                             "orchestrator": orchestrator,
                             "request_id": request_id, "actual_args": actual, "call_id": call_id,
                             "user_prompt": user, "user_at": user_at}
                if key in matches and matches[key] != candidate:
                    conflicts.add(key)
                matches[key] = candidate
    # A matching call must be unique across all fresh native sessions.
    expected = expected_args or {}
    candidates = [match for key, match in matches.items() if key not in conflicts
                  and all(match["actual_args"].get(name) == value for name, value in expected.items())]
    if len(candidates) != 1:
        raise PermissionError("Devin tool call did not resolve to one fresh native caller")
    return candidates[0]


def make_role_server(cfg, project):
    from mcp.server.fastmcp import FastMCP
    server = FastMCP(server_name(project), instructions=(
        "Project role and mailbox operations are bound to the current Devin Local ACP chat. "
        "Takeover requires an exact direct user request, prepare_role_handoff, then the matching take tool in the same turn."
    ))
    cfg.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = config.open_store(cfg)
    role_values = {"coordinator": "coordinator", "bridge": "inbox"}

    def current(tool, args=None):
        from .devin_acp import WAKE_PREFIX, FINISH_TOOL, PICKUP_TOOL

        call = _native_call(project, tool, args)
        if (call["user_prompt"] or "").startswith(WAKE_PREFIX) and tool not in {PICKUP_TOOL, FINISH_TOOL}:
            raise PermissionError("This native wake is scoped to its notification tools")
        return call

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
            db.execute("CREATE TABLE IF NOT EXISTS devin_role_challenge_proofs(challenge_hash TEXT PRIMARY KEY,orchestrator INTEGER NOT NULL,request_id TEXT NOT NULL)")
            db.execute("DELETE FROM devin_role_challenges WHERE expires<=? OR used=1", (time.time(),))
            db.execute("DELETE FROM devin_role_challenge_proofs WHERE challenge_hash NOT IN (SELECT challenge_hash FROM devin_role_challenges)")
            digest = hashlib.sha256(nonce.encode()).hexdigest()
            db.execute("INSERT INTO devin_role_challenges VALUES(?,?,?,?,?,?,0)",
                       (digest, project,
                        call["session"], str(call["workspace"]), role, time.time()+120))
            db.execute("INSERT INTO devin_role_challenge_proofs VALUES(?,?,?)",
                       (digest, int(call.get("orchestrator", False)), call["request_id"]))
        return {"challenge": nonce, "expires_in_seconds": 120,
                "orchestrator": bool(call.get("orchestrator", False))}

    def take(role, challenge):
        if not isinstance(challenge, str) or len(challenge) != 43:
            raise PermissionError("A fresh role handoff challenge is required")
        tool = "take_coordinator" if role == "coordinator" else "take_bridge"
        call = _native_call(project, tool, {"challenge": challenge}, role_command=role)
        digest = hashlib.sha256(challenge.encode()).hexdigest()
        with closing(sqlite3.connect(cfg.database)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS devin_role_challenge_proofs(challenge_hash TEXT PRIMARY KEY,orchestrator INTEGER NOT NULL,request_id TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session,workspace,role,expires,used FROM devin_role_challenges WHERE challenge_hash=? AND project=?", (digest,project)).fetchone()
            proof = db.execute("SELECT orchestrator,request_id FROM devin_role_challenge_proofs WHERE challenge_hash=?", (digest,)).fetchone()
            if (not row or row[0] != call["session"] or row[1] != str(call["workspace"])
                    or row[2] != role or row[3] <= time.time() or row[4]
                    or (proof is None and call.get("orchestrator", False))
                    or (proof is not None and (bool(proof[0]) != bool(call.get("orchestrator", False))
                                               or proof[1] != call["request_id"]))):
                raise PermissionError("Role challenge is expired, used, or belongs to another native session")
            db.execute("UPDATE devin_role_challenges SET used=1,role=? WHERE challenge_hash=?", (role,digest))
        try:
            if role == "coordinator":
                takeover = store.take_coordinator(project, "devin", call["session"],
                                                  designate=bool(call.get("orchestrator", False)),
                                                  request_id=call["request_id"])
                roles = takeover["roles"]
                orchestrator_status = takeover["orchestrator"]
            else:
                roles = store.bind(project,"devin",role_values[role],call["session"], take=True)
        except Exception:
            with closing(sqlite3.connect(cfg.database)) as db, db:
                db.execute("UPDATE devin_role_challenges SET used=0 WHERE challenge_hash=? AND expires>?",
                           (digest, time.time()))
            raise
        else:
            with closing(sqlite3.connect(cfg.database)) as db, db:
                db.execute("DELETE FROM devin_role_challenge_proofs WHERE challenge_hash=?", (digest,))
        title = call.get("title")
        result = {"project":project,"harness":"devin","role":role_values[role],"session":call["session"],"roles":roles,
                  "status":store.status(project,"devin"),"pending":store.pending_overview(project,"devin",20)}
        if role == "coordinator":
            result["orchestrator"] = orchestrator_status
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
        return {"roles":roles,"status":store.status(project,"devin"),"role":role,
                "orchestrator":store.orchestrator_status(project)}

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
    def pickup_wake(notification_id: str) -> dict:
        """Pick up only the exact notification in the current automatic native wake."""
        from . import devin_acp

        call = current(devin_acp.PICKUP_TOOL, {"notification_id": notification_id})
        if set(call["actual_args"]) != {"notification_id"}:
            raise PermissionError("Wake pickup must match the exact native tool inputs")
        role, _, _ = devin_acp.active_wake(store, project, server_name(project), call, notification_id)
        return devin_acp._pickup_notification(store, project, role, call["session"], notification_id)

    @server.tool()
    def finish_wake(notification_id: str, lease: str, reply: str | None = None) -> dict:
        """Complete the exact automatic notification with its pickup lease and optional text reply."""
        from . import devin_acp

        args = {"notification_id": notification_id, "lease": lease, "reply": reply}
        call = current(devin_acp.FINISH_TOOL, args)
        if set(call["actual_args"]) - set(args):
            raise PermissionError("Wake finish must match the exact native tool inputs")
        name = server_name(project)
        role, started, prompt = devin_acp.active_wake(store, project, name, call, notification_id)
        if role != "coordinator":
            raise PermissionError("Only the woken coordinator can complete this notification")
        observed = devin_acp._native_completion(call["session"], call["workspace"], name, prompt,
                                                notification_id, started, _native_db())
        return devin_acp._finish_notification(store, project, call["session"], notification_id,
                                              observed.get(devin_acp.PICKUP_TOOL), notification_id, lease, reply)

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
    def send_fanout(recipients: list[str], body: str, key: str) -> dict:
        """Send one free-text request to all named peer harnesses in this project. Reuse the key on retry."""
        args = {"recipients": recipients, "body": body, "key": key}
        call = current("send_fanout", args)
        return store.send_fanout(project, "devin", recipients, body, key, session=call["session"])

    @server.tool()
    def reply_free_text(reply_to: str, body: str, key: str) -> dict:
        """Send a correlated reply to a received request. Use its delivery ID and reuse the reply key on retry."""
        args = {"reply_to": reply_to, "body": body, "key": key}
        call = current("reply_free_text", args)
        return store.reply_free_text(project, "devin", reply_to, body, key, session=call["session"])

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
