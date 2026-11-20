"""Portable SQLite queue and role bindings for multiple projects."""

from contextlib import closing
import secrets
import sqlite3
import time
import uuid

ROLES = {"coordinator", "inbox"}
LEASE_SECONDS = 30
SCHEMA_VERSION = 2


class Store:
    """Durable project queue. One database may safely contain many projects."""

    def __init__(self, path):
        self.path = str(path)
        with closing(self._connect()) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema {version} is newer than supported {SCHEMA_VERSION}"
                )
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS bindings (
                    project TEXT NOT NULL, harness TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('coordinator','inbox')),
                    session TEXT NOT NULL,
                    PRIMARY KEY(project,harness,role),
                    UNIQUE(project,harness,session));
                CREATE TABLE IF NOT EXISTS control (
                    project TEXT NOT NULL, harness TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    explicit INTEGER NOT NULL DEFAULT 0 CHECK(explicit IN (0,1)),
                    PRIMARY KEY(project,harness));
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL,
                    sender TEXT NOT NULL, recipient TEXT NOT NULL,
                    body TEXT NOT NULL, key TEXT NOT NULL, reply_to TEXT,
                    stage TEXT NOT NULL CHECK(stage IN ('inbox','coordinator','done')),
                    lease TEXT, lease_role TEXT, lease_session TEXT,
                    deadline REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(project,sender,key));
                CREATE INDEX IF NOT EXISTS message_ready
                    ON messages(project,recipient,stage,created);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(control)")}
            if "explicit" not in columns:
                db.execute(
                    "ALTER TABLE control ADD COLUMN explicit INTEGER NOT NULL DEFAULT 0"
                )
            message_columns = {
                row[1] for row in db.execute("PRAGMA table_info(messages)")
            }
            if "updated" not in message_columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN updated REAL NOT NULL DEFAULT 0"
                )
                db.execute("UPDATE messages SET updated=created WHERE updated=0")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS binding_session_scope ON bindings(harness,session)"
            )
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level="DEFERRED")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _name(value, field):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return value

    def bind(self, project, harness, role, session):
        project, harness, session = (
            self._name(project, "project"),
            self._name(harness, "harness"),
            self._name(session, "session"),
        )
        if role not in ROLES:
            raise ValueError("role must be coordinator or inbox")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO control(project,harness,enabled,explicit) VALUES(?,?,0,0) ON CONFLICT(project,harness) DO NOTHING",
                (project, harness),
            )
            old = db.execute(
                "SELECT session FROM bindings WHERE project=? AND harness=? AND role=?",
                (project, harness, role),
            ).fetchone()
            if old and old[0] == session:
                return self.roles(project, harness)
            if old:
                active = db.execute(
                    """SELECT 1 FROM messages WHERE project=? AND recipient=?
                    AND lease_role=? AND lease_session=? AND lease IS NOT NULL AND deadline>? LIMIT 1""",
                    (project, harness, role, old[0], time.time()),
                ).fetchone()
                if active:
                    raise ValueError("cannot replace a role with an active lease")
            try:
                db.execute(
                    """INSERT INTO bindings VALUES(?,?,?,?)
                ON CONFLICT(project,harness,role) DO UPDATE SET session=excluded.session""",
                    (project, harness, role, session),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "session is already bound to this harness in another project or role"
                ) from exc
            complete = (
                db.execute(
                    "SELECT COUNT(*) FROM bindings WHERE project=? AND harness=?",
                    (project, harness),
                ).fetchone()[0]
                == 2
            )
            db.execute(
                "UPDATE control SET enabled=CASE WHEN explicit=0 AND ?=1 THEN 1 ELSE enabled END WHERE project=? AND harness=?",
                (int(complete), project, harness),
            )
        return self.roles(project, harness)

    def roles(self, project, harness):
        with closing(self._connect()) as db:
            result = {role: None for role in ROLES}
            for row in db.execute(
                "SELECT role,session FROM bindings WHERE project=? AND harness=?",
                (project, harness),
            ):
                result[row["role"]] = row["session"]
            return result

    def set_enabled(self, project, harness, enabled, *, session=None):
        project, harness = self._name(project, "project"), self._name(
            harness, "harness"
        )
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if session is not None:
                binding = db.execute(
                    "SELECT session FROM bindings WHERE project=? AND harness=? AND role='coordinator'",
                    (project, harness),
                ).fetchone()
                if not binding or binding[0] != session:
                    raise ValueError("session is not the current coordinator")
            db.execute(
                "INSERT INTO control(project,harness,enabled,explicit) VALUES(?,?,?,1) ON CONFLICT(project,harness) DO UPDATE SET enabled=excluded.enabled,explicit=1",
                (project, harness, int(bool(enabled))),
            )
        return bool(enabled)

    def send(
        self, project, sender, recipient, body, key, reply_to=None, *, session=None
    ):
        for value, label in (
            (project, "project"),
            (sender, "sender"),
            (recipient, "recipient"),
            (key, "key"),
        ):
            self._name(value, label)
        if not isinstance(body, str):
            raise ValueError("body must be a string")
        if reply_to is not None:
            self._name(reply_to, "reply_to")
        message_id = str(uuid.uuid4())
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            enabled = db.execute(
                "SELECT enabled FROM control WHERE project=? AND harness=?",
                (project, sender),
            ).fetchone()
            if enabled is not None and not enabled[0]:
                raise ValueError("sending is disabled for this project and harness")
            if session is not None:
                binding = db.execute(
                    "SELECT session FROM bindings WHERE project=? AND harness=? AND role='coordinator'",
                    (project, sender),
                ).fetchone()
                if not binding or binding[0] != session:
                    raise ValueError(
                        "session is not the current coordinator for sender"
                    )
            if reply_to is not None:
                original = db.execute(
                    "SELECT sender,recipient FROM messages WHERE id=? AND project=?",
                    (reply_to, project),
                ).fetchone()
                if not original or (original["sender"], original["recipient"]) != (
                    recipient,
                    sender,
                ):
                    raise ValueError(
                        "reply_to must reference a message in this project with reversed sender and recipient"
                    )
            now = time.time()
            db.execute(
                """INSERT INTO messages(id,project,sender,recipient,body,key,reply_to,stage,created,updated)
                VALUES(?,?,?,?,?,?,?,'inbox',?,?) ON CONFLICT(project,sender,key) DO NOTHING""",
                (message_id, project, sender, recipient, body, key, reply_to, now, now),
            )
            row = db.execute(
                "SELECT id,recipient,body,reply_to FROM messages WHERE project=? AND sender=? AND key=?",
                (project, sender, key),
            ).fetchone()
            if (row["recipient"], row["body"], row["reply_to"]) != (
                recipient,
                body,
                reply_to,
            ):
                raise ValueError(
                    "message key was reused with a different recipient, body, or reply_to"
                )
            return {"id": row[0]}

    def claim(self, project, harness, role, session):
        self._validate_claim(project, harness, role, session)
        now, lease = time.time(), secrets.token_urlsafe(24)
        stage = "inbox" if role == "inbox" else "coordinator"
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            binding = db.execute(
                "SELECT session FROM bindings WHERE project=? AND harness=? AND role=?",
                (project, harness, role),
            ).fetchone()
            enabled = db.execute(
                "SELECT enabled FROM control WHERE project=? AND harness=?",
                (project, harness),
            ).fetchone()
            if not binding or binding[0] != session or (enabled and not enabled[0]):
                return None
            row = db.execute(
                """SELECT * FROM messages WHERE project=? AND recipient=? AND stage=?
                AND (lease IS NULL OR deadline<=?) ORDER BY created,id LIMIT 1""",
                (project, harness, stage, now),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """UPDATE messages SET lease=?,lease_role=?,lease_session=?,deadline=?,
                attempts=attempts+1,updated=? WHERE id=?""",
                (lease, role, session, now + LEASE_SECONDS, now, row["id"]),
            )
            return {
                "id": row["id"],
                "lease": lease,
                "body": row["body"],
                "sender": row["sender"],
                "recipient": row["recipient"],
                "reply_to": row["reply_to"],
                "attempt": row["attempts"] + 1,
            }

    def pending(self, project, harness, role, session):
        """Read the next eligible notification without claiming or acknowledging it."""
        self._validate_claim(project, harness, role, session)
        stage = "inbox" if role == "inbox" else "coordinator"
        now = time.time()
        with closing(self._connect()) as db:
            binding = db.execute(
                "SELECT session FROM bindings WHERE project=? AND harness=? AND role=?",
                (project, harness, role),
            ).fetchone()
            enabled = db.execute(
                "SELECT enabled FROM control WHERE project=? AND harness=?",
                (project, harness),
            ).fetchone()
            if not binding or binding[0] != session or (enabled and not enabled[0]):
                return None
            r = db.execute(
                """SELECT id,body,sender,recipient,reply_to,attempts FROM messages
                WHERE project=? AND recipient=? AND stage=? AND (lease IS NULL OR deadline<=?)
                ORDER BY created,id LIMIT 1""",
                (project, harness, stage, now),
            ).fetchone()
            if r is None:
                return None
            return {
                "id": r["id"],
                "body": r["body"],
                "sender": r["sender"],
                "recipient": r["recipient"],
                "reply_to": r["reply_to"],
                "attempt": r["attempts"] + 1,
            }

    def finish(self, project, harness, role, session, id, lease):
        self._validate_claim(project, harness, role, session)
        stage = "inbox" if role == "inbox" else "coordinator"
        next_stage = "coordinator" if role == "inbox" else "done"
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = db.execute(
                """SELECT 1 FROM messages m WHERE m.id=? AND m.project=?
                AND m.recipient=? AND m.stage=? AND m.lease=? AND m.lease_role=?
                AND m.lease_session=? AND m.deadline>? AND EXISTS (
                    SELECT 1 FROM bindings b WHERE b.project=m.project AND b.harness=m.recipient
                    AND b.role=? AND b.session=m.lease_session)""",
                (id, project, harness, stage, lease, role, session, now, role),
            ).fetchone()
            if row is None:
                return False
            # A stop blocks new work; an already-issued lease remains finishable.
            db.execute(
                """UPDATE messages SET stage=?,lease=NULL,lease_role=NULL,lease_session=NULL,
                deadline=NULL,updated=? WHERE id=?""",
                (next_stage, now, id),
            )
            return True

    def status(self, project, harness):
        with closing(self._connect()) as db:
            ctrl = db.execute(
                "SELECT enabled FROM control WHERE project=? AND harness=?",
                (project, harness),
            ).fetchone()
            counts = {"inbox": 0, "coordinator": 0, "done": 0, "leased": 0}
            for row in db.execute(
                "SELECT stage,COUNT(*) n FROM messages WHERE project=? AND recipient=? GROUP BY stage",
                (project, harness),
            ):
                counts[row["stage"]] = row["n"]
            counts["leased"] = db.execute(
                "SELECT COUNT(*) FROM messages WHERE project=? AND recipient=? AND lease IS NOT NULL AND deadline>?",
                (project, harness, time.time()),
            ).fetchone()[0]
            return {"enabled": bool(ctrl[0]) if ctrl else False, **counts}

    def pending_overview(self, project, harness=None, limit=100):
        """Return bounded, read-only metadata for unfinished messages."""
        self._name(project, "project")
        if harness is not None:
            self._name(harness, "harness")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit must be between 1 and 1000")
        with closing(self._connect()) as db:
            if harness is None:
                rows = db.execute(
                    """SELECT id,project,sender,recipient,stage,created,updated,attempts
                    FROM messages WHERE project=? AND stage<>'done' ORDER BY created,id LIMIT ?""",
                    (project, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT id,project,sender,recipient,stage,created,updated,attempts
                    FROM messages WHERE project=? AND recipient=? AND stage<>'done'
                    ORDER BY created,id LIMIT ?""",
                    (project, harness, limit),
                ).fetchall()
            return [dict(row) for row in rows]

    def trace(self, project, message_id):
        """Return one message's lifecycle metadata, scoped to its project."""
        self._name(project, "project")
        self._name(message_id, "message_id")
        with closing(self._connect()) as db:
            row = db.execute(
                """SELECT id,project,sender,recipient,stage,created,updated,
                attempts,lease_role,deadline FROM messages WHERE project=? AND id=?""",
                (project, message_id),
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _validate_claim(project, harness, role, session):
        for value, label in (
            (project, "project"),
            (harness, "harness"),
            (session, "session"),
        ):
            Store._name(value, label)
        if role not in ROLES:
            raise ValueError("role must be coordinator or inbox")
