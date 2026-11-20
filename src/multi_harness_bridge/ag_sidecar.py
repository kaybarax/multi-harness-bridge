"""Submit ID-only wakes to bound Antigravity conversations from an App sidecar."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from . import ag_wake, config


def sidecar_id(project):
    return f"mhb-wake-{config.identifier(project)}"


def agentapi(project):
    """Use the public CLI and context that Antigravity supplies to this sidecar."""
    directory = Path(os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR", ""))
    expected = Path.home() / ".gemini/antigravity/sidecar_data" / sidecar_id(project) / "data"
    if not directory.is_absolute() or directory.resolve() != expected.resolve():
        return None
    return shutil.which("agentapi")


def send(project, role, session, notification_id, *, cfg=None, summary_db=None):
    """Ask the Desktop App to wake its existing conversation without UI input."""
    from .wake import WAKE_PROMPT, WakeDeferred, WakeError

    cfg = cfg or config.load()
    if not (command := agentapi(project)):
        raise WakeDeferred("Antigravity wakes run in the enabled Desktop sidecar for this project")
    notification_id = ag_wake._uuid(notification_id, "Notification ID")
    try:
        identity = ag_wake.verify(project, role, session, cfg=cfg, summary_db=summary_db)
        current = config.load(cfg.root)
        pending = current.projects.get(project) == cfg.projects[project] and config.open_store(current).pending(
            project, "antigravity", role, session, message_id=notification_id,
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        raise WakeDeferred("Cannot verify the live Antigravity sidecar destination") from None
    if not pending:
        raise WakeDeferred("Bridge pickup changed before the Antigravity sidecar wake")
    try:
        result = subprocess.run(
            [command, "send-message", session, WAKE_PROMPT.format(notification_id=notification_id)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12, check=False,
        )
    except FileNotFoundError:
        raise WakeDeferred("Antigravity's sidecar command is unavailable") from None
    except (OSError, subprocess.TimeoutExpired):
        raise WakeError("Antigravity sidecar submission outcome is uncertain") from None
    if result.returncode:
        raise WakeError("Antigravity sidecar did not confirm submission; reconcile before retrying")
    return {**identity, "notification_id": notification_id, "status": "sent"}


def install(cfg, project, *, directory=None, executable=None, enable=False):
    """Install the scoped sidecar definition, with explicit App activation."""
    cfg.require(project)
    name = sidecar_id(project)
    directory = Path(directory or Path.home() / ".gemini/config").expanduser()
    manifest = directory / "sidecars" / name / "sidecar.json"
    target = {
        "command": os.path.abspath(executable or sys.executable),
        "args": ["-m", "multi_harness_bridge.ag_sidecar", "--home", str(cfg.root), project, "run"],
        "display_name": f"Multi Harness Bridge ({project})",
        "description": "Wake existing, bound Antigravity inbox and coordinator chats for pending work.",
        "restart_policy": "on-failure",
    }
    if enable:
        if not ag_wake._project_ids(cfg).get(project):
            raise ValueError("Open the registered Antigravity Desktop project before enabling its sidecar")
    with config.registry_writer(directory):
        if manifest.exists() and json.loads(manifest.read_text(encoding="utf-8")) != target:
            raise ValueError("Conflicting Antigravity sidecar definition; preserving existing settings")
        app_config = directory / "config.json"
        settings = json.loads(app_config.read_text(encoding="utf-8")) if app_config.exists() else {}
        if not isinstance(settings, dict):
            raise ValueError("Invalid Antigravity configuration")
        sidecars = settings.setdefault("sidecars", {})
        if not isinstance(sidecars, dict):
            raise ValueError("Invalid Antigravity sidecar configuration")
        entry = sidecars.get(name, {})
        if not isinstance(entry, dict):
            raise ValueError("Conflicting Antigravity sidecar activation; preserving existing settings")
        if entry.get("enabled") is True and not manifest.exists() and not enable:
            raise ValueError("Confirm --enable before restoring an enabled Antigravity sidecar definition")
        manifest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not manifest.exists():
            config.atomic_json(manifest, target)
        if enable:
            # The App's projectId field scopes creation of new conversations.
            # This sidecar only sends to an existing, verified conversation ID.
            enabled_entry = {**entry, "enabled": True}
            if entry != enabled_entry:
                sidecars[name] = enabled_entry
                config.atomic_json(app_config, settings)
    return {"id": name, "manifest": str(manifest), "enabled": enable or entry.get("enabled") is True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    parser.add_argument("project")
    parser.add_argument("action", choices=("install", "run"))
    parser.add_argument("--enable", action="store_true", help="Enable the installed Antigravity App sidecar")
    args = parser.parse_args(argv)
    cfg = config.load(args.home)
    project = cfg.require(args.project)
    if args.action == "install":
        print(json.dumps(install(cfg, project, enable=args.enable)))
        return 0
    if not agentapi(project):
        parser.error("Run this worker in its Antigravity App sidecar")
    from . import wake

    os.environ["MHB_HOME"] = str(cfg.root)
    os.chdir(cfg.projects[project][0])
    return wake.main(["--project", project, "--harness", "antigravity"])


if __name__ == "__main__":
    raise SystemExit(main())
