"""Project-scoped provisioning of one reviewed, repository-owned skill entry."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
import re
import shutil
import tempfile

from . import config
from . import project_entry_ports
from .propagation import (BUILTIN_ROOT, MANIFEST, _blockers, _files, _inventory, _manifest, _replace,
                          digest, resolve_workspace)

DECLARATION = Path(".mhb/project-skills.json")
_SKILL_NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}")


@contextmanager
def _writer_locks(paths):
    with ExitStack() as stack:
        for path in sorted({Path(path).resolve() for path in paths}, key=str):
            key = hashlib.sha256(os.fsencode(path)).hexdigest()
            lock_root = Path(tempfile.gettempdir()) / "multi-harness-bridge-skill-locks" / key
            stack.enter_context(config.registry_writer(lock_root))
        yield


def _read_json(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Project skill metadata must be a regular file: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read project skill metadata ({type(exc).__name__})") from None


def _project_file(root: Path, value: str, *, label: str):
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError(f"{label} must be a workspace-relative file path")
    relative = Path(value)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{label} contains an unsafe path component")
    path = root / relative
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"{label} must not traverse symlinks")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError(f"{label} is missing from the active workspace") from None
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"{label} must resolve to a file inside its registered workspace")
    return resolved


def _sha256(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _declared_index(root: Path, *, expected_path: str | None = None):
    declaration_path = root / DECLARATION
    declaration = _read_json(declaration_path)
    if not isinstance(declaration, dict) or declaration.get("version") != 1:
        raise ValueError("Unsupported project skill declaration; expected version 1")
    entry, index = declaration.get("entry"), declaration.get("index")
    if not isinstance(entry, dict) or not isinstance(index, dict):
        raise ValueError("Project skill declaration requires entry and index objects")
    name, entry_path, entry_hash = entry.get("name"), entry.get("path"), entry.get("sha256")
    index_path, index_hash = index.get("path"), index.get("sha256")
    if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name):
        raise ValueError("Project skill entry name must be a lowercase identifier")
    if expected_path is not None and index_path != expected_path:
        raise ValueError("Dependency path does not match the registered workspace declaration")
    if not isinstance(entry_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", entry_hash):
        raise ValueError("Project skill entry must pin its SHA-256 revision")
    if not isinstance(index_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", index_hash):
        raise ValueError("Project skill index must pin its SHA-256 revision")
    entry_file = _project_file(root, entry_path, label="Project skill entry path")
    index_file = _project_file(root, index_path, label="Project skill index path")
    if entry_file.name != "SKILL.md":
        raise ValueError("Project skill entry path must name SKILL.md")
    if _sha256(entry_file) != entry_hash:
        raise ValueError("Project skill entry revision is stale; update the declaration")
    entry_text = entry_file.read_text(encoding="utf-8")
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|\Z)", entry_text, re.S)
    name_line = re.search(r"^name\s*:\s*(?:\"([^\"]+)\"|'([^']+)'|([a-z][a-z0-9_-]*))\s*$",
                          frontmatter.group(1), re.M) if frontmatter else None
    frontmatter_name = next((value for value in name_line.groups() if value is not None), None) if name_line else None
    if frontmatter_name != name:
        raise ValueError("Configured project skill name must match the SKILL.md frontmatter name")
    if _sha256(index_file) != index_hash:
        raise ValueError("Project skill index revision is stale; update the declaration")
    data = _read_json(index_file)
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("procedures"), list):
        raise ValueError("Project skill index must contain version 1 and a procedures list")
    procedures = []
    for item in data["procedures"]:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or not item["name"].strip() or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            raise ValueError("Each project procedure needs a name, relative path, and SHA-256")
        procedure_path = _project_file(root, item.get("path"), label="Project procedure path")
        if _sha256(procedure_path) != item["sha256"]:
            raise ValueError(f"Project procedure revision is stale: {item['name']}")
        procedures.append({"name": item["name"], "path": item["path"], "sha256": item["sha256"],
                           "workspace": root})
    dependencies = data.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise ValueError("Project skill dependencies must be a list")
    return {"name": name, "entry_path": entry_path, "entry_file": entry_file,
            "entry_hash": entry_hash, "index_hash": index_hash,
            "index_path": index_path, "index_file": index_file,
            "index": data, "procedures": procedures, "dependencies": dependencies}


def _resolve_dependencies(index_info, active_root: Path, roots: tuple[Path, ...]):
    procedures = list(index_info["procedures"])
    for dependency in index_info["dependencies"]:
        if (not isinstance(dependency, dict) or dependency.get("scope") != "other-registered-workspace"
                or not isinstance(dependency.get("path"), str)):
            raise ValueError("Each project skill dependency needs a path and supported scope")
        path = dependency["path"]
        if Path(path).is_absolute() or any(part in {"", ".", ".."} for part in Path(path).parts):
            raise ValueError("Project skill dependency path is unsafe")
        matches = []
        invalid = []
        for root in roots:
            if root == active_root:
                continue
            candidate = root / path
            if candidate.exists():
                try:
                    info = _declared_index(root, expected_path=path)
                except (OSError, ValueError):
                    invalid.append(root)
                    continue
                matches.append(info)
        if not matches:
            if invalid:
                return None, {"status": "stale-dependency", "path": path,
                              "detail": "A matching workspace has a stale or invalid project skill declaration."}
            return None, {"status": "missing-dependency", "path": path,
                           "detail": "Register the workspace containing the declared project skill index."}
        if len(matches) > 1:
            return None, {"status": "ambiguous-dependency", "path": path,
                           "detail": "More than one registered workspace provides this index."}
        procedures.extend(matches[0]["procedures"])
    return procedures, None


def _candidate_report(workspace_roots, destination, target_harness, sources, manifest, declared, entry_name):
    inventory, review = _inventory(workspace_roots, sources, destination, manifest, include_managed=True)
    conflicts = []
    candidates = []
    by_name = {}
    for item in declared:
        by_name.setdefault(item["name"], []).append(item)
    for name, entries in sorted(inventory.items()):
        if name == entry_name:
            continue
        paths = sorted({str(path) for path, _, _ in entries})
        hashes = {fingerprint for _, fingerprint, _ in entries}
        if len(hashes) > 1:
            conflicts.append({"skill": name, "status": "conflict-sources", "paths": paths,
                              "source_harnesses": sorted({host for _, _, host in entries})})
            continue
        source, fingerprint, _ = entries[0]
        packaged = BUILTIN_ROOT / name
        if packaged.is_dir():
            try:
                if digest(source) == digest(packaged):
                    continue
            except ValueError:
                pass
        source_file = source / "SKILL.md"
        matching_roots = [root for root in workspace_roots if source_file.is_relative_to(root)]
        source_workspace = max(matching_roots, key=lambda root: len(root.parts)) if matching_roots else None
        relative = source_file.relative_to(source_workspace).as_posix() if source_workspace else None
        portability = _blockers(source, target_harness)
        if name in by_name:
            found = any(item["sha256"] == _sha256(source_file) for item in by_name[name])
            status = "already-indexed" if found else "conflict-index-content"
            if not found:
                conflicts.append({"skill": name, "status": status, "paths": paths,
                                  "source_harnesses": sorted({host for _, _, host in entries})})
        else:
            status = "needs-project-review"
        files = tuple(_files(source))
        companions = [file.relative_to(source).as_posix() for file in files
                      if file.relative_to(source).as_posix() != "SKILL.md"]
        if status == "already-indexed" and companions:
            status = "needs-project-review"
        item = {"skill": name, "status": status, "paths": paths, "source_hash": fingerprint,
                "workspace_path": relative, "workspace": str(source_workspace) if source_workspace else None,
                "source_harnesses": sorted({host for _, _, host in entries})}
        if companions:
            item["unverified_companion_files"] = companions
        if portability:
            item["portability_review"] = portability
            if status == "already-indexed":
                item["status"] = "needs-project-review"
        candidates.append(item)
    return candidates, conflicts, review


def install(project, source_harnesses, target_harness, *, workspace=None, cfg=None):
    """Provision the configured entry and report source-only skills for review."""
    cfg = cfg or config.load()
    workspace_root, roots = resolve_workspace(project, cfg, workspace)
    sources = tuple(dict.fromkeys(source_harnesses))
    if not sources or set(sources) - set(project_entry_ports.HARNESS_DIRS):
        raise ValueError("Unknown or empty harness selection")
    picker = project_entry_ports.native_skill_picker(target_harness)
    target = picker.destination(workspace_root, "pending").parent.resolve()
    if not target.is_relative_to(workspace_root):
        raise ValueError("Skill destination escapes the active workspace")
    with _writer_locks([target]):
        return _install_locked(project, sources, target_harness, cfg, workspace_root, roots, picker, target)


def _install_locked(project, sources, target_harness, cfg, workspace_root, roots, picker, target):
    declaration = _read_json(workspace_root / DECLARATION)
    if not isinstance(declaration, dict) or declaration.get("version") != 1:
        raise ValueError("Unsupported project skill declaration; expected version 1")
    index_info = _declared_index(workspace_root)
    declared, dependency_error = _resolve_dependencies(index_info, workspace_root, roots)
    if dependency_error:
        return {"project": project, "from": list(sources), "to": target_harness,
                "active_workspace": str(workspace_root), "destination": str(target / index_info["name"]),
                "skills": [], "procedures": [], "candidates": [], "conflicts": [],
                "review": [dependency_error], "status": dependency_error["status"]}

    candidates, conflicts, review = _candidate_report(roots, target, target_harness, sources,
                                                       _manifest(target / MANIFEST), declared, index_info["name"])
    report = {"project": project, "from": list(sources), "to": target_harness,
              "active_workspace": str(workspace_root), "destination": str(target / index_info["name"]),
              "registered_workspaces": [str(root) for root in roots], "skills": [],
              "procedures": [{"name": item["name"], "path": item["path"],
                              "workspace": str(item["workspace"]), "sha256": item["sha256"]}
                             for item in declared],
              "candidates": candidates, "conflicts": conflicts, "review": review}
    portability = _blockers(index_info["entry_file"], target_harness)
    if portability:
        report["review"].append({"skill": index_info["name"], "status": "entry-needs-reviewed-variant",
                                 "reasons": portability})
        report["status"] = "complete-with-review"
        return report

    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest = picker.destination(workspace_root, index_info["name"])
    manifest_path = target / MANIFEST
    manifest = _manifest(manifest_path)
    old = manifest.get(index_info["name"], {})
    with tempfile.TemporaryDirectory(prefix=".mhb-entry-") as stage_dir:
        recovered = dest.with_name(".mhb-previous-" + dest.name)
        if recovered.exists() and not dest.exists():
            os.replace(recovered, dest)
        desired = Path(stage_dir) / index_info["name"]
        desired.mkdir()
        shutil.copy2(index_info["entry_file"], desired / "SKILL.md")
        shutil.copy2(index_info["index_file"], desired / "index.json")
        if (_sha256(index_info["entry_file"]) != index_info["entry_hash"]
                or _sha256(index_info["index_file"]) != index_info["index_hash"]
                or _sha256(desired / "SKILL.md") != index_info["entry_hash"]
                or _sha256(desired / "index.json") != index_info["index_hash"]):
            raise ValueError("Project skill entry changed during provisioning; retry after updating its declaration")
        desired_hash = digest(desired)
        source_hash = hashlib.sha256((index_info["entry_path"] + "\0" + index_info["index_path"]
                                      + "\0" + index_info["entry_hash"]
                                      + index_info["index_hash"]).encode()).hexdigest()
        backup = None
        source_directory = index_info["entry_file"].parent
        native_source = False
        if dest.is_symlink():
            native_source = dest.resolve() == source_directory
            status = "already-current" if native_source else "conflict-symlink"
        elif dest.resolve() == source_directory:
            native_source = True
            status = "already-current"
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
            backup = _replace(desired, dest)
            status = "installed"
        item = {"skill": index_info["name"], "status": status}
        report["skills"].append(item)
        if status.startswith("conflict"):
            report["conflicts"].append(item)
        elif not native_source:
            record = {"sources": [index_info["entry_path"], index_info["index_path"]],
                      "source_hash": source_hash, "installed_hash": desired_hash,
                      "variant": target_harness, "source_harnesses": list(sources)}
            if record != old:
                manifest[index_info["name"]] = record
                config.atomic_json(manifest_path, manifest)
            if backup and backup.exists():
                shutil.rmtree(backup)
    report["status"] = ("complete-with-conflicts" if report["conflicts"] else
                        "complete-with-review" if report["review"] or any(item["status"] == "needs-project-review"
                                                                           for item in candidates) else "complete")
    return report


def refresh(project, *, workspace=None, cfg=None):
    """Refresh pinned hashes after an explicit edit to the project-owned entry or index."""
    cfg = cfg or config.load()
    workspace_root, _ = resolve_workspace(project, cfg, workspace)
    targets = {workspace_root / relative for relative in project_entry_ports.HARNESS_DIRS.values()}
    with _writer_locks(targets):
        declaration_path = workspace_root / DECLARATION
        declaration = _read_json(declaration_path)
        if not isinstance(declaration, dict) or declaration.get("version") != 1:
            raise ValueError("Unsupported project skill declaration; expected version 1")
        entry, index = declaration.get("entry"), declaration.get("index")
        if not isinstance(entry, dict) or not isinstance(index, dict):
            raise ValueError("Project skill declaration requires entry and index objects")
        entry_file = _project_file(workspace_root, entry.get("path"), label="Project skill entry path")
        index_file = _project_file(workspace_root, index.get("path"), label="Project skill index path")
        index_data = _read_json(index_file)
        if not isinstance(index_data, dict) or index_data.get("version") != 1 or not isinstance(index_data.get("procedures"), list):
            raise ValueError("Project skill index must contain version 1 and a procedures list")
        for procedure in index_data["procedures"]:
            if not isinstance(procedure, dict) or not isinstance(procedure.get("name"), str):
                raise ValueError("Each project procedure needs a name, path, and SHA-256")
            path = _project_file(workspace_root, procedure.get("path"), label="Project procedure path")
            procedure["sha256"] = _sha256(path)
        config.atomic_json(index_file, index_data)
        entry["sha256"] = _sha256(entry_file)
        index["sha256"] = _sha256(index_file)
        config.atomic_json(declaration_path, declaration)
    return {"project": project, "workspace": str(workspace_root),
            "entry": entry["path"], "index": index["path"], "status": "hashes-refreshed"}
