"""Antigravity Desktop PreToolUse hook and project-scoped stdio MCP server."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import sqlite3
import subprocess
import sys
import time

from . import ag_wake, config
from .store import Store

TOOLS = ("take_coordinator", "take_bridge", "pickup_notification", "acknowledge", "send_message", "set_pickup", "bridge_status")


def server_name(project):
    return "mhb_" + config.identifier(project)


def _db(cfg):
    db = sqlite3.connect(cfg.database, timeout=5)
    db.execute("""CREATE TABLE IF NOT EXISTS ag_hook_grants(
        digest TEXT PRIMARY KEY,project TEXT,session TEXT,tool TEXT,parameters TEXT,title TEXT,expires REAL)""")
    return db


def before_tool(cfg, project, event):
    name = server_name(project)
    call = event.get("toolCall", {})
    native = call.get("name", "")
    if not isinstance(native, str):
        raise ValueError("Invalid tool name")
    if native in TOOLS:
        # Antigravity's per-server PreToolUse can provide the short tool name.
        tool = native
    else:
        tool = next((t for t in TOOLS if native in {
            f"mcp_{name}_{t}", f"mcp__{name}__{t}", f"{name}_{t}"}), None)
    if tool is None:
        return {"decision":"allow"}
    session = event.get("conversationId")
    summary = ag_wake._desktop_summary(project, session, cfg=cfg)
    roots = {ag_wake._local_path(p) for p in event.get("workspacePaths", [])}
    brain = ag_wake._summary_path().parent / "brain" / session
    artifact = ag_wake._local_path(event.get("artifactDirectoryPath"))
    transcript = ag_wake._local_path(event.get("transcriptPath"))
    if (not summary or roots != set(cfg.projects[project])
            or (artifact != brain and transcript != brain/".system_generated/logs/transcript.jsonl")
            or (transcript and any(p in {"antigravity-cli","antigravity-ide"} for p in transcript.parts))):
        raise PermissionError("Native Desktop conversation and registered project must agree")
    args = dict(call.get("args") or {})
    args.pop("desktop_grant", None)
    title = None
    if tool in {"take_bridge", "take_coordinator"}:
        from .role_adapters import _parse
        title = _parse(event.get("lastUserInput"), "inbox" if tool == "take_bridge" else "coordinator")
    else:
        roles = Store(cfg.database).roles(project, "antigravity")
        if session not in roles.values() or tool in {"send_message", "set_pickup"} and session != roles["coordinator"]:
            raise PermissionError("Current Desktop chat does not own this role")
    token = secrets.token_urlsafe(32)
    with closing(_db(cfg)) as db, db:
        db.execute("DELETE FROM ag_hook_grants WHERE expires<=?", (time.time(),))
        db.execute("INSERT INTO ag_hook_grants VALUES(?,?,?,?,?,?,?)",
                   (hashlib.sha256(token.encode()).hexdigest(), project, session, tool,
                    json.dumps(args, sort_keys=True), title, time.time()+120))
    return {"decision":"allow", "overwrite":{"desktop_grant":token}}


def invoke(cfg, project, tool, grant):
    if not isinstance(grant, str) or len(grant) != 43:
        raise PermissionError("Antigravity's native hook must authorize this tool call")
    with closing(_db(cfg)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        fingerprint = hashlib.sha256(grant.encode()).hexdigest()
        row = db.execute("SELECT session,parameters,title FROM ag_hook_grants WHERE digest=? AND project=? AND tool=? AND expires>?",
                         (fingerprint, project, tool, time.time())).fetchone()
        if not row:
            raise PermissionError("Expired, used or mismatched Desktop grant")
        db.execute("DELETE FROM ag_hook_grants WHERE digest=?", (fingerprint,))
    session, data, title = row
    args = json.loads(data)
    store = Store(cfg.database)
    if not ag_wake._desktop_summary(project, session, cfg=cfg):
        raise PermissionError("Desktop session changed during tool call")
    roles = store.roles(project, "antigravity")
    if tool in {"take_coordinator", "take_bridge"}:
        role = "coordinator" if tool == "take_coordinator" else "inbox"
        previous = roles[role]
        roles = store.bind(project, "antigravity", role, session)
        result = {"project":project,"harness":"antigravity","role":role,"previous":previous,
                  "roles":roles,"status":store.status(project,"antigravity"),
                  "pending":store.pending_overview(project,"antigravity",20)}
        if title is not None:
            try:
                ag_wake.rename(project, role, session, title, cfg=cfg)
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                result["title_error"] = str(exc)
            else:
                result["verified_title"] = title
        return result
    role = next((key for key, value in roles.items() if session == value), None)
    if role is None:
        raise PermissionError("The Desktop chat no longer owns a role")
    if tool == "bridge_status":
        return {"roles":roles, "status":store.status(project,"antigravity")}
    if tool == "pickup_notification":
        item = store.claim(project, "antigravity", role, session, message_id=args.get("notification_id"))
        if not item:
            return {"message":None}
        if role == "inbox":
            ok = store.finish(project,"antigravity",role,session,item["id"],item["lease"])
            return {"message_id":item["id"],"forwarded":ok}
        return {"message":item, "next":"Call acknowledge after recording receipt. Use send_message for a reply."}
    if tool == "acknowledge":
        return {"finished":store.finish(project,"antigravity",role,session,args["id"],args["lease"])}
    if role != "coordinator":
        raise PermissionError("Only the bound coordinator can send or change pickup")
    if tool == "set_pickup":
        if not isinstance(args.get("enabled"), bool):
            raise ValueError("enabled must be boolean")
        return {"enabled":store.set_enabled(project,"antigravity",args["enabled"],session=session)}
    if tool == "send_message":
        return store.send(project,"antigravity",args["recipient"],args["body"],args["key"],
                          reply_to=args.get("reply_to"),session=session)
    raise ValueError("Unknown operation")


def make_server(cfg, project):
    from mcp.server.fastmcp import FastMCP
    server = FastMCP(server_name(project))

    def simple(name):
        @server.tool(name=name)
        def action(desktop_grant: str = "") -> dict:
            """Use the current native chat and project verified by the Desktop hook."""
            return invoke(cfg, project, name, desktop_grant)
    for name in ("take_coordinator", "take_bridge", "bridge_status"):
        simple(name)

    @server.tool()
    def pickup_notification(notification_id: str | None = None, desktop_grant: str = "") -> dict:
        """Inbox forwards, coordinator receives. Use the wake's message ID when supplied."""
        return invoke(cfg, project, "pickup_notification", desktop_grant)

    @server.tool()
    def acknowledge(id: str, lease: str, desktop_grant: str = "") -> dict:
        """Confirm receipt of the exact leased message."""
        return invoke(cfg, project, "acknowledge", desktop_grant)

    @server.tool()
    def send_message(recipient: str, body: str, key: str, reply_to: str | None = None, desktop_grant: str = "") -> dict:
        """Send through the current project's bridge. Reuse the key on retry."""
        return invoke(cfg, project, "send_message", desktop_grant)

    @server.tool()
    def set_pickup(enabled: bool, desktop_grant: str = "") -> dict:
        """Stop or resume the bound project and harness."""
        return invoke(cfg, project, "set_pickup", desktop_grant)
    return server


def install(cfg, project, directory=None):
    cfg.require(project)
    root = Path(directory or Path.home()/".gemini/config")
    root.mkdir(parents=True, exist_ok=True)
    name = server_name(project)
    def invocation(action):
        return [sys.executable, "-m", "multi_harness_bridge.ag_inbox", "--home", str(cfg.root), project, action]
    cli = invocation("hook")
    shell_command = subprocess.list2cmdline(cli) if os.name == "nt" else shlex.join(cli)
    server = {"command":sys.executable, "args":invocation("serve")[1:], "tools":{t:{"eager":True} for t in TOOLS}}
    hook = {"PreToolUse":[{"matcher":".*(?:"+"|".join(TOOLS)+")$",
                           "hooks":[{"type":"command","command":shell_command,"timeout":5}]}]}
    def read(filename):
        p = root/filename
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if not isinstance(data,dict):
            raise ValueError(f"Invalid {filename}")
        return data
    with config.registry_writer(cfg.root):
        mcp = read("mcp_config.json")
        hooks = read("hooks.json")
        entries = mcp.setdefault("mcpServers", {})
        if not isinstance(entries,dict):
            raise ValueError("Invalid Antigravity server configuration")
        if entries.get(name) not in (None,server) or hooks.get(name) not in (None,hook):
            raise ValueError("Conflicting native bridge entry. Review the existing configuration.")
        if entries.get(name) != server:
            entries[name] = server
            config.atomic_json(root/"mcp_config.json",mcp)
        if hooks.get(name) != hook:
            hooks[name] = hook
            config.atomic_json(root/"hooks.json",hooks)
    return {"server":name,"configuration":str(root),"reload":"Reload this server in Antigravity Desktop."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    parser.add_argument("project")
    parser.add_argument("action", choices=("serve","hook","install"))
    args = parser.parse_args(argv)
    if args.home:
        os.environ["MHB_HOME"] = str(args.home)
    cfg = config.load()
    cfg.require(args.project)
    if args.action == "hook":
        try:
            out = before_tool(cfg,args.project,json.load(sys.stdin))
        except (ValueError,RuntimeError,OSError,TypeError,KeyError,sqlite3.Error) as exc:
            out = {"decision":"deny","reason":str(exc)}
        print(json.dumps(out))
    elif args.action == "serve":
        make_server(cfg,args.project).run(transport="stdio")
    else:
        print(json.dumps(install(cfg,args.project)))


if __name__ == "__main__":
    main()
