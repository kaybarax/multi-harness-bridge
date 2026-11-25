"""Antigravity Desktop PreToolUse hook and project-scoped stdio MCP server."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import time

from . import ag_wake, config

COORDINATOR_TOOLS = {"send_message", "send_fanout", "reply_free_text", "set_pickup"}
_LEGACY_TOOLS = ("take_coordinator", "take_bridge", "pickup_notification", "acknowledge",
                 "send_message", "set_pickup", "bridge_status", "propagate_skills")
TOOLS = (*_LEGACY_TOOLS, "send_fanout", "reply_free_text")


def server_name(project):
    return "mhb_" + config.identifier(project)


def _db(cfg):
    db = sqlite3.connect(cfg.database, timeout=5)
    db.execute("""CREATE TABLE IF NOT EXISTS ag_hook_grants(
        digest TEXT PRIMARY KEY,project TEXT,session TEXT,tool TEXT,parameters TEXT,title TEXT,expires REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS ag_hook_grant_proofs(
        digest TEXT PRIMARY KEY,orchestrator INTEGER NOT NULL,request_id TEXT)""")
    return db


def _extract_user_request(content):
    """Extract one direct request from Antigravity's native transcript envelope."""
    if not isinstance(content, str):
        raise ValueError("Native user content must be text")
    opening, closing = "<USER_REQUEST>", "</USER_REQUEST>"
    if opening not in content and closing not in content:
        return content
    if content.count(opening) != 1 or content.count(closing) != 1:
        raise ValueError("Native user request envelope is missing or duplicated")
    match = re.fullmatch(r"\s*<USER_REQUEST>(.*?)</USER_REQUEST>(.*)", content, re.DOTALL)
    if not match:
        raise ValueError("Native user request envelope has an invalid shape")
    request, trailer = match.groups()
    sections = re.fullmatch(
        r"\s*(?:<ADDITIONAL_METADATA>(.*?)</ADDITIONAL_METADATA>\s*)?"
        r"(?:<USER_SETTINGS_CHANGE>(.*?)</USER_SETTINGS_CHANGE>\s*)?",
        trailer, re.DOTALL,
    )
    if not sections:
        raise ValueError("Native user request has malformed trailing context")
    return request


def _orchestrator_request_id(event, brain, session):
    """Bind opt-in to the latest verified native user turn, never prompt text alone."""
    transcript = brain / ".system_generated/logs/transcript.jsonl"
    try:
        latest = None
        with transcript.open(encoding="utf-8") as stream:
            for line in stream:
                item = json.loads(line)
                if (isinstance(item, dict) and item.get("source") == "USER_EXPLICIT"
                        and item.get("type") == "USER_INPUT"):
                    latest = item
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise PermissionError("Cannot verify Antigravity's current native user turn for -orc") from None
    prompt = event.get("lastUserInput")
    if not isinstance(prompt, str):
        raise PermissionError("Antigravity -orc requires the direct user command in native turn evidence")
    prompt = prompt.strip().removeprefix("@").strip()
    step = latest.get("step_index") if latest else None
    content = latest.get("content") if latest else None
    try:
        request = _extract_user_request(content).strip()
    except ValueError:
        raise PermissionError("Antigravity native user request envelope is invalid") from None
    request = request.strip().removeprefix("@").strip()
    if (not isinstance(step, int) or isinstance(step, bool) or step < 0
            or request != prompt.strip()):
        raise PermissionError("Cannot match Antigravity -orc to the latest native user turn")
    return f"antigravity:{session}:{step}"


def _explicit_skill_sources(prompt):
    """Compatibility helper for callers that only need the requested sources."""
    from .propagation import explicit_skill_request
    return explicit_skill_request(prompt)[1]


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
    raw_roots = event.get("workspacePaths", [])
    roots = {ag_wake._local_path(p) for p in raw_roots} if isinstance(raw_roots, list) else set()
    active_workspace = event.get("workspace") or event.get("workspacePath")
    if tool == "propagate_skills":
        summary = ag_wake._desktop_propagation_summary(
            project, session, cfg=cfg, event_roots=roots,
            active_workspace=active_workspace,
        )
    else:
        summary = ag_wake._desktop_summary(project, session, cfg=cfg)
    brain = ag_wake._summary_path().parent / "brain" / session
    artifact = ag_wake._local_path(event.get("artifactDirectoryPath"))
    transcript = ag_wake._local_path(event.get("transcriptPath"))
    if (not summary or tool != "propagate_skills" and roots != set(cfg.projects[project])
            or (artifact != brain and transcript != brain/".system_generated/logs/transcript.jsonl")
            or (transcript and any(p in {"antigravity-cli","antigravity-ide"} for p in transcript.parts))):
        raise PermissionError("Native Desktop conversation and registered project must agree")
    args = dict(call.get("args") or {})
    args.pop("desktop_grant", None)
    from . import wake_policy
    kind, woken = wake_policy.classify("antigravity", text=event.get("lastUserInput"))
    wake_policy.check(cfg.root, project, "antigravity", kind, wake_policy.tool_action(tool, args),
                      reply_to=args.get("reply_to"), woken=woken)
    args["_turn"] = kind
    title = None
    orchestrator = False
    request_id = None
    if tool in {"take_bridge", "take_coordinator"}:
        from .role_adapters import _parse_details
        title, orchestrator = _parse_details((event.get("lastUserInput") or "").removeprefix("@"),
                                            "inbox" if tool == "take_bridge" else "coordinator")
        if orchestrator:
            request_id = _orchestrator_request_id(event, brain, session)
    elif tool == "propagate_skills":
        from .propagation import explicit_skill_request
        mode, sources = explicit_skill_request(event.get("lastUserInput"))
        supplied = args.get("sources")
        if (not isinstance(supplied, list) or any(not isinstance(item, str) for item in supplied)
                or supplied != sources):
            raise PermissionError("Tool skill sources must exactly match the current user request")
        active = summary["active_workspace"]
        requested_workspace = args.get("workspace")
        if requested_workspace and ag_wake._local_path(requested_workspace) != active:
            raise PermissionError("Requested destination does not match the active native workspace")
        entry_declared = (active / ".mhb/project-skills.json").is_file()
        selected_mode = "entry" if entry_declared else "copy"
        supplied_mode = args.get("mode", "auto")
        if supplied_mode not in {"auto", selected_mode} or mode == "entry" and not entry_declared:
            raise PermissionError("Propagation mode must match the active project's declaration")
        args["mode"] = selected_mode
        args["workspace"] = str(active)
        args["_native_roots"] = sorted(str(root) for root in summary["roots"])
        args["_native_project_id"] = summary["project_id"]
    else:
        roles = config.open_store(cfg).roles(project, "antigravity")
        if session not in roles.values() or tool in COORDINATOR_TOOLS and session != roles["coordinator"]:
            raise PermissionError("Current Desktop chat does not own this role")
    token = secrets.token_urlsafe(32)
    with closing(_db(cfg)) as db, db:
        db.execute("DELETE FROM ag_hook_grants WHERE expires<=?", (time.time(),))
        db.execute("DELETE FROM ag_hook_grant_proofs WHERE digest NOT IN (SELECT digest FROM ag_hook_grants)")
        digest = hashlib.sha256(token.encode()).hexdigest()
        db.execute("INSERT INTO ag_hook_grants(digest,project,session,tool,parameters,title,expires) VALUES(?,?,?,?,?,?,?)",
                   (digest, project, session, tool, json.dumps(args, sort_keys=True), title, time.time()+120))
        db.execute("INSERT INTO ag_hook_grant_proofs VALUES(?,?,?)",
                   (digest, int(orchestrator), request_id))
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
        proof = db.execute("SELECT orchestrator,request_id FROM ag_hook_grant_proofs WHERE digest=?",
                           (fingerprint,)).fetchone()
        db.execute("DELETE FROM ag_hook_grants WHERE digest=?", (fingerprint,))
        db.execute("DELETE FROM ag_hook_grant_proofs WHERE digest=?", (fingerprint,))
    session, data, title = row
    args = json.loads(data)
    from . import wake_policy
    sent_from = {"origin": wake_policy.origin(args.pop("_turn", wake_policy.UNKNOWN))}
    orchestrator, request_id = (bool(proof[0]), proof[1]) if proof else (False, None)
    store = config.open_store(cfg)
    if tool == "propagate_skills":
        summary = ag_wake._desktop_propagation_summary(
            project, session, cfg=cfg, active_workspace=args.get("workspace"))
        if (not summary or set(args.get("_native_roots", [])) != {str(root) for root in summary["roots"]}
                or args.get("_native_project_id") != summary["project_id"]):
            raise PermissionError("Desktop project workspace changed during propagation")
    else:
        summary = ag_wake._desktop_summary(project, session, cfg=cfg)
    if not summary:
        raise PermissionError("Desktop session changed during tool call")
    if tool == "propagate_skills":
        if args.get("mode") == "entry":
            from .project_entry import install
        else:
            from .propagation import install
        return install(project, args["sources"], "antigravity", workspace=args["workspace"], cfg=cfg)
    roles = store.roles(project, "antigravity")
    if tool in {"take_coordinator", "take_bridge"}:
        role = "coordinator" if tool == "take_coordinator" else "inbox"
        previous = roles[role]
        if role == "coordinator":
            takeover = store.take_coordinator(project, "antigravity", session,
                                              designate=bool(orchestrator), request_id=request_id)
            roles = takeover["roles"]
            orchestrator_status = takeover["orchestrator"]
        else:
            roles = store.bind(project, "antigravity", role, session, take=True)
        result = {"project":project,"harness":"antigravity","role":role,"previous":previous,
                  "roles":roles,"status":store.status(project,"antigravity"),
                  "pending":store.pending_overview(project,"antigravity",20)}
        if role == "coordinator":
            result["orchestrator"] = orchestrator_status
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
        return {"roles":roles, "status":store.status(project,"antigravity"),
                "orchestrator":store.orchestrator_status(project)}
    if tool == "pickup_notification":
        item = store.claim(project, "antigravity", role, session, message_id=args.get("notification_id"))
        if not item:
            return {"message":None}
        if role == "inbox":
            ok = store.finish(project,"antigravity",role,session,item["id"],item["lease"])
            result = {"message_id": item["id"], "forwarded": ok}
            if "request_id" in item:
                result.update(request_id=item["request_id"], stage=item["stage"])
            return result
        return {"message":item, "next":"Call acknowledge after recording receipt. Use reply_free_text for a request with a request_id."}
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
                          reply_to=args.get("reply_to"),session=session,**sent_from)
    if tool == "send_fanout":
        return store.send_fanout(project, "antigravity", args["recipients"], args["body"], args["key"],
                                 session=session, **sent_from)
    if tool == "reply_free_text":
        return store.reply_free_text(project, "antigravity", args["reply_to"], args["body"], args["key"],
                                     session=session, **sent_from)
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
    def send_fanout(recipients: list[str], body: str, key: str, desktop_grant: str = "") -> dict:
        """Send one free-text request to all named peer harnesses in this project. Reuse the key on retry."""
        return invoke(cfg, project, "send_fanout", desktop_grant)

    @server.tool()
    def reply_free_text(reply_to: str, body: str, key: str, desktop_grant: str = "") -> dict:
        """Send a correlated reply to a received request. Use its delivery ID and reuse the reply key on retry."""
        return invoke(cfg, project, "reply_free_text", desktop_grant)

    @server.tool()
    def set_pickup(enabled: bool, desktop_grant: str = "") -> dict:
        """Stop or resume the bound project and harness."""
        return invoke(cfg, project, "set_pickup", desktop_grant)

    @server.tool()
    def propagate_skills(sources: list[str], mode: str = "auto", workspace: str = "",
                         desktop_grant: str = "") -> dict:
        """Provision named project skills or the declared entry after native request verification."""
        return invoke(cfg, project, "propagate_skills", desktop_grant)
    return server


def installation_entries(cfg, project, *, executable=None, tools=TOOLS):
    """Build the managed server and matching one-use grant hook registrations."""
    executable = str(executable or sys.executable)

    def invocation(action):
        return [executable, "-m", "multi_harness_bridge.ag_inbox", "--home", str(cfg.root), project, action]

    cli = invocation("hook")
    shell_command = subprocess.list2cmdline(cli) if os.name == "nt" else shlex.join(cli)
    server = {"command":executable, "args":invocation("serve")[1:], "tools":{t:{"eager":True} for t in tools}}
    hook = {"PreToolUse":[{"matcher":".*(?:"+"|".join(tools)+")$",
                           "hooks":[{"type":"command","command":shell_command,"timeout":5}]}]}
    return server, hook


def install(cfg, project, directory=None):
    cfg.require(project)
    root = Path(directory or Path.home()/".gemini/config")
    root.mkdir(parents=True, exist_ok=True)
    name = server_name(project)
    server, hook = installation_entries(cfg, project)
    prior_server, prior_hook = installation_entries(cfg, project, tools=_LEGACY_TOOLS)

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
        if entries.get(name) not in (None, server, prior_server) or hooks.get(name) not in (None, hook, prior_hook):
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
