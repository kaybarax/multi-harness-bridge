"""Read authoritative role and pickup state from a co-located legacy bridge."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import uuid


ACTORS = {"devin": "devin_local", "antigravity": "antigravity_desktop"}
HARNESSES = frozenset({"codex", "claude", *ACTORS})


@contextmanager
def _read_db(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db, db:
        db.execute("BEGIN")
        yield db


def _marker(path):
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def _uuid(value):
    return None if value is None else str(uuid.UUID(value))


def _devin(value):
    return None if value is None else "acp/devin-cli/" + value.removeprefix("acp/devin-cli/")


def _enabled(db, table):
    row = db.execute(f"SELECT enabled FROM {table} WHERE slot=1").fetchone()
    return row is not None and row[0] == 1


@dataclass(frozen=True)
class LegacyBindings:
    project: str
    installation: Path

    @property
    def state(self):
        return self.installation / "state"

    @property
    def mailbox(self):
        return self.state / "mailbox.sqlite3"

    @property
    def watcher(self):
        return self.state / "watcher"

    def handles(self, project, harness):
        return project == self.project and harness in HARNESSES

    @contextmanager
    def guard(self, project, *, exclusive=False):
        """Serialize with legacy native role handoffs without changing their state."""
        if project != self.project:
            yield
            return
        import fcntl

        with (self.state / "project-bindings.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            if (self.state / "role-handoff.pending.json").exists():
                raise RuntimeError("The legacy role handoff needs recovery before free-text pickup")
            yield

    def _watcher_roles(self, harness):
        coordinator = _marker(self.watcher / f"{harness}.thread")
        inbox = _marker(self.watcher / f"{harness}.receiver")
        if harness == "claude" and coordinator is None:
            health = json.loads((self.installation / "receiver-health.json").read_text(encoding="utf-8"))
            coordinator = health.get("coordinator_session")
        if harness == "claude" and coordinator is not None:
            coordinator = coordinator.removeprefix("local_")
        return _uuid(coordinator), _uuid(inbox)

    def _native_pair(self, harness):
        if harness == "devin":
            with _read_db(self.state / "devin-local-relay.sqlite3") as db:
                rows = (db.execute(f"SELECT session FROM {table} WHERE slot=1").fetchone()
                        for table in ("coordinator_binding", "inbox_binding"))
                return tuple(_devin(row[0]) if row else None for row in rows)
        with _read_db(self.state / "antigravity-desktop.sqlite3") as db:
            roles = dict(db.execute("SELECT role,conversation FROM desktop_roles"))
        return _uuid(roles.get("coordinator")), _uuid(roles.get("inbox"))

    def roles(self, project, harness):
        if not self.handles(project, harness):
            raise ValueError("Unknown legacy project or harness")
        if harness in ACTORS:
            with _read_db(self.mailbox) as db:
                row = db.execute(
                    "SELECT coordinator,receiver FROM harness_bindings WHERE actor=?",
                    (ACTORS[harness],),
                ).fetchone()
            normalize = _devin if harness == "devin" else _uuid
            roles = tuple(map(normalize, row)) if row else (None, None)
            if self._native_pair(harness) != roles:
                raise RuntimeError("The legacy mailbox and native role binding disagree")
            coordinator, inbox = roles
        else:
            coordinator, inbox = self._watcher_roles(harness)
        if coordinator is not None and coordinator == inbox:
            raise RuntimeError("The legacy coordinator and inbox must be distinct")
        return {"coordinator": coordinator, "inbox": inbox}

    def enabled(self, project, harness):
        if not self.handles(project, harness):
            raise ValueError("Unknown legacy project or harness")
        if harness in {"codex", "claude"}:
            return ((self.watcher / "enabled").is_file()
                    and not any((self.watcher / name).exists() for name in ("disabled", "stopped-all"))
                    and not (harness == "codex" and (self.watcher / "claude-only").exists()))
        with _read_db(self.mailbox) as db:
            if not _enabled(db, "project_delivery_control"):
                return False
        filename, table = {
            "devin": ("devin-local-relay.sqlite3", "transport_control"),
            "antigravity": ("antigravity-desktop.sqlite3", "desktop_pickup_control"),
        }[harness]
        with _read_db(self.state / filename) as db:
            return _enabled(db, table)


def load(cfg):
    path = cfg.root / "legacy.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (not isinstance(record, dict) or set(record) != {"version", "project", "installation"}
            or record["version"] != 1 or not isinstance(record["installation"], str)
            or not isinstance(record["project"], str)
            or not Path(record["installation"]).is_absolute()
            or set(cfg.projects) != {record["project"]}):
        raise ValueError("A legacy binding registry must identify one project and installation")
    installation = Path(record["installation"]).resolve(strict=True)
    bindings = LegacyBindings(record["project"], installation)
    if cfg.database.resolve() == bindings.mailbox.resolve() or not bindings.mailbox.is_file():
        raise ValueError("The free-text and legacy mailboxes require distinct files")
    return bindings
