"""Preview and reconcile project-specific Devin registrations across workspace roots."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys

from . import config, devin_tools, legacy


DEVIN_SESSION = re.compile(r"(?:acp/devin-cli/)?[a-z0-9][a-z0-9-]{2,100}\Z")
DEVIN_MANIFEST = Path(".devin/mcp_config.local.json")


@dataclass(frozen=True)
class ManifestChange:
    path: Path
    original: bytes | None
    updated: dict
    servers: tuple[str, ...]

    @property
    def before_digest(self):
        return None if self.original is None else hashlib.sha256(self.original).hexdigest()


def _contents(path):
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"Refusing to reconcile a symlinked Devin manifest: {path}")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _legacy_registration(name, entry, project, legacy_root, roles):
    if name == f"{project}-local-coordinator":
        script = legacy_root / "devin_local_relay.py"
        args = [str(script), "serve-coordinator"]
        role = "coordinator"
    else:
        role = name.rsplit("-", 1)[-1]
        script = legacy_root / "devin_local_route_mcp.py"
        args = [str(script), role, project]
    current = entry.get("args", []) if isinstance(entry, dict) else []
    if (not isinstance(current, list) or current[:-1] != args or len(current) != len(args) + 1
            or not isinstance(current[-1], str) or not DEVIN_SESSION.fullmatch(current[-1])
            or entry.get("command") != str(legacy_root / ".venv/bin/python")):
        raise ValueError(f"Unrecognized legacy Devin registration {name}; preserving it")
    return {**entry, "args": [*args, roles[role]]}


def _legacy_handoff_registration(cfg, project, executable):
    return {
        "command": str(executable or sys.executable),
        "args": ["-m", "multi_harness_bridge.legacy_devin", "--home", str(cfg.root),
                 project, "serve-handoff"],
    }


def _plan_legacy_handoff(existing, target, project, legacy_root, predecessor):
    stock = {
        "command": str(legacy_root / ".venv/bin/python"),
        "args": [str(legacy_root / "devin_local_route_mcp.py"), "role-handoff", project],
    }
    if existing == stock:
        return target
    return _planned_registration(existing, target, f"devin-{project}-role-handoff", predecessor)


def _same_registration_home(existing, target):
    if not isinstance(existing, dict):
        return False
    args, wanted = existing.get("args"), target["args"]
    if (not isinstance(args, list) or len(args) != len(wanted)
            or not isinstance(args[3], str) or not Path(args[3]).is_absolute()):
        return False
    # Normalize only the generated --home value. All other fields, including
    # the venv launch path, must match the installed candidate or predecessor.
    if wanted[2] != "--home" or Path(args[3]).resolve() != Path(wanted[3]).resolve():
        return False
    return {**existing, "args": [*args[:3], wanted[3], *args[4:]]} == target


def _planned_registration(existing, target, name, predecessor):
    if existing is None or existing == target or _same_registration_home(existing, target):
        return target
    if predecessor and _same_registration_home(existing, {**target, "command": str(predecessor)}):
        return target
    raise ValueError(f"Unrecognized Devin registration {name}; preserving it")


def plan_devin(cfg, project, *, executable=None, predecessor=None, legacy_root=None, roles=None):
    """Build a preview without touching manifests, queues, roles, or native caches."""
    cfg.require(project)
    name = devin_tools.server_name(project)
    target = devin_tools.installation_entry(cfg, project, executable=executable)
    if legacy_root is not None:
        legacy_root = Path(legacy_root).resolve(strict=True)
        if (not isinstance(roles, dict) or set(roles) != {"coordinator", "inbox"}
                or not all(isinstance(session, str) and DEVIN_SESSION.fullmatch(session)
                           for session in roles.values())
                or len({session.removeprefix("acp/devin-cli/") for session in roles.values()}) != 2):
            raise ValueError("Reconciliation requires a current, distinct legacy Devin role pair")
    legacy_names = (
        f"{project}-local-coordinator",
        f"devin-{project}-route-coordinator",
        f"devin-{project}-route-inbox",
    )
    changes = []
    for root in cfg.projects[project]:
        path = root / DEVIN_MANIFEST
        original = _contents(path)
        try:
            manifest = {} if original is None else json.loads(original)
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError(f"Invalid Devin manifest; preserving {path}") from None
        if not isinstance(manifest, dict):
            raise ValueError(f"Expected a Devin manifest object in {path}")
        servers = manifest.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError(f"Expected a Devin mcpServers object in {path}")
        replacements = {name: _planned_registration(servers.get(name), target, name, predecessor)}
        if legacy_root is not None:
            for old_name in legacy_names:
                if old_name in servers:
                    replacements[old_name] = _legacy_registration(
                        old_name, servers[old_name], project, legacy_root, roles)
            handoff_name = f"devin-{project}-role-handoff"
            if handoff_name in servers:
                target_handoff = _legacy_handoff_registration(cfg, project, executable)
                replacements[handoff_name] = _plan_legacy_handoff(
                    servers[handoff_name], target_handoff, project, legacy_root, predecessor,
                )
        changed = tuple(key for key, entry in replacements.items() if servers.get(key) != entry)
        if changed:
            servers.update(replacements)
            changes.append(ManifestChange(path, original, manifest, changed))
    return tuple(changes)


def apply_devin(changes):
    """Check the complete preview for concurrent edits before writing any file."""
    for change in changes:
        if _contents(change.path) != change.original:
            raise ValueError(f"Devin manifest changed after preview: {change.path}")
    for change in changes:
        if _contents(change.path) != change.original:
            raise ValueError(f"Devin manifest changed while applying: {change.path}")
        change.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        config.atomic_json(change.path, change.updated)


def reconcile_devin(cfg, project, *, apply=False, **options):
    provider = legacy.load(cfg)
    guard = provider.guard(project, exclusive=apply) if provider else nullcontext()
    writer = config.registry_writer(cfg.root) if apply else nullcontext()
    with writer, guard:
        if config.load(cfg.root).projects != cfg.projects:
            raise ValueError("Project registry changed after preview")
        if provider:
            options.update(legacy_root=provider.installation,
                           roles=provider.roles(project, "devin"))
        changes = plan_devin(cfg, project, **options)
        if apply:
            apply_devin(changes)
        return changes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--executable", type=Path, default=Path(sys.executable))
    parser.add_argument("--previous-executable", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    cfg = config.load(args.home)
    changes = reconcile_devin(
        cfg, args.project, apply=args.apply, executable=args.executable,
        predecessor=args.previous_executable,
    )
    print(json.dumps({
        "project": args.project,
        "applied": args.apply,
        "changes": [{"file": str(change.path), "servers": change.servers,
                     "previous_sha256": change.before_digest} for change in changes],
    }))


if __name__ == "__main__":
    main()
