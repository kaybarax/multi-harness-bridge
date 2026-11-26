"""Operator commands for a local bridge installation."""
import argparse
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from itertools import permutations

from . import config as settings
from .store import Store
from .transport import Principal, issue, revoke, serve_mcp

HOSTS = ("codex", "claude", "devin", "antigravity")


def build_parser():
    parser = argparse.ArgumentParser(prog="mhb", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize private local state")
    commands.add_parser("doctor", help="Inspect runtime, projects, and optional executables")
    commands.add_parser("self-test", help="Run an isolated two-project mailbox round trip")
    project = commands.add_parser("project").add_subparsers(dest="action", required=True)
    project.add_parser("list")
    add = project.add_parser("add")
    add.add_argument("name")
    add.add_argument("roots", nargs="+", type=Path)
    orchestrator = commands.add_parser("orchestrator", help="Inspect or clear the optional project board owner")
    orchestrator_commands = orchestrator.add_subparsers(dest="action", required=True)
    orchestrator_status = orchestrator_commands.add_parser("status")
    orchestrator_status.add_argument("project")
    orchestrator_clear = orchestrator_commands.add_parser("clear")
    orchestrator_clear.add_argument("project")
    skill_entry = commands.add_parser("skill-entry", help="Validate or refresh repository-owned project skill pins")
    entry_actions = skill_entry.add_subparsers(dest="action", required=True)
    entry_refresh = entry_actions.add_parser("refresh", help="Recompute entry, index, and procedure hashes")
    entry_refresh.add_argument("project")
    entry_refresh.add_argument("--workspace", type=Path,
                               help="Registered workspace to refresh; defaults to the current directory")
    option = commands.add_parser("option", help="Read or change per-project harness options")
    option_cmds = option.add_subparsers(dest="action", required=True)
    listing = option_cmds.add_parser("list")
    listing.add_argument("project", nargs="?")
    setting = option_cmds.add_parser("set")
    setting.add_argument("project")
    setting.add_argument("harness", choices=HOSTS)
    setting.add_argument("key")
    setting.add_argument("value")
    removing = option_cmds.add_parser("unset")
    removing.add_argument("project")
    removing.add_argument("harness", choices=HOSTS)
    removing.add_argument("key")
    watch = commands.add_parser("watch", help="Inspect or start the project's shared watcher")
    watch.add_argument("project")
    watch.add_argument("action", choices=("status", "ensure"))
    pickup_cmd = commands.add_parser(
        "pickup", help="Ensure the watcher and print the listener for a bound chat (for take skills of other tools)")
    pickup_cmd.add_argument("project")
    pickup_cmd.add_argument("harness", choices=HOSTS)
    pickup_cmd.add_argument("role", choices=("coordinator", "inbox"))
    pickup_cmd.add_argument("--no-start", action="store_true", help="report watcher health without starting it")
    digest = commands.add_parser("digest", help="Show a bridge digest or switch digests on or off")
    digest.add_argument("project")
    digest.add_argument("harness", choices=HOSTS)
    digest.add_argument("action", choices=("show", "on", "off"), nargs="?", default="show")
    for name in ("bind", "status", "send", "claim", "finish", "stop", "resume", "trace", "pending",
                 "monitor", "listen"):
        cmd = commands.add_parser(name)
        cmd.add_argument("project")
        if name == "pending":
            cmd.add_argument("--harness")
            cmd.add_argument("--limit", type=int, default=30)
            continue
        if name == "trace":
            cmd.add_argument("id")
            continue
        cmd.add_argument("harness", help="Local actor name")
        if name in ("bind", "claim", "finish", "monitor", "listen"):
            cmd.add_argument("role", choices=("inbox", "coordinator"))
            cmd.add_argument("session")
        if name == "finish":
            cmd.add_argument("id")
            cmd.add_argument("lease")
        if name == "send":
            cmd.add_argument("recipient")
            cmd.add_argument("body")
            cmd.add_argument("--key", required=True, help="Stable retry key for this message")
            cmd.add_argument("--reply-to")
    credentials = commands.add_parser("credential").add_subparsers(dest="action", required=True)
    create = credentials.add_parser("issue")
    for name in ("project", "harness", "role", "session"):
        create.add_argument(name)
    create.add_argument("--out", required=True, type=Path, help="New private file for the bearer token")
    delete = credentials.add_parser("revoke")
    delete.add_argument("--file", required=True, type=Path)
    mcp = commands.add_parser("mcp", help="Start a role-scoped MCP server on standard I/O")
    mcp.add_argument("--credential", type=Path, required=True)
    a2a = commands.add_parser("serve", help="Start the A2A v1 mailbox on loopback")
    a2a.add_argument("--port", type=int, default=9417)
    return parser


def self_test():
    """Exercise the queue in temporary storage; connected apps are unnecessary."""
    checks = []
    with tempfile.TemporaryDirectory(prefix="mhb-check-") as directory:
        store = Store(Path(directory) / "demo.sqlite3")
        for project in ("orchard", "harbor"):
            for harness in ("codex", "devin", "antigravity"):
                for role in ("inbox", "coordinator"):
                    store.bind(project, harness, role, f"{project}-{harness}-{role}")
        def deliver(project, harness, expected):
            for role in ("inbox", "coordinator"):
                session = f"{project}-{harness}-{role}"
                item = store.claim(project, harness, role, session)
                if not item or item["id"] != expected:
                    raise RuntimeError("Mailbox delivered an unexpected item")
                if not store.finish(project, harness, role, session, item["id"], item["lease"]):
                    raise RuntimeError("Delivery lease was lost")
        for project in ("orchard", "harbor"):
            for origin, peer in permutations(("codex", "devin", "antigravity"), 2):
                key = f"{origin}-{peer}"
                request = store.send(project, origin, peer, "Receipt check", f"request-{key}")
                deliver(project, peer, request["id"])
                response = store.send(project, peer, origin, "Received", f"reply-{key}", reply_to=request["id"])
                deliver(project, origin, response["id"])
                checks.append({"project": project, "route": f"{origin} -> {peer} -> {origin}", "result": "ok"})
    return {"checks": checks}


def _action(args):
    """The wake-policy class of one mhb command."""
    command, action = args.command, getattr(args, "action", None)
    if command in {"project", "option", "watch", "digest", "orchestrator", "credential"} and action in {
            "list", "status", "show", None}:
        return "read"
    if command in {"project", "skill-entry", "option", "watch", "digest", "orchestrator", "credential",
                   "init", "stop", "resume", "serve", "mcp"}:
        return "settings"
    if command == "pickup":
        return "read" if args.no_start else "settings"
    if command == "bind":
        return "role"
    if command == "send":
        return "reply" if getattr(args, "reply_to", None) else "send"
    if command == "finish":
        return "finish"
    return "read"


def _enforce(config, args):
    """Refuse what the wake policy does not allow from the calling host turn."""
    from . import skills
    project = getattr(args, "project", None)
    if project is not None and project not in config.projects:
        project = None
    kind, _ = skills.enforce_caller(config, project, _action(args), reply_to=getattr(args, "reply_to", None))
    return kind


def run(args):
    if args.command == "self-test":
        return self_test()
    config = settings.load()
    turn = _enforce(config, args)
    if args.command == "project":
        if args.action == "add":
            config = settings.register(args.name, args.roots)
        return {name: [str(path) for path in paths] for name, paths in config.projects.items()}
    if args.command == "skill-entry":
        from . import project_entry
        project = config.require(args.project)
        workspace = (args.workspace or Path.cwd()).expanduser().resolve(strict=True)
        return project_entry.refresh(project, workspace=workspace, cfg=config)
    if args.command == "option":
        if args.action == "list":
            options = settings.load_options(config.root)
            if args.project is not None:
                options = {name: opts for name, opts in options.items() if name == args.project}
            return {"options": options}
        project = config.require(args.project)
        value = args.value if args.action == "set" else None
        options = settings.set_project_option(config.root, project, args.harness, args.key, value)
        result = {"options": options}
        if args.harness == "codex" and args.key == "coordinator_wake" and value == "agent-relay":
            result["codex_workspaces"] = [settings.provision_codex_workspace(path)
                                          for path in config.projects[project]]
        return result
    if args.command == "doctor":
        return {"platform": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                "state": str(config.root), "initialized": config.database.is_file(),
                "projects": {k: all(p.is_dir() for p in v) for k, v in config.projects.items()},
                "commands_on_path": {name: shutil.which(name) for name in (*HOSTS, "hermes")}}
    if args.command == "init":
        config.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        settings.open_store(config)
        return {"home": str(config.root), "database": str(config.database)}
    if not config.database.exists():
        raise ValueError("Initialize the bridge with mhb init")
    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            raise ValueError("Invalid listening port")
        try:
            from .a2a_server import application
            import uvicorn
            app = application(config, args.port)
        except ImportError as exc:
            raise RuntimeError("Install the a2a extra to start the HTTP service") from exc
        uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False)
        return
    if args.command == "mcp":
        serve_mcp(config, args.credential)
        return
    if args.command == "credential":
        if args.action == "revoke":
            return {"revoked": bool(revoke(config, args.file.read_text(encoding="ascii").strip()))}
        principal = Principal(args.project, args.harness, args.role, args.session)
        issue(config, principal, args.out)
        return {"credential_file": str(args.out.resolve()), "project": args.project, "role": args.role}
    project = config.require(args.project)
    if args.command == "watch":
        from . import watcher
        if args.action == "status":
            return watcher.health(config.root, project)
        return watcher.ensure(config.root, project)
    if args.command == "digest":
        from . import watcher
        return watcher.digest_command(config.root, project, args.harness, args.action)
    store = settings.open_store(config)
    if args.command == "pickup":
        # The attachment point for a project whose role handoff lives in another
        # tool: after that tool binds a chat, it calls this to start or verify
        # the watcher and to get the listener for the currently bound session.
        from . import pickup
        session = store.roles(project, args.harness).get(args.role)
        if not session:
            raise ValueError(f"No {args.role} is bound for {args.harness} in this project")
        return {"project": project, "harness": args.harness, "role": args.role, "session": session,
                **pickup.role_report(config, store, project, args.harness, args.role, session,
                                     config.projects[project][0], ensure_watcher=not args.no_start)}
    if args.command == "orchestrator":
        if args.action == "status":
            return store.project_status(project)
        return store.clear_orchestrator(project)
    if args.command == "pending":
        return store.pending_overview(project, args.harness, args.limit)
    if args.command == "trace":
        return store.trace(project, args.id)
    harness = settings.identifier(args.harness)
    if args.command == "monitor":
        from .monitor import subscribe
        subscribe(config, store, project, harness, args.role, args.session, output=sys.stdout)
        return
    if args.command == "listen":
        from .pickup import listen
        code = listen(config, store, project, harness, args.session, role=args.role, output=sys.stdout)
        if code:
            print(json.dumps({"mhb": "listener-exit", "reason": "session is not the bound role",
                              "rearm": False}), flush=True)
            raise SystemExit(code)
        return
    if args.command == "bind":
        return store.bind(project, harness, args.role, args.session)
    if args.command == "status":
        return {"project": project, "harness": harness, "roles": store.roles(project, harness),
                "queue": store.status(project, harness)}
    if args.command == "send":
        from . import wake_policy
        return store.send(project, harness, settings.identifier(args.recipient), args.body, args.key,
                          reply_to=args.reply_to, origin=wake_policy.origin(turn))
    if args.command == "claim":
        return {"message": store.claim(project, harness, args.role, args.session)}
    if args.command == "finish":
        accepted = store.finish(project, harness, args.role, args.session, args.id, args.lease)
        if not accepted:
            raise ValueError("Lease is stale or belongs to another delivery")
        return {"finished": True}
    return {"enabled": store.set_enabled(project, harness, args.command == "resume")}


def main():
    if os.name == "posix":
        os.umask(0o077)
    parser = build_parser()
    try:
        result = run(parser.parse_args())
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        parser.exit(1, f"mhb: {exc}\n")


if __name__ == "__main__":
    main()
