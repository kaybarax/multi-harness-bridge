"""Verified macOS Antigravity Desktop role checks, wakes, and title changes.

The helper sends only a fixed mailbox wake notice containing the notification ID.
It never reads or submits mailbox content, claims work, or acknowledges it.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

from . import config
from .store import Store

HARNESS = "antigravity"
SUMMARY_DB = Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
HELPER_NAME = "antigravity-ax"
BUNDLE_ID = "com.google.antigravity"
SERVER_LABEL = "local.multi-harness-bridge.antigravity-wake"


def _error(message):
    from .wake import WakeError
    return WakeError(message)


def _uuid(value, label):
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{label} must be a UUID") from None
    if canonical != value:
        raise ValueError(f"{label} must be a canonical lowercase UUID")
    return canonical


def _local_path(value):
    if not isinstance(value, str) or not value:
        return None
    from urllib.parse import unquote, urlsplit
    uri = urlsplit(value)
    if uri.scheme:
        if uri.scheme != "file" or uri.netloc or uri.query or uri.fragment:
            return None
        value = unquote(uri.path)
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else None


def _summary_path():
    return Path(os.environ.get("MHB_ANTIGRAVITY_SUMMARIES_DB", SUMMARY_DB)).expanduser().resolve()


def _project_ids(cfg, db_path=None):
    """Resolve one unique native Antigravity project ID per configured root set."""
    path = Path(db_path).expanduser().resolve() if db_path else _summary_path()
    if not path.is_file():
        raise ValueError("Antigravity native conversation summary database is unavailable")
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(conversation_summaries)")}
            required = {"conversation_id", "title", "workspace_uris", "project_id", "app_data_dir"}
            if not required.issubset(columns):
                raise ValueError("Unsupported Antigravity native summary schema")
            rows = db.execute("SELECT title,workspace_uris,project_id,app_data_dir FROM conversation_summaries").fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f"Unable to inspect Antigravity native summary database ({type(exc).__name__})") from None
    candidates = {project: set() for project in cfg.projects}
    for _, workspace_uris, project_id, app_data_dir in rows:
        if app_data_dir != "antigravity" or not isinstance(project_id, str):
            continue
        try:
            values = json.loads(workspace_uris)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
            continue
        roots = {_local_path(value) for value in values}
        if None in roots:
            continue
        for project, configured in cfg.projects.items():
            if roots == set(configured):
                candidates[project].add(project_id)
    resolved = {}
    for project, ids in candidates.items():
        if len(ids) > 1:
            raise ValueError(f"Antigravity project ID is ambiguous for registered project {project!r}")
        if ids:
            native_id = next(iter(ids))
            try:
                _uuid(native_id, "Antigravity project ID")
            except ValueError:
                raise ValueError(f"Invalid Antigravity project ID for registered project {project!r}") from None
            resolved[project] = native_id
    reverse = {}
    for project, native_id in resolved.items():
        if native_id in reverse:
            raise ValueError("Antigravity project IDs collide across registered workspaces")
        reverse[native_id] = project
    return resolved


def _desktop_summary(project, conversation, *, cfg=None, db_path=None):
    """Return title and exact native workspace metadata only for the requested project."""
    cfg = cfg or config.load()
    cfg.require(project)
    conversation = _uuid(conversation, "Antigravity conversation ID")
    path = Path(db_path).expanduser().resolve() if db_path else _summary_path()
    ids = _project_ids(cfg, path)
    expected_id = ids.get(project)
    if expected_id is None:
        return None
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)) as db:
            row = db.execute("""SELECT title,workspace_uris,project_id,app_data_dir
                FROM conversation_summaries WHERE conversation_id=?""", (conversation,)).fetchone()
    except sqlite3.Error as exc:
        raise ValueError(f"Unable to inspect Antigravity conversation summary ({type(exc).__name__})") from None
    if not row or not isinstance(row[0], str) or not row[0].strip():
        return None
    try:
        values = json.loads(row[1])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
        return None
    roots = {_local_path(value) for value in values}
    configured = set(cfg.projects[project])
    if (None in roots or roots != configured or row[2] != expected_id
            or row[3] != "antigravity"):
        return None
    return {"title": row[0], "roots": roots, "project_id": expected_id}


def _native_role_snapshot(cfg=None, *, summary_db=None):
    """Validate native role bindings and collision-free project identity mappings."""
    cfg = cfg or config.load()
    project_ids = _project_ids(cfg, summary_db)
    store = Store(cfg.database)
    snapshot = {}
    seen = set()
    for project in sorted(cfg.projects):
        native_id = project_ids.get(project)
        if native_id is None:
            continue
        bound = store.roles(project, HARNESS)
        roles = {}
        for role in ("inbox", "coordinator"):
            conversation = bound.get(role)
            if conversation is None:
                continue
            conversation = _uuid(conversation, "Bound Antigravity conversation ID")
            if conversation in seen:
                raise ValueError("Antigravity role conversations must be unique across projects")
            summary = _desktop_summary(project, conversation, cfg=cfg, db_path=summary_db)
            if not summary or summary["project_id"] != native_id:
                raise ValueError(f"Bound Antigravity {role} does not match its registered workspace")
            roles[role] = {"conversation": conversation, "title": summary["title"], "project_id": native_id}
            seen.add(conversation)
        if roles:
            snapshot[project] = roles
    return snapshot


def _write_ax_context(directory, *, project, action, role=None, session=None,
                      title=None, notification_id=None, cfg=None, summary_db=None):
    """Write short-lived owner-only role context consumed by the native helper."""
    if action not in {"verify-bindings", "wake", "rename-project"}:
        raise ValueError("Unsupported Antigravity Accessibility operation")
    snapshot = _native_role_snapshot(cfg, summary_db=summary_db)
    if project not in snapshot:
        raise ValueError("No verified Antigravity roles are registered for this project")
    if action != "verify-bindings":
        if role not in {"coordinator", "inbox"} or session is None:
            raise ValueError("Antigravity operation needs an exact bound role and session")
        session = _uuid(session, "Antigravity session")
        if snapshot[project].get(role, {}).get("conversation") != session:
            raise ValueError("Antigravity session is not bound to this project role")
    if action == "rename-project" and (not isinstance(title, str) or not title.strip()
            or len(title) > 240 or any(ord(char) < 32 for char in title)):
        raise ValueError("Requested Antigravity title is invalid")
    if action == "wake":
        notification_id = _uuid(notification_id, "Notification ID")
    payload = {"schema": "antigravity-native-roles/v1", "created_at": time.time(),
        "project_key": project, "action": action, "role": role, "conversation": session,
        "requested_title": title, "operation_identity": notification_id if action == "wake" else session,
        "operation_argument": session if action == "wake" else title,
        "roles": snapshot}
    directory = Path(directory)
    target, temporary = directory / "native-role-context.json", directory / ".native-role-context.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _helper_path(cfg=None):
    cfg = cfg or config.load()
    return cfg.root / "native" / HELPER_NAME


def install_helper(cfg=None):
    """Compile and ad-hoc sign a stable macOS AX helper installation."""
    if sys.platform != "darwin":
        raise ValueError("Antigravity Desktop Accessibility automation is macOS-only")
    cfg = cfg or config.load()
    helper = _helper_path(cfg)
    helper.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    helper.parent.chmod(0o700)
    source = Path(__file__).with_name("assets") / "antigravity_ax.swift"
    if not source.is_file():
        raise ValueError("Packaged Antigravity AX helper source is missing")
    with tempfile.TemporaryDirectory(prefix=".antigravity-ax-", dir=helper.parent) as directory:
        temporary = Path(directory) / HELPER_NAME
        subprocess.run(["/usr/bin/swiftc", str(source), "-framework", "AppKit",
                        "-framework", "ApplicationServices", "-o", str(temporary)],
                       check=True, capture_output=True, text=True, timeout=90)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier",
                        "com.multi-harness-bridge.antigravity-ax", str(temporary)],
                       check=True, capture_output=True, text=True, timeout=30)
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(temporary)],
                       check=True, capture_output=True, text=True, timeout=30)
        os.chmod(temporary, 0o700)
        os.replace(temporary, helper)
    return helper


def _run_helper(project, role, session, action, argument, *, title=None, cfg=None, summary_db=None):
    if sys.platform != "darwin":
        raise ValueError("Antigravity Desktop Accessibility automation is macOS-only")
    cfg = cfg or config.load()
    helper = _helper_path(cfg)
    if not helper.is_file():
        raise ValueError("Antigravity AX helper is not installed; run `python -m multi_harness_bridge.ag_wake install`")
    # launchd executes the signed helper so macOS can apply its Accessibility grant.
    with tempfile.TemporaryDirectory(prefix="ag-wake-", dir=helper.parent) as directory:
        root = Path(directory)
        context = _write_ax_context(root, project=project, action=action, role=role,
            session=session, title=title, notification_id=argument if action == "wake" else None,
            cfg=cfg, summary_db=summary_db)
        stdout, stderr = root / "stdout", root / "stderr"
        label = f"{SERVER_LABEL}.{project}.{role or 'verify'}"
        plist_path = root / "job.plist"
        argv = [str(helper), action]
        if action != "verify-bindings":
            argv.extend([project, role, session, argument if action == "wake" else title])
        with plist_path.open("wb") as stream:
            plistlib.dump({"Label": label, "ProgramArguments": argv, "RunAtLoad": True,
                "Umask": 63, "StandardOutPath": str(stdout), "StandardErrorPath": str(stderr),
                "EnvironmentVariables": {"ANTIGRAVITY_AX_NATIVE_CONTEXT": str(context)}}, stream)
        domain = f"gui/{os.getuid()}"
        start = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist_path)],
                               capture_output=True, text=True, timeout=8)
        if start.returncode:
            raise _error("Could not register the one-shot Antigravity AX helper; reconcile before retrying")
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                status = subprocess.run(["/bin/launchctl", "print", f"{domain}/{label}"],
                                        capture_output=True, text=True, timeout=4)
                match = re.search(r"last exit code = (-?\d+)", status.stdout)
                if match:
                    code = int(match.group(1))
                    detail = " ".join(path.read_text(encoding="utf-8") for path in (stdout, stderr) if path.exists()).strip()
                    if code in (2, 3) and action == "wake":
                        from .wake import WakeDeferred, WakeRejected
                        if any(phrase in detail for phrase in ("recent user input", "contains a draft",
                                "already working", "active dialog", "Leave one Antigravity window")):
                            raise WakeDeferred(detail)
                        raise WakeRejected(detail or "Desktop refused the wake before submission")
                    if code:
                        raise _error(detail or "Antigravity AX helper rejected the operation")
                    return detail
                time.sleep(0.2)
            raise _error("Antigravity AX helper did not return a verified result; delivery is uncertain")
        finally:
            subprocess.run(["/bin/launchctl", "bootout", f"{domain}/{label}"],
                           capture_output=True, timeout=5)


def verify(project, role, session, *, cfg=None, summary_db=None):
    """Confirm the native Antigravity summary and exact bridge role binding."""
    cfg = cfg or config.load()
    cfg.require(project)
    session = _uuid(session, "Antigravity session")
    roles = Store(cfg.database).roles(project, HARNESS)
    if roles.get(role) != session or role not in {"coordinator", "inbox"}:
        raise _error("Antigravity session is not the exact bound role")
    native = _desktop_summary(project, session, cfg=cfg, db_path=summary_db)
    if not native:
        raise _error("Antigravity conversation does not match its registered native workspace")
    return {"project": project, "harness": HARNESS, "role": role, "session": session}


def wake(project, role, session, notification_id, *, cfg=None, summary_db=None):
    """Send one ID-only wake prompt to the exact bound Desktop conversation."""
    identity = verify(project, role, session, cfg=cfg, summary_db=summary_db)
    notification_id = _uuid(notification_id, "Notification ID")
    _run_helper(project, role, session, "wake", notification_id, cfg=cfg, summary_db=summary_db)
    # Check again after the native send; caller marks any uncertainty permanent.
    verify(project, role, session, cfg=cfg, summary_db=summary_db)
    return {**identity, "notification_id": notification_id, "status": "sent"}


def rename(project, role, session, title, *, cfg=None, summary_db=None):
    """Rename the exact active bound conversation through Antigravity's native UI."""
    verify(project, role, session, cfg=cfg, summary_db=summary_db)
    _run_helper(project, role, session, "rename-project", session, title=title,
                cfg=cfg, summary_db=summary_db)
    verify(project, role, session, cfg=cfg, summary_db=summary_db)
    return title


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install", help="Compile and sign the macOS Accessibility helper")
    args = parser.parse_args(argv)
    if args.command == "install":
        path = install_helper()
        print(f"Installed signed Antigravity Accessibility helper: {path}")
        print("Grant Accessibility access in System Settings → Privacy & Security → Accessibility, then run `multi-harness-wake --harness antigravity --once`.")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
