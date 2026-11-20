"""Project-scoped skill discovery, portability checks, and installation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from . import config

from .propagation import (BUILTIN_ROOT, HARNESS_DIRS, MANIFEST, _blockers, _skills,
                          bootstrap, digest, install)


def invoking_harness() -> str:
    value = os.environ.get("MULTI_HARNESS_BRIDGE_HARNESS")
    markers = {
        "codex": bool(os.environ.get("CODEX_THREAD_ID")),
        "claude": bool(os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDECODE")),
        "devin": bool(os.environ.get("DEVIN_SESSION_ID") or os.environ.get("DEVIN_LOCAL_SESSION_ID")),
        "antigravity": bool(os.environ.get("MHB_ANTIGRAVITY_EVENT")),
    }
    detected = [name for name, present in markers.items() if present]
    if value in HARNESS_DIRS:
        if detected and detected != [value]:
            raise ValueError("Configured native harness identity conflicts with active host markers")
        return value
    if len(detected) == 1:
        return detected[0]
    if len(detected) > 1:
        raise ValueError("Multiple native harness identities are active")
    raise ValueError("No trusted native harness identity; invoke through that harness's registered adapter")


def _native_invocation(host, project_hint=None, *, requested_role=None):
    from . import role_adapters
    if host == "codex":
        return role_adapters.codex_invocation(project_hint=project_hint)
    if host == "claude":
        caller_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if not caller_session:
            raise ValueError("Claude current session identity is unavailable")
        context_path = os.environ.get("MHB_CLAUDE_HOOK_CONTEXT")
        path = Path(context_path).expanduser() if context_path else cfg_context_path("claude", caller_session)
        context = json.loads(path.read_text())
        if requested_role is not None and time.time() - float(context.get("captured_at", 0)) > 120:
            raise ValueError("Claude native prompt context is stale; submit a fresh user turn")
        return role_adapters.claude_invocation(requested_role, context, project_hint=project_hint,
                                               expected_session=caller_session)
    if host == "devin":
        raise ValueError("Use this project's mhb_roles_<slug> native ACP tools for Devin role and mailbox operations")
    event_path = os.environ.get("MHB_ANTIGRAVITY_EVENT")
    if not event_path:
        raise ValueError("Antigravity requires current native event context")
    return role_adapters.antigravity_native_invocation(None, json.loads(Path(event_path).read_text()), project_hint=project_hint)


def cfg_context_path(host, session=None):
    if host == "claude":
        if not session:
            raise ValueError("Claude session identity is required to locate native context")
        key = hashlib.sha256(session.encode("utf-8")).hexdigest()[:32]
        return config.home() / "native-context" / f"claude-{key}.json"
    return config.home() / "native-context" / f"{host}-current.json"


def install_native(project, host, cfg):
    """Install only host-owned project hooks/helpers; preserve existing config."""
    cfg.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    from .store import Store
    Store(cfg.database)
    if host == "claude":
        from .role_adapters import install_claude_hook
        return install_claude_hook(cfg.projects[project][0])
    if host == "antigravity":
        from . import ag_inbox, ag_wake
        result = ag_inbox.install(cfg, project)
        try:
            result["accessibility_helper"] = str(ag_wake.install_helper(cfg))
            result["helper_status"] = "installed"
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            result["helper_status"] = "failed"
            result["helper_error"] = type(exc).__name__
        return result
    if host == "devin":
        from .devin_tools import install as install_devin
        return install_devin(cfg, project)
    if host == "codex":
        return {"host": host, "status": "native-thread-verifier-ready",
                "detail": "Uses CODEX_THREAD_ID and the native thread database; no host config is changed."}
    raise ValueError("Unsupported host")


def _mailbox(args, host, cfg):
    from .store import Store
    invocation = _native_invocation(host, args.project)
    project = cfg.resolve(invocation.workspace, args.project)
    if project != invocation.project:
        raise ValueError("Native workspace and project selection disagree")
    store = Store(cfg.database)
    roles = store.roles(project, host)
    role = next((name for name in ("coordinator", "inbox") if roles[name] == invocation.session), None)
    if role is None:
        raise ValueError("This verified native chat is not bound to a project role")
    if args.mailbox == "status":
        result = {"project": project, "harness": host, "role": role, "session": invocation.session,
                  "roles": roles, "queue": store.status(project, host)}
    elif args.mailbox == "claim":
        message = store.claim(project, host, role, invocation.session)
        if message:
            lease_dir = cfg.root / "leases"
            lease_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            lease_path = lease_dir / (hashlib.sha256("\0".join((project,host,role,invocation.session,message["id"])).encode()).hexdigest() + ".json")
            config.atomic_json(lease_path, {"id": message["id"], "lease": message["lease"], "expires": time.time()+30})
            message.pop("lease", None)
        result = {"project": project, "harness": host, "role": role, "message": message}
    elif args.mailbox == "finish":
        if not args.message_id:
            raise ValueError("--message-id is required for finish")
        lease_dir = cfg.root / "leases"
        path = lease_dir / (hashlib.sha256("\0".join((project,host,role,invocation.session,args.message_id)).encode()).hexdigest() + ".json")
        record = json.loads(path.read_text())
        finished = store.finish(project, host, role, invocation.session, args.message_id, record["lease"])
        if finished:
            path.unlink(missing_ok=True)
        result = {"project": project, "role": role, "message_id": args.message_id, "finished": finished}
    elif args.mailbox == "send":
        if not args.recipient or args.body is None:
            raise ValueError("--recipient and --body are required for send")
        if not args.key:
            raise ValueError("--key is required so retries are idempotent")
        result = {"project": project, "id": store.send(project, host, args.recipient, args.body,
                                                           args.key, reply_to=args.reply_to,
                                                           session=invocation.session)["id"]}
    elif args.mailbox in {"stop", "resume"}:
        enabled = args.mailbox == "resume"
        store.set_enabled(project, host, enabled, session=invocation.session)
        result = {"project": project, "harness": host, "enabled": enabled}
    else:
        raise ValueError("Unsupported mailbox operation")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="sources", nargs="+", choices=sorted(HARNESS_DIRS))
    parser.add_argument("--project")
    parser.add_argument("--to", choices=sorted(HARNESS_DIRS))
    parser.add_argument("--bootstrap", action="store_true", help="Install packaged bridge skills")
    parser.add_argument("--install-native", action="store_true", help="Install supported host-local hook/helper")
    parser.add_argument("--take", choices=("coordinator", "bridge"), help="Transfer the current verified native chat to this role")
    parser.add_argument("--hint", help="Disambiguate a project matching the current native workspace")
    parser.add_argument("--mailbox", choices=("status", "claim", "finish", "send", "stop", "resume"), help="Use the mailbox as the role bound to this verified native chat")
    parser.add_argument("--message-id")
    parser.add_argument("--recipient")
    parser.add_argument("--body")
    parser.add_argument("--key")
    parser.add_argument("--reply-to")
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
        if args.mailbox or args.take:
            if args.to:
                raise ValueError("--to cannot select a caller for role transfer or mailbox operations")
            host = invoking_harness()
        elif args.bootstrap or args.install_native:
            host = args.to or invoking_harness()
        else:
            host = invoking_harness()
            if args.to and args.to != host:
                raise ValueError("Propagation destination must match the invoking native harness")
        if args.mailbox:
            print(json.dumps(_mailbox(args, host, cfg), indent=2))
            return
        if args.take:
            from . import role_adapters
            role = "inbox" if args.take == "bridge" else "coordinator"
            if host == "codex":
                invocation = role_adapters.codex_invocation(role, project_hint=args.hint or args.project)
            elif host == "claude":
                caller_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
                if not caller_session:
                    raise ValueError("Claude current session identity is unavailable")
                context_path = os.environ.get("MHB_CLAUDE_HOOK_CONTEXT")
                path = Path(context_path).expanduser() if context_path else cfg_context_path("claude", caller_session)
                context = json.loads(path.read_text())
                if time.time() - float(context.get("captured_at", 0)) > 120:
                    raise ValueError("Claude native prompt context is stale; submit a fresh direct user request")
                invocation = role_adapters.claude_invocation(role, context, project_hint=args.hint or args.project,
                                                             expected_session=caller_session)
            else:
                raise ValueError(f"Use this project's native {host} MCP role tools for the takeover")
            if cfg.resolve(invocation.workspace, args.project or args.hint) != invocation.project:
                raise ValueError("Native workspace does not match project selection")
            from .store import Store
            outcome = role_adapters.bind(Store(cfg.database), invocation)
            if role == "coordinator":
                outcome["pending"] = Store(cfg.database).pending(invocation.project, host, role, invocation.session)
            outcome["title"] = role_adapters.apply_native_title(invocation)
            print(json.dumps(outcome, indent=2))
            return
        project = cfg.resolve(Path.cwd(), args.project)
        if args.bootstrap:
            result = bootstrap(project, host)
            try:
                result["native"] = install_native(project, host, cfg)
            except (OSError, ValueError, RuntimeError) as exc:
                result["native"] = {"host": host, "status": "failed", "reason": type(exc).__name__}
            print(json.dumps(result, indent=2))
        elif args.install_native:
            print(json.dumps(install_native(project, host, cfg), indent=2))
        else:
            if not args.sources:
                raise ValueError("--from requires one or more source harnesses")
            print(json.dumps(install(project, args.sources, host), indent=2))
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
