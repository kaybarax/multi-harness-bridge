"""Per-user project registry. Only an explicit operator command changes it."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("MHB_HOME") or Path.home() / ".multi-harness-bridge").expanduser().resolve()


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", value):
        raise ValueError("Use a lowercase identifier beginning with a letter (up to 63 characters)")
    return value


def _prepare(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)


@contextmanager
def registry_writer(root):
    """Serialize short config and credential writes across processes on all supported OSes."""
    _prepare(root)
    with closing(sqlite3.connect(root / "settings-lock.sqlite3", timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        yield


def atomic_json(path: Path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".mhb-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@dataclass(frozen=True)
class Config:
    root: Path
    projects: dict[str, tuple[Path, ...]]

    @property
    def database(self) -> Path:
        return self.root / "bridge.sqlite3"

    def resolve(self, cwd, hint=None) -> str:
        folders = cwd if isinstance(cwd, (list, tuple)) else [cwd]
        matches = set()
        for folder in folders:
            path = Path(folder).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError("A workspace directory is required")
            candidates = [(len(root.parts), project) for project, roots in self.projects.items()
                          for root in roots if path.is_relative_to(root)]
            if candidates:
                depth = max(size for size, _ in candidates)
                matches.update(name for size, name in candidates if size == depth)
        if hint is not None and hint in matches:
            return hint
        if hint is None and len(matches) == 1:
            return matches.pop()
        if matches:
            raise ValueError("Choose a matching registered project: " + ", ".join(sorted(matches)))
        raise ValueError("No registered project matches the workspace")

    def require(self, project):
        if project not in self.projects:
            raise ValueError("Unknown project. Register its workspace with mhb project add.")
        return project


def load(root=None) -> Config:
    root = Path(root).expanduser().resolve() if root is not None else home()
    path = root / "projects.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config(root, {})
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("projects"), dict):
        raise ValueError("Unsupported project registry")
    projects = {}
    for name, paths in data["projects"].items():
        identifier(name)
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not Path(p).is_absolute() for p in paths):
            raise ValueError(f"Invalid workspace roots for {name}")
        projects[name] = tuple(Path(p).resolve() for p in paths)
    return Config(root, projects)


def register(name, paths, root=None):
    identifier(name)
    roots = tuple(dict.fromkeys(Path(p).expanduser().resolve(strict=True) for p in paths))
    if not roots or any(not p.is_dir() for p in roots):
        raise ValueError("Provide at least one existing workspace directory")
    directory = home() if root is None else Path(root).expanduser().resolve()
    with registry_writer(directory):
        config = load(directory)
        projects = dict(config.projects)
        if name in projects and set(projects[name]) != set(roots):
            raise ValueError("This project already has different roots. Update projects.json explicitly.")
        projects[name] = roots
        atomic_json(directory / "projects.json", {"version": 1, "projects": {
            k: [str(v) for v in values] for k, values in sorted(projects.items())}})
    return load(directory)


def open_store(cfg):
    """Open the configured queue and use live legacy bindings when declared."""
    from . import legacy
    from .store import Store

    provider = legacy.load(cfg)
    return Store(cfg.database, **({"bindings_provider": provider} if provider else {}))
