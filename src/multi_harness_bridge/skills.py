"""Project-scoped skill discovery, portability checks, and installation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from . import config
from .wake import WAKE_PROMPT, record_agent_relay

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
                                               cwd=Path.cwd(), expected_session=caller_session)
    if host == "devin":
        raise ValueError("Use this project's mhb_roles_<slug> native ACP tools for Devin role and mailbox operations")
    raise ValueError("Use this project's mhb_<slug> native Desktop MCP tools for Antigravity role and mailbox operations")


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
    config.open_store(cfg)
    if host == "claude":
        from .role_adapters import install_claude_hook
        with config.registry_writer(cfg.root):
            results = [install_claude_hook(workspace, state_home=cfg.root) for workspace in cfg.projects[project]]
        installed = any(result["status"] == "installed" for result in results)
        return {**results[0], "status": "installed" if installed else "already-current",
                "settings_files": [result["settings"] for result in results]}
    if host == "antigravity":
        from . import ag_inbox, ag_sidecar, ag_wake
        result = ag_inbox.install(cfg, project)
        result["wake_sidecar"] = ag_sidecar.install(cfg, project)
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
    invocation = _native_invocation(host, args.project)
    project = cfg.resolve(invocation.workspace, args.project)
    if project != invocation.project:
        raise ValueError("Native workspace and project selection disagree")
    store = config.open_store(cfg)
    roles = store.roles(project, host)
    role = next((name for name in ("coordinator", "inbox") if roles[name] == invocation.session), None)
    if role is None:
        raise ValueError("This verified native chat is not bound to a project role")
    if args.mailbox == "status":
        result = {"project": project, "harness": host, "role": role, "session": invocation.session,
                  "roles": roles, "queue": store.status(project, host)}
    elif args.mailbox == "claim":
        message = store.claim(project, host, role, invocation.session, message_id=args.message_id)
        result = {"project": project, "harness": host, "role": role, "message": message}
    elif args.mailbox == "finish":
        if not args.message_id:
            raise ValueError("--message-id is required for finish")
        lease = args.lease
        previous_claim = None
        if lease is None:
            # A claim made before the upgrade may still have an active cached lease.
            scope = "\0".join((project, host, role, invocation.session, args.message_id))
            previous_claim = cfg.root / "leases" / (hashlib.sha256(scope.encode()).hexdigest() + ".json")
            try:
                lease = json.loads(previous_claim.read_text())["lease"]
            except FileNotFoundError:
                raise ValueError("--lease from the claim result is required for finish") from None
        finished = store.finish(project, host, role, invocation.session, args.message_id, lease)
        if finished and previous_claim is not None:
            previous_claim.unlink(missing_ok=True)
        result = {"project": project, "role": role, "message_id": args.message_id, "finished": finished}
        if (finished and host == "codex" and role == "inbox"
                and config.project_option(cfg.root, project, "codex", "coordinator_wake") == "agent-relay"):
            trace = store.trace(project, args.message_id)
            if trace and trace.get("stage") == "coordinator":
                coordinator = store.roles(project, "codex").get("coordinator")
                if coordinator:
                    result["wake_relay"] = {"tool": "send_message_to_thread",
                                            "threadId": coordinator,
                                            "prompt": WAKE_PROMPT.format(notification_id=args.message_id)}
    elif args.mailbox == "relay-confirm":
        # The inbox agent confirms only after the relayed tool call succeeded;
        # until then the dispatcher's queued fallback stays eligible.
        if host != "codex":
            raise ValueError("relay-confirm is only valid for the codex harness")
        if role != "inbox":
            raise PermissionError("The bound inbox session relays coordinator wakes")
        if not args.message_id:
            raise ValueError("--message-id is required for relay-confirm")
        trace = store.trace(project, args.message_id)
        if not trace or trace.get("stage") != "coordinator" or trace.get("recipient") != host:
            raise ValueError("message is not awaiting this harness's coordinator")
        coordinator = store.roles(project, host).get("coordinator")
        if not coordinator:
            raise ValueError("no coordinator is bound for this harness")
        try:
            record_agent_relay(store, project, host, "coordinator", coordinator, args.message_id)
        except (OSError, sqlite3.Error) as exc:
            raise ValueError(f"relay confirmation could not be recorded ({type(exc).__name__})") from None
        result = {"project": project, "role": role, "message_id": args.message_id,
                  "relay_confirmed": True}
    elif args.mailbox in {"send", "send-fanout", "reply"}:
        if role != "coordinator":
            raise PermissionError("Coordinator ownership is required for sending and replying")
        body = args.body
        if args.body_file == "-":
            body = sys.stdin.buffer.read().decode("utf-8")
        elif args.body_file is not None:
            body = Path(args.body_file).read_bytes().decode("utf-8")
        if body is None or not args.key:
            raise ValueError("A message body and a stable --key are required")
        recipients = args.recipient or []
        if args.mailbox == "send":
            if len(recipients) != 1:
                raise ValueError("send takes exactly one --recipient")
            message = store.send(project, host, recipients[0], body, args.key,
                                 reply_to=args.reply_to, session=invocation.session)
            result = {"id": message["id"]}
        elif args.mailbox == "send-fanout":
            if not recipients or args.reply_to is not None:
                raise ValueError("send-fanout requires --recipient for each destination")
            result = store.send_fanout(project, host, recipients, body, args.key,
                                       session=invocation.session)
        else:
            if recipients or not args.reply_to:
                raise ValueError("reply requires only --reply-to to identify the original delivery")
            result = store.reply_free_text(project, host, args.reply_to, body, args.key,
                                          session=invocation.session)
        result = {"project": project, **result}
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
    parser.add_argument("--mailbox", choices=("status", "claim", "finish", "send", "send-fanout", "reply", "relay-confirm", "stop", "resume"), help="Use the mailbox as the role bound to this verified native chat")
    parser.add_argument("--message-id")
    parser.add_argument("--lease", help="Exact lease returned by the claim being finished")
    parser.add_argument("--recipient", action="append", help="Destination harness, repeat for send-fanout")
    body = parser.add_mutually_exclusive_group()
    body.add_argument("--body")
    body.add_argument("--body-file", help="Read the message as UTF-8, or use - for standard input")
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
            store = config.open_store(cfg)
            outcome = role_adapters.bind(store, invocation)
            if role == "coordinator":
                outcome["pending"] = store.pending(invocation.project, host, role, invocation.session)
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
