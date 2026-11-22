"""Per-user project registry. Only an explicit operator command changes it."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import tomllib
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


# Optional per-project harness switches live in <MHB_HOME>/options.json next to
# projects.json. A missing or malformed file means every option stays off.
#   {"version": 1, "projects": {"<slug>": {"codex": {"coordinator_wake": "agent-relay"}}}}
_OPTION_KEY = re.compile(r"[a-z][a-z0-9_]{0,62}")
_OPTION_VALUE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def load_options(root=None) -> dict:
    """Return the projects map from options.json; missing or malformed means no options."""
    root = Path(root).expanduser().resolve() if root is not None else home()
    try:
        data = json.loads((root / "options.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("projects"), dict):
        return {}
    return data["projects"]


def project_option(root, project, harness, key):
    """Return one nested option value, or None when unset or malformed."""
    project_opts = load_options(root).get(project)
    if not isinstance(project_opts, dict):
        return None
    harness_opts = project_opts.get(harness)
    if not isinstance(harness_opts, dict):
        return None
    return harness_opts.get(key)


def set_project_option(root, project, harness, key, value):
    """Merge one option into options.json under the registry writer; None removes it."""
    identifier(project)
    identifier(harness)
    if not isinstance(key, str) or not _OPTION_KEY.fullmatch(key):
        raise ValueError("Use a lowercase option key beginning with a letter (up to 63 characters)")
    if value is not None and (not isinstance(value, str) or not _OPTION_VALUE.fullmatch(value)):
        raise ValueError("Option values are short lowercase tokens such as agent-relay")
    directory = Path(root).expanduser().resolve() if root is not None else home()
    with registry_writer(directory):
        projects = dict(load_options(directory))
        raw_project = projects.get(project)
        project_opts = dict(raw_project) if isinstance(raw_project, dict) else {}
        raw_harness = project_opts.get(harness)
        harness_opts = dict(raw_harness) if isinstance(raw_harness, dict) else {}
        if value is None:
            harness_opts.pop(key, None)
        else:
            harness_opts[key] = value
        if harness_opts:
            project_opts[harness] = harness_opts
        else:
            project_opts.pop(harness, None)
        if project_opts:
            projects[project] = project_opts
        else:
            projects.pop(project, None)
        atomic_json(directory / "options.json", {"version": 1, "projects": {
            name: opts for name, opts in sorted(projects.items())}})
    return load_options(directory)


# Codex applies approval mode per thread at creation time, so a project that
# relays coordinator wakes through its inbox agent needs these keys visible in
# each registered workspace before that inbox thread is created.
CODEX_APPROVAL_KEYS = {"approval_policy": "on-request", "approvals_reviewer": "auto_review"}
_TOML_SCALAR = r'(?:"(?:[^"\\]|\\.)*"|\'[^\']*\'|[^\s#,]+)'


def provision_codex_workspace(workspace) -> dict:
    """Merge required Codex approval keys into <workspace>/.codex/config.toml.

    Existing keys, comments, and unrelated content are preserved; missing
    parent directories are created. The result is verified as TOML before it
    replaces the file, and an already-current file is left untouched.
    """
    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Codex workspace must be an existing directory")
    target = root / ".codex" / "config.toml"
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        raise ValueError(f"Cannot read Codex config ({type(exc).__name__})") from None
    lines = text.splitlines()
    section = next((index for index, line in enumerate(lines)
                    if line.lstrip().startswith("[")), len(lines))
    changed = False
    for key, value in CODEX_APPROVAL_KEYS.items():
        pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*){_TOML_SCALAR}(.*)$")
        for index in range(section):
            match = pattern.match(lines[index])
            if match:
                updated = f'{match.group(1)}"{value}"{match.group(2)}'
                if lines[index] != updated:
                    lines[index] = updated
                    changed = True
                break
        else:
            lines.insert(section, f'{key} = "{value}"')
            section += 1
            changed = True
    if not changed:
        return {"host": "codex", "status": "already-current", "config": str(target)}
    rendered = "\n".join(lines) + "\n"
    try:
        tomllib.loads(rendered)
    except tomllib.TOMLDecodeError:
        raise ValueError("Merged Codex config is not valid TOML; fix it manually") from None
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".mhb-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return {"host": "codex", "status": "installed", "config": str(target)}
