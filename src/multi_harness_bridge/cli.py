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
    for name in ("bind", "status", "send", "claim", "finish", "stop", "resume", "trace", "pending", "monitor"):
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
        if name in ("bind", "claim", "finish", "monitor"):
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


def run(args):
    if args.command == "self-test":
        return self_test()
    config = settings.load()
    if args.command == "project":
        if args.action == "add":
            config = settings.register(args.name, args.roots)
        return {name: [str(path) for path in paths] for name, paths in config.projects.items()}
    if args.command == "doctor":
        return {"platform": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                "state": str(config.root), "initialized": config.database.is_file(),
                "projects": {k: all(p.is_dir() for p in v) for k, v in config.projects.items()},
                "commands_on_path": {name: shutil.which(name) for name in (*HOSTS, "hermes")}}
    if args.command == "init":
        config.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        Store(config.database)
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
    store = Store(config.database)
    if args.command == "pending":
        return store.pending_overview(project, args.harness, args.limit)
    if args.command == "trace":
        return store.trace(project, args.id)
    harness = settings.identifier(args.harness)
    if args.command == "monitor":
        from .monitor import subscribe
        subscribe(config, store, project, harness, args.role, args.session, output=sys.stdout)
        return
    if args.command == "bind":
        return store.bind(project, harness, args.role, args.session)
    if args.command == "status":
        return {"project": project, "harness": harness, "roles": store.roles(project, harness),
                "queue": store.status(project, harness)}
    if args.command == "send":
        return store.send(project, harness, settings.identifier(args.recipient), args.body, args.key,
                          reply_to=args.reply_to)
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
