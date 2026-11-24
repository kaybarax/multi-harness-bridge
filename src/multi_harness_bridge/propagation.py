"""Install project-attributed skills with content provenance and reviewed variants."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

from . import config

HARNESS_DIRS = dict(codex=".agents/skills", claude=".claude/skills", devin=".agents/skills", antigravity=".agents/skills")
GLOBAL_DIRS = {"codex": Path.home()/".codex/skills", "claude": Path.home()/".claude/skills",
               "devin": Path.home()/".config/devin/skills", "antigravity": Path.home()/".gemini/config/skills"}
MANIFEST = ".multi-harness-skills.json"
BUILTIN_ROOT = Path(__file__).with_name("builtin_skills")


def _skills(path):
    if not path.is_dir():
        return []
    return sorted(p for p in path.iterdir() if not p.name.startswith(".") and (p/"SKILL.md").is_file())


def _files(path):
    for p in sorted(path.rglob("*")):
        if p.is_symlink():
            # A checkout may link a skill directory to its canonical source.
            # Within the bundle, require ordinary portable files.
            raise ValueError(f"Nested symlink needs review: {p.relative_to(path)}")
        if p.is_file():
            yield p
        elif not p.is_dir():
            raise ValueError(f"Unsupported file type in skill: {p.relative_to(path)}")


def digest(path):
    h = hashlib.sha256()
    for p in _files(path):
        h.update(p.relative_to(path).as_posix().encode() + b"\0")
        with p.open("rb") as contents:
            h.update(hashlib.file_digest(contents, "sha256").digest())
        # Scripts in a bundle can depend on retaining their executable bit.
        if os.name == "posix":
            h.update(b"X" if p.stat().st_mode & 0o111 else b"-")
    return h.hexdigest()


def _blockers(skill, target):
    foreign = set(HARNESS_DIRS) - {target}
    files = _files(skill) if skill.is_dir() else (skill,)
    for p in files:
        if p.suffix.lower() not in (".md", ".txt", ".py", ".js", ".sh", ".ps1", ".json", ".toml"):
            continue
        relative = p.relative_to(skill).as_posix() if skill.is_dir() else p.name
        words = relative + "\n" + p.read_text(encoding="utf-8", errors="replace")
        for host in sorted(foreign):
            if re.search(r"(?<![\w])" + re.escape(host) + r"(?![\w])", words, re.I):
                return [f"{p.name} references {host}. Supply a reviewed variant for {target}."]
    return []


def _manifest(path):
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError("Review symlinked skill manifest before writing")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
        raise ValueError("Invalid skill manifest")
    return data


def _inventory(roots, harnesses, target, manifest, *, include_managed=False):
    inventory = defaultdict(list)
    review = []
    for root in roots:
        registry = root / ".bridge-skills" / "sources.json"
        extra = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {}
        if not isinstance(extra, dict) or set(extra) - set(HARNESS_DIRS):
            raise ValueError("Invalid .bridge-skills/sources.json")
        for host in harnesses:
            paths = extra.get(host, [])
            if not isinstance(paths, list) or any(not isinstance(p, str) or not p.strip() for p in paths):
                raise ValueError("Skill source roots must be a list of directory paths")
            registered = tuple((root / Path(p).expanduser()).resolve(strict=True) for p in paths)
            allowed = (*roots, *registered)
            local = root / HARNESS_DIRS[host]
            for directory in (local, *registered, GLOBAL_DIRS[host]):
                provenance = "shared" if directory == root / ".agents/skills" else host
                for item in _skills(directory):
                    src = item.resolve()
                    if not any(src.is_relative_to(r) for r in allowed):
                        if directory != GLOBAL_DIRS[host]:
                            review.append({"skill":item.name, "status":"external-link-needs-registration"})
                        continue
                    if not include_managed and item.parent.resolve() == target and item.name in manifest:
                        continue
                    try:
                        fingerprint = digest(src)
                    except ValueError as exc:
                        review.append({"skill":item.name, "status":"blocked-review", "reason":str(exc)})
                        continue
                    inventory[item.name].append((src, fingerprint, provenance))
    return inventory, review


def _replace(source, dest):
    backup = dest.with_name(".mhb-previous-" + dest.name)
    if backup.exists():
        raise ValueError(f"Previous copy requires reconciliation: {backup}")
    with tempfile.TemporaryDirectory(prefix=".mhb-stage-", dir=dest.parent) as stage:
        ready = Path(stage)/"ready"
        shutil.copytree(source, ready)
        if digest(ready) != digest(source):
            raise ValueError("Skill source changed during copy. Retry.")
        if dest.exists():
            os.replace(dest, backup)
        try:
            os.replace(ready, dest)
        except BaseException:
            if backup.exists():
                os.replace(backup, dest)
            raise
    return backup


def resolve_workspace(project, cfg, workspace=None):
    """Resolve the registered workspace containing the active project checkout."""
    cfg.require(project)
    roots = tuple(path.resolve() for path in cfg.projects[project])
    if workspace is None:
        if len(roots) != 1:
            raise ValueError("This project has multiple workspaces; provide the active workspace")
        active = roots[0]
    else:
        active = Path(workspace).expanduser().resolve(strict=True)
    if not active.is_dir() or cfg.resolve(active, project) != project:
        raise ValueError("The active workspace does not belong to the registered project")
    candidates = [root for root in roots if active.is_relative_to(root)]
    if not candidates:
        raise ValueError("The active workspace is outside the registered project workspaces")
    workspace_root = max(candidates, key=lambda root: len(root.parts))
    if any(workspace_root.is_relative_to(other.resolve()) or other.resolve().is_relative_to(workspace_root)
           for name, group in cfg.projects.items() if name != project for other in group):
        raise ValueError("Skill destination needs a workspace exclusive to this project")
    return workspace_root, roots


def _install(project, harnesses, target_host, project_roots, builtin, cfg=None, workspace=None):
    cfg = cfg or config.load()
    cfg.require(project)
    if target_host not in HARNESS_DIRS or (not builtin and (not harnesses or set(harnesses)-set(HARNESS_DIRS))):
        raise ValueError("Unknown or empty harness selection")
    roots = tuple(p.resolve() for p in cfg.projects[project])
    if project_roots and tuple(Path(p).resolve() for p in project_roots) != roots:
        raise ValueError("Workspace roots must match the registered project")
    workspace, roots = resolve_workspace(project, cfg, workspace)
    target = (workspace/HARNESS_DIRS[target_host]).resolve()
    if not target.is_relative_to(workspace):
        raise ValueError("Skill destination needs a workspace exclusive to this project")
    lock_key = hashlib.sha256(os.fsencode(target)).hexdigest()
    lock_root = Path(tempfile.gettempdir()) / "multi-harness-bridge-skill-locks" / lock_key
    with config.registry_writer(lock_root):
        manifest_path = target/MANIFEST
        manifest = _manifest(manifest_path)
        if builtin:
            inventory = {p.name:[(p, digest(p), "multi-harness-bridge")] for p in _skills(BUILTIN_ROOT)}
            review = []
        else:
            inventory, review = _inventory(roots, harnesses, target, manifest)
        report = {"project":project, "from":list(harnesses), "to":target_host, "destination":str(target),
                  "active_workspace":str(workspace), "registered_workspaces":[str(p) for p in roots],
                  "skills":[], "conflicts":[], "review":review}
        for name, entries in sorted(inventory.items()):
            sources = sorted({str(p) for p, _, _ in entries})
            if len({fingerprint for _, fingerprint, _ in entries}) > 1:
                report["conflicts"].append({"skill":name, "status":"conflict-sources", "paths":sources})
                continue
            source, original_hash, _ = entries[0]
            desired = source
            variant = None
            reasons = [] if builtin else _blockers(source, target_host)
            if reasons:
                # Three harnesses may read the same .agents folder. A reviewed
                # replacement there must explicitly support all three hosts.
                variant = "claude" if target_host == "claude" else "shared-agents"
                folder = roots[0]/".bridge-skills"/"variants"/variant/name
                stamp = folder/"source.sha256"
                replacement = folder/"skill"
                if (not folder.resolve().is_relative_to(roots[0]) or not (replacement/"SKILL.md").is_file()
                        or not replacement.resolve().is_relative_to(roots[0]) or not stamp.is_file()
                        or stamp.read_text(encoding="ascii").strip() != original_hash):
                    review.append({"skill":name, "status":"blocked-review", "reasons":reasons,
                                   "source_hash":original_hash, "variant":variant})
                    continue
                desired = replacement
            dest = target/name
            recovered = dest.with_name(".mhb-previous-" + dest.name)
            if recovered.exists() and not dest.exists():
                os.replace(recovered, dest)
            old = manifest.get(name, {})
            backup = None
            desired_hash = digest(desired)
            if dest.is_symlink():
                status = "already-current" if dest.resolve() == desired.resolve() else "conflict-symlink"
            elif dest.exists():
                try:
                    current = digest(dest)
                except ValueError:
                    current = None
                if current == desired_hash:
                    status = "already-current"
                elif old and current and current == old.get("installed_hash"):
                    backup = _replace(desired, dest)
                    status = "updated"
                else:
                    status = "conflict-local-edits" if old else "conflict-existing"
            else:
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                backup = _replace(desired, dest)
                status = "installed"
            item = {"skill":name,"status":status}
            report["skills"].append(item)
            if status.startswith("conflict"):
                report["conflicts"].append(item)
                continue
            # Native source skills already visible in place stay user-owned.
            if not builtin and dest.exists() and dest.resolve() == source.resolve():
                continue
            record = {"sources":sources, "source_hash":original_hash, "installed_hash":desired_hash,
                      "variant":variant, "source_harnesses":sorted({host for _,_,host in entries})}
            if record != old:
                manifest[name] = record
                config.atomic_json(manifest_path, manifest)
            if backup and backup.exists():
                shutil.rmtree(backup)
        report["status"] = ("complete-with-conflicts" if report["conflicts"] else "complete-with-review" if review
                            else "complete" if inventory else "no project skills found")
        return report


def install(project, source_harnesses, target_harness, *, project_roots=None, workspace=None, cfg=None):
    return _install(project, tuple(dict.fromkeys(source_harnesses)), target_harness, project_roots, False, cfg,
                    workspace=workspace)


def bootstrap(project, target_harness, *, project_roots=None, workspace=None, cfg=None):
    return _install(project, (), target_harness, project_roots, True, cfg, workspace=workspace)
