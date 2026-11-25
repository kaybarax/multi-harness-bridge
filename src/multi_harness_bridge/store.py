"""Portable SQLite queue and role bindings for multiple projects."""

from contextlib import closing, nullcontext
import secrets
import sqlite3
import time
import uuid

ROLES = {"coordinator", "inbox"}
LEASE_SECONDS = 30
DB_TIMEOUT_SECONDS = 10
# Leave time for the adapter's bounded launch, verification, and native pickup.
WAKE_STALE_SECONDS = 135.0
WAKE_RETRY_BASE_SECONDS = 2.0
WAKE_RETRY_MAX_SECONDS = 60.0
SCHEMA_VERSION = 4
HUMAN_NOTIFICATION_ROUTES = {"telegram", "pma"}


class Store:
    """Durable project queue. One database may safely contain many projects."""

    def __init__(self, path, *, bindings_provider=None):
        self.path = str(path)
        self.bindings_provider = bindings_provider
        with closing(self._connect()) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            self._check_schema_version(version)
            message_columns = {row[1] for row in db.execute("PRAGMA table_xinfo(messages)")}
            required_columns = {"project", "stage", "lease"}
            if version == SCHEMA_VERSION:
                required_columns.update({"request_id", "sender_session", "pickup_harness"})
            if (message_columns or version == SCHEMA_VERSION) and not required_columns <= message_columns:
                raise ValueError("This is a different mailbox format. Configure a separate portable queue.")
            if version == SCHEMA_VERSION:
                if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    self._ensure_wal(db)
                self._ensure_orchestrator_schema(db)
                return
            self._ensure_wal(db)
            db.execute("BEGIN IMMEDIATE")
            # A different bridge process may have migrated while we waited.
            version = db.execute("PRAGMA user_version").fetchone()[0]
            self._check_schema_version(version)
            if version == SCHEMA_VERSION:
                return
            self._migrate(db)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _ensure_wal(db):
        # SQLite may return BUSY immediately while switching journal modes,
        # even with busy_timeout set. Concurrent bridge startups may race here.
        until = time.monotonic() + DB_TIMEOUT_SECONDS
        while True:
            try:
                mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if mode != "wal":
                    raise RuntimeError(f"The portable mailbox requires SQLite WAL mode, got {mode}")
                return
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY or time.monotonic() >= until:
                    raise
                time.sleep(0.01)

    @staticmethod
    def _check_schema_version(version):
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema {version} is newer than supported {SCHEMA_VERSION}"
            )

    @staticmethod
    def _migrate(db):
        db.execute("""CREATE TABLE IF NOT EXISTS bindings (
            project TEXT NOT NULL, harness TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('coordinator','inbox')),
            session TEXT NOT NULL,
            PRIMARY KEY(project,harness,role), UNIQUE(project,harness,session))""")
        db.execute("""CREATE TABLE IF NOT EXISTS control (
            project TEXT NOT NULL, harness TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
            explicit INTEGER NOT NULL DEFAULT 0 CHECK(explicit IN (0,1)),
            PRIMARY KEY(project,harness))""")
        if "explicit" not in {row[1] for row in db.execute("PRAGMA table_info(control)")}:
            db.execute(
                "ALTER TABLE control ADD COLUMN explicit INTEGER NOT NULL DEFAULT 0"
            )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS binding_session_scope ON bindings(harness,session)"
        )
        db.execute("""CREATE TABLE free_text_requests (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, sender TEXT NOT NULL,
            key TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(project,sender,key))""")
        old_columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        if old_columns:
            db.execute("ALTER TABLE messages RENAME TO old_messages")
        db.execute("""CREATE TABLE messages (
            id TEXT PRIMARY KEY, project TEXT NOT NULL,
            sender TEXT NOT NULL, recipient TEXT NOT NULL,
            body TEXT NOT NULL, key TEXT NOT NULL, reply_to TEXT,
            stage TEXT NOT NULL CHECK(stage IN ('outbound','inbox','coordinator','done')),
            lease TEXT, lease_role TEXT, lease_session TEXT,
            deadline REAL, attempts INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL, updated REAL NOT NULL,
            request_id TEXT, sender_session TEXT,
            pickup_harness TEXT GENERATED ALWAYS AS (
                CASE WHEN stage='outbound' THEN sender ELSE recipient END
            ) VIRTUAL)""")
        if old_columns:
            updated = "updated" if "updated" in old_columns else "created"
            db.execute(f"""INSERT INTO messages
                (id,project,sender,recipient,body,key,reply_to,stage,lease,lease_role,
                 lease_session,deadline,attempts,created,updated)
                SELECT id,project,sender,recipient,body,key,reply_to,stage,lease,
                lease_role,lease_session,deadline,attempts,created,{updated}
                FROM old_messages""")
            db.execute("DROP TABLE old_messages")
        db.execute("""CREATE UNIQUE INDEX legacy_message_key
            ON messages(project,sender,key) WHERE request_id IS NULL""")
        db.execute("""CREATE UNIQUE INDEX free_text_delivery
            ON messages(request_id,recipient) WHERE request_id IS NOT NULL AND reply_to IS NULL""")
        db.execute("""CREATE UNIQUE INDEX free_text_reply
            ON messages(reply_to) WHERE request_id IS NOT NULL AND reply_to IS NOT NULL""")
        db.execute("""CREATE UNIQUE INDEX free_text_reply_key
            ON messages(project,sender,key) WHERE request_id IS NOT NULL AND reply_to IS NOT NULL""")
        db.execute("""CREATE INDEX message_ready
            ON messages(project,pickup_harness,stage,created)""")
        db.execute("CREATE INDEX replies ON messages(project,reply_to)")
        Store._ensure_orchestrator_schema(db)
        db.execute("""CREATE TABLE IF NOT EXISTS delivery_events(
            message_id TEXT NOT NULL, at REAL NOT NULL, event TEXT NOT NULL,
            attempt INTEGER NOT NULL, PRIMARY KEY(message_id,event,attempt))""")
        db.execute("""CREATE TRIGGER record_enqueue AFTER INSERT ON messages
            BEGIN INSERT INTO delivery_events VALUES(NEW.id,NEW.created,'enqueued',0); END""")
        db.execute("""CREATE TRIGGER record_delivery AFTER UPDATE ON messages
            WHEN OLD.stage<>NEW.stage OR OLD.attempts<>NEW.attempts
            BEGIN INSERT INTO delivery_events VALUES(NEW.id,NEW.updated,
                CASE WHEN OLD.attempts<>NEW.attempts THEN NEW.stage || '-claimed'
                     WHEN NEW.stage='done' THEN 'acknowledged'
                     WHEN OLD.stage='outbound' THEN 'routed'
                     ELSE 'forwarded' END,
                NEW.attempts); END""")

    @staticmethod
    def _ensure_orchestrator_schema(db):
        """Install additive project-owner state without changing queue row formats."""
        db.execute("""CREATE TABLE IF NOT EXISTS project_orchestrators (
            project TEXT PRIMARY KEY, harness TEXT NOT NULL, session TEXT NOT NULL,
            revision INTEGER NOT NULL, changed REAL NOT NULL,
            valid INTEGER NOT NULL DEFAULT 1 CHECK(valid IN (0,1)))""")
        db.execute("""CREATE TABLE IF NOT EXISTS orchestrator_events (
            project TEXT NOT NULL, revision INTEGER NOT NULL, event TEXT NOT NULL,
            harness TEXT, session TEXT, previous_harness TEXT, previous_session TEXT,
            at REAL NOT NULL, PRIMARY KEY(project,revision))""")
        db.execute("""CREATE TABLE IF NOT EXISTS role_take_requests (
            project TEXT NOT NULL, request_id TEXT NOT NULL, harness TEXT NOT NULL,
            session TEXT NOT NULL, designate INTEGER NOT NULL, PRIMARY KEY(project,request_id))""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS stale_orchestrator_on_coordinator_rebind
            AFTER UPDATE OF session ON bindings
            WHEN OLD.role='coordinator' AND OLD.session<>NEW.session AND EXISTS(
                SELECT 1 FROM project_orchestrators WHERE project=OLD.project
                AND harness=OLD.harness AND session=OLD.session AND valid=1)
            BEGIN
                UPDATE project_orchestrators SET valid=0,
                    revision=(SELECT COALESCE(MAX(revision),0)+1 FROM orchestrator_events WHERE project=OLD.project),
                    changed=CAST(strftime('%s','now') AS REAL)
                WHERE project=OLD.project AND harness=OLD.harness AND session=OLD.session;
                INSERT INTO orchestrator_events(project,revision,event,previous_harness,previous_session,at)
                    SELECT OLD.project,COALESCE(MAX(revision),0)+1,'stale',OLD.harness,OLD.session,
                    CAST(strftime('%s','now') AS REAL) FROM orchestrator_events WHERE project=OLD.project;
            END""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=DB_TIMEOUT_SECONDS, isolation_level="DEFERRED")
        db.execute("PRAGMA synchronous=FULL")
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _name(value, field):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return value

    def _managed_route(self, project, harness):
        provider = self.bindings_provider
        return provider is not None and provider.handles(project, harness)

    def _route_guard(self, project):
        provider = self.bindings_provider
        return provider.guard(project) if provider is not None else nullcontext()

    def bind(self, project, harness, role, session, *, take=False, designate=False,
             request_id=None):
        project, harness, session = (
            self._name(project, "project"),
            self._name(harness, "harness"),
            self._name(session, "session"),
        )
        if role not in ROLES:
            raise ValueError("role must be coordinator or inbox")
        if request_id is not None:
            request_id = self._name(request_id, "request_id")
            if role != "coordinator":
                raise ValueError("request_id is supported only for coordinator takeovers")
        if self._managed_route(project, harness):
            raise ValueError("use the native role picker for this managed route")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if request_id is not None:
                previous_request = db.execute(
                    "SELECT harness,session,designate FROM role_take_requests WHERE project=? AND request_id=?",
                    (project, request_id)).fetchone()
                if previous_request:
                    if tuple(previous_request) != (harness, session, int(bool(designate))):
                        raise ValueError("coordinator invocation key was reused for a different request")
                    return self._roles(db, project, harness)
            db.execute(
                "INSERT INTO control(project,harness,enabled,explicit) VALUES(?,?,0,0) ON CONFLICT(project,harness) DO NOTHING",
                (project, harness),
            )
            old = db.execute(
                "SELECT session FROM bindings WHERE project=? AND harness=? AND role=?",
                (project, harness, role),
            ).fetchone()
            owner_before = self._owner_row(db, project) if role == "coordinator" else None
            owner_was_active = bool(owner_before and owner_before["harness"] == harness
                                    and old and owner_before["session"] == old[0]
                                    and owner_before["valid"])
            if designate and owner_was_active and old and old[0] != session:
                # A deliberate transfer replaces the owner directly; suppress
                # the binding trigger's stale transition for the old owner.
                db.execute("UPDATE project_orchestrators SET valid=0 WHERE project=?", (project,))
            resume = take and role == "inbox"
            if old and old[0] == session and not resume:
                if designate:
                    if role != "coordinator":
                        raise ValueError("only a coordinator can be designated as project orchestrator")
                    self._set_orchestrator(db, project, harness, session, "designated")
                if request_id is not None:
                    db.execute("INSERT INTO role_take_requests(project,request_id,harness,session,designate) VALUES(?,?,?,?,?)",
                               (project, request_id, harness, session, int(bool(designate))))
                return self._roles(db, project, harness)
            if old and old[0] != session:
                active = db.execute(
                    """SELECT 1 FROM messages WHERE project=? AND pickup_harness=?
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
            if resume:
                # Return this route to automatic activation when both roles exist.
                db.execute("UPDATE control SET explicit=0, enabled=0 WHERE project=? AND harness=?",
                           (project, harness))
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
            if role == "coordinator" and old is not None and old[0] != session:
                self._record_implicit_owner_change(db, project, harness, session)
                if owner_was_active and not designate:
                    owner_after = self._owner_row(db, project)
                    if owner_after:
                        self._enqueue_orchestrator_notices(db, project, harness, session,
                                                           owner_after["revision"], "stale")
            if designate:
                if role != "coordinator":
                    raise ValueError("only a coordinator can be designated as project orchestrator")
                self._set_orchestrator(db, project, harness, session, "designated")
            if request_id is not None:
                db.execute("INSERT INTO role_take_requests(project,request_id,harness,session,designate) VALUES(?,?,?,?,?)",
                           (project, request_id, harness, session, int(bool(designate))))
            return self._roles(db, project, harness)

    def take_coordinator(self, project, harness, session, *, designate=False,
                         request_id=None):
        """Apply a verified coordinator takeover and optional project election.

        Local bindings and designation share one transaction. Native providers
        own their binding handoff; for those routes this verifies the live role
        under their exclusive lock before updating only the project record.
        """
        project, harness, session = (self._name(project, "project"),
                                    self._name(harness, "harness"),
                                    self._name(session, "session"))
        if self._managed_route(project, harness):
            with self.bindings_provider.guard(project, exclusive=True):
                if self.bindings_provider.roles(project, harness).get("coordinator") != session:
                    raise ValueError("session is not the current native coordinator")
                with closing(self._connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    if request_id is not None:
                        request_id = self._name(request_id, "request_id")
                        prior = db.execute("SELECT harness,session,designate FROM role_take_requests WHERE project=? AND request_id=?",
                                           (project, request_id)).fetchone()
                        if prior:
                            if tuple(prior) != (harness, session, int(bool(designate))):
                                raise ValueError("coordinator invocation key was reused for a different request")
                            return self._take_result_db(db, project, harness, session)
                    if not designate:
                        self._record_implicit_owner_change(db, project, harness, session)
                    if designate:
                        self._set_orchestrator(db, project, harness, session, "designated")
                    if request_id is not None:
                        db.execute("INSERT INTO role_take_requests(project,request_id,harness,session,designate) VALUES(?,?,?,?,?)",
                                   (project, request_id, harness, session, int(bool(designate))))
                    return self._take_result_db(db, project, harness, session)
        self.bind(project, harness, "coordinator", session, take=True,
                  designate=designate, request_id=request_id)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            return self._take_result_db(db, project, harness, session)

    @staticmethod
    def _owner_row(db, project):
        return db.execute("SELECT harness,session,revision,valid FROM project_orchestrators WHERE project=?",
                          (project,)).fetchone()

    def _set_orchestrator(self, db, project, harness, session, event):
        previous = self._owner_row(db, project)
        if previous and (previous["harness"], previous["session"]) == (harness, session) and previous["valid"]:
            return previous["revision"]
        revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM orchestrator_events WHERE project=?",
                              (project,)).fetchone()[0]
        now = time.time()
        db.execute("""INSERT INTO project_orchestrators(project,harness,session,revision,changed,valid)
            VALUES(?,?,?,?,?,1) ON CONFLICT(project) DO UPDATE SET harness=excluded.harness,
            session=excluded.session,revision=excluded.revision,changed=excluded.changed,valid=1""",
                   (project, harness, session, revision, now))
        db.execute("""INSERT INTO orchestrator_events
            (project,revision,event,harness,session,previous_harness,previous_session,at)
            VALUES(?,?,?,?,?,?,?,?)""",
                   (project, revision, event, harness, session,
                    previous["harness"] if previous else None,
                    previous["session"] if previous else None, now))
        self._enqueue_orchestrator_notices(db, project, harness, session, revision,
                                           "designated")
        return revision

    def _record_implicit_owner_change(self, db, project, harness, new_session):
        owner = self._owner_row(db, project)
        if owner and owner["harness"] == harness and owner["session"] != new_session and owner["valid"]:
            revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM orchestrator_events WHERE project=?",
                                  (project,)).fetchone()[0]
            now = time.time()
            db.execute("UPDATE project_orchestrators SET revision=?,changed=?,valid=0 WHERE project=?",
                       (revision, now, project))
            db.execute("""INSERT INTO orchestrator_events
                (project,revision,event,harness,session,previous_harness,previous_session,at)
                VALUES(?,?,'stale',NULL,NULL,?,?,?)""",
                       (project, revision, owner["harness"], owner["session"], now))
            self._enqueue_orchestrator_notices(db, project, harness, new_session, revision, "stale")

    def _enqueue_orchestrator_notices(self, db, project, sender, session, revision, state):
        """Durably route notices through the existing harness mailbox controls."""
        try:
            self._require_free_text_sender(db, project, sender, session)
        except (ValueError, RuntimeError, sqlite3.Error):
            return
        recipients = {row[0] for row in db.execute(
            "SELECT DISTINCT harness FROM bindings WHERE project=?", (project,))}
        if self.bindings_provider is not None:
            recipients.update({"codex", "claude", "devin", "antigravity"})
        recipients.discard(sender)
        body = (f"The project board owner is {state}. This change concerns {sender}. "
                f"Check project status for designation revision {revision}.")
        for recipient in sorted(recipients):
            try:
                self._require_bound_route(db, project, recipient)
                if not self._enabled(db, project, recipient):
                    continue
            except (ValueError, RuntimeError, sqlite3.Error):
                continue
            request_id = str(uuid.uuid4())
            key = f"mhb-orchestrator-{request_id}"
            db.execute("INSERT INTO free_text_requests(id,project,sender,key,body) VALUES(?,?,?,?,?)",
                       (request_id, project, sender, key, body))
            self._enqueue_routed(db, request_id, project, sender, recipient, body, key, session)

    def _project_orchestrator(self, db, project):
        owner = self._owner_row(db, project)
        if owner is None:
            last_event = db.execute("SELECT event FROM orchestrator_events WHERE project=? ORDER BY revision DESC LIMIT 1",
                                    (project,)).fetchone()
            return {"status": "cleared" if last_event and last_event[0] == "cleared" else "never_designated",
                    "harness": None, "session": None}
        active = bool(owner["valid"]) and self._roles(db, project, owner["harness"]).get("coordinator") == owner["session"]
        return {"status": "active" if active else "stale", "harness": owner["harness"],
                "session": owner["session"], "revision": owner["revision"]}

    def _take_result_db(self, db, project, harness, session):
        return {"project": project, "harness": harness, "session": session,
                "roles": self._roles(db, project, harness),
                "orchestrator": self._project_orchestrator(db, project)}

    def project_status(self, project):
        """Return live coordinator bindings and the optional project owner."""
        self._name(project, "project")
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._observe_owner_staleness(db, project)
            harnesses = {row[0] for row in db.execute(
                "SELECT DISTINCT harness FROM bindings WHERE project=?", (project,))}
            if self.bindings_provider is not None:
                harnesses.update({"codex", "claude", "devin", "antigravity"})
            coordinators = {name: self._roles(db, project, name).get("coordinator")
                            for name in sorted(harnesses)}
            return {"project": project, "coordinators": coordinators,
                    "orchestrator": self._project_orchestrator(db, project),
                    "orchestrator_events": [dict(row) for row in db.execute(
                        "SELECT revision,event,harness,session,previous_harness,previous_session,at "
                        "FROM orchestrator_events WHERE project=? ORDER BY revision", (project,))]}

    def orchestrator_status(self, project):
        """Read only the project owner, without querying every peer binding."""
        self._name(project, "project")
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._observe_owner_staleness(db, project)
            return self._project_orchestrator(db, project)

    def _observe_owner_staleness(self, db, project):
        owner = self._owner_row(db, project)
        if owner is None:
            return
        if not owner["valid"]:
            return
        current = self._roles(db, project, owner["harness"]).get("coordinator")
        if current == owner["session"]:
            return
        revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM orchestrator_events WHERE project=?",
                              (project,)).fetchone()[0]
        now = time.time()
        db.execute("UPDATE project_orchestrators SET valid=0,revision=?,changed=? WHERE project=?",
                   (revision, now, project))
        db.execute("""INSERT INTO orchestrator_events
            (project,revision,event,previous_harness,previous_session,at)
            VALUES(?,?,'stale',?,?,?)""", (project, revision, owner["harness"], owner["session"], now))
        if current:
            self._enqueue_orchestrator_notices(db, project, owner["harness"], current, revision, "stale")

    def clear_orchestrator(self, project):
        """Clear project designation while keeping all harness bindings intact."""
        self._name(project, "project")
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._owner_row(db, project)
            if owner is None:
                return {"project": project, "cleared": False,
                        "orchestrator": self._project_orchestrator(db, project)}
            revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM orchestrator_events WHERE project=?",
                                  (project,)).fetchone()[0]
            now = time.time()
            db.execute("DELETE FROM project_orchestrators WHERE project=?", (project,))
            db.execute("""INSERT INTO orchestrator_events
                (project,revision,event,harness,session,previous_harness,previous_session,at)
                VALUES(?,?,'cleared',NULL,NULL,?,?,?)""",
                       (project, revision, owner["harness"], owner["session"], now))
            roles = self._roles(db, project, owner["harness"])
            sender_session = roles.get("coordinator")
            if sender_session:
                self._enqueue_orchestrator_notices(db, project, owner["harness"], sender_session,
                                                   revision, "cleared")
            return {"project": project, "cleared": True,
                    "orchestrator": {"status": "cleared", "harness": None, "session": None}}

    def roles(self, project, harness):
        with self._route_guard(project), closing(self._connect()) as db:
            return self._roles(db, project, harness)

    def _roles(self, db, project, harness):
        if self._managed_route(project, harness):
            return self.bindings_provider.roles(project, harness)
        result = dict.fromkeys(ROLES)
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
        if self._managed_route(project, harness):
            raise ValueError("use the native stop or resume control for this managed route")
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
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if self._managed_route(project, sender):
                if not self._enabled(db, project, sender):
                    raise ValueError("sending is disabled for this project and harness")
            else:
                enabled = db.execute(
                    "SELECT enabled FROM control WHERE project=? AND harness=?",
                    (project, sender),
                ).fetchone()
                if enabled is not None and not enabled["enabled"]:
                    raise ValueError("sending is disabled for this project and harness")
            if session is not None and self._roles(db, project, sender)["coordinator"] != session:
                raise ValueError("session is not the current coordinator for sender")
            if reply_to is not None:
                original = db.execute(
                    "SELECT sender,recipient FROM messages WHERE id=? AND project=? AND request_id IS NULL",
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
                VALUES(?,?,?,?,?,?,?,'inbox',?,?) ON CONFLICT(project,sender,key) WHERE request_id IS NULL DO NOTHING""",
                (message_id, project, sender, recipient, body, key, reply_to, now, now),
            )
            row = db.execute(
                """SELECT id,recipient,body,reply_to FROM messages
                WHERE project=? AND sender=? AND key=? AND request_id IS NULL""",
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

    def send_fanout(self, project, sender, recipients, body, key, *, session):
        """Enqueue one durable outbound delivery per registered destination."""
        self._validate_free_text(project, sender, body, key, session)
        if not isinstance(recipients, (list, tuple)) or not recipients:
            raise ValueError("recipients must be a non-empty list of harnesses")
        for recipient in recipients:
            self._validate_free_text_route(recipient)
        if sender in recipients or len(set(recipients)) != len(recipients):
            raise ValueError("recipients must be distinct and exclude the sender")
        recipients = sorted(recipients)
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._require_free_text_sender(db, project, sender, session)
            for recipient in recipients:
                self._require_bound_route(db, project, recipient)
            request = db.execute(
                "SELECT id,body FROM free_text_requests WHERE project=? AND sender=? AND key=?",
                (project, sender, key),
            ).fetchone()
            if request:
                deliveries = [dict(row) for row in db.execute(
                    """SELECT id,recipient FROM messages WHERE request_id=? AND reply_to IS NULL
                    ORDER BY recipient""",
                    (request["id"],),
                )]
                if request["body"] != body or [item["recipient"] for item in deliveries] != recipients:
                    raise ValueError("request key was reused with different recipients or body")
                return {"request_id": request["id"], "deliveries": deliveries}

            request_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO free_text_requests(id,project,sender,key,body) VALUES(?,?,?,?,?)",
                (request_id, project, sender, key, body),
            )
            deliveries = [
                dict(id=self._enqueue_routed(db, request_id, project, sender, recipient,
                                             body, key, session), recipient=recipient)
                for recipient in recipients
            ]
            return {"request_id": request_id, "deliveries": deliveries}

    def reply_free_text(self, project, sender, reply_to, body, key, *, session):
        """Send one substantive reply for a delivery back through both bridges."""
        self._validate_free_text(project, sender, body, key, session)
        self._name(reply_to, "reply_to")
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return self._reply_free_text(db, project, sender, reply_to, body, key, session)

    def _reply_free_text(self, db, project, sender, reply_to, body, key, session):
        self._require_free_text_sender(db, project, sender, session)
        original = db.execute(
            """SELECT sender,request_id,stage,lease_session,deadline FROM messages
            WHERE id=? AND project=? AND recipient=? AND request_id IS NOT NULL
            AND reply_to IS NULL""",
            (reply_to, project, sender),
        ).fetchone()
        if original is None:
            raise ValueError("reply_to must reference a free-text delivery to this harness in this project")
        self._require_bound_route(db, project, original["sender"])
        existing = db.execute(
            """SELECT id,body,key,reply_to FROM messages WHERE project=? AND sender=?
            AND request_id IS NOT NULL AND reply_to IS NOT NULL AND (key=? OR reply_to=?)""",
            (project, sender, key, reply_to),
        ).fetchall()
        if existing:
            if len(existing) == 1 and (
                existing[0]["body"], existing[0]["key"], existing[0]["reply_to"]
            ) == (body, key, reply_to):
                return {"request_id": original["request_id"], "id": existing[0]["id"]}
            raise ValueError("delivery already has a reply or reply key was reused")

        received = original["stage"] == "done" or (
            original["stage"] == "coordinator"
            and original["lease_session"] == session
            and original["deadline"] is not None
            and original["deadline"] > time.time()
        )
        if not received:
            raise ValueError("claim the free-text delivery as coordinator before replying")
        message_id = self._enqueue_routed(
            db, original["request_id"], project, sender, original["sender"],
            body, key, session, reply_to=reply_to,
        )
        return {"request_id": original["request_id"], "id": message_id}

    @staticmethod
    def _enqueue_routed(db, request_id, project, sender, recipient, body, key, session, reply_to=None):
        message_id, now = str(uuid.uuid4()), time.time()
        db.execute(
            """INSERT INTO messages(id,project,sender,recipient,body,key,reply_to,stage,created,
            updated,request_id,sender_session) VALUES(?,?,?,?,?,?,?,'outbound',?,?,?,?)""",
            (message_id, project, sender, recipient, body, key, reply_to, now, now,
             request_id, session),
        )
        return message_id

    @classmethod
    def _validate_free_text(cls, project, sender, body, key, session):
        cls._name(project, "project")
        cls._validate_free_text_route(sender)
        cls._name(body, "body")
        cls._name(key, "key")
        cls._name(session, "session")

    @classmethod
    def _validate_free_text_route(cls, harness):
        cls._name(harness, "harness")
        if harness in HUMAN_NOTIFICATION_ROUTES:
            raise ValueError("free text is reserved for harness-to-harness messaging")

    def _require_bound_route(self, db, project, harness):
        bindings = self._roles(db, project, harness)
        if not all(bindings.get(role) for role in ROLES):
            raise ValueError(f"harness {harness} needs a coordinator and inbox in this project")
        return bindings

    def _require_free_text_sender(self, db, project, harness, session):
        if self._require_bound_route(db, project, harness)["coordinator"] != session:
            raise ValueError("session is not the current coordinator for sender")
        if not self._enabled(db, project, harness):
            raise ValueError("sending is disabled for this project and harness")

    def _enabled(self, db, project, harness):
        if self._managed_route(project, harness):
            return self.bindings_provider.enabled(project, harness)
        row = db.execute(
            "SELECT enabled FROM control WHERE project=? AND harness=?", (project, harness)
        ).fetchone()
        return bool(row and row["enabled"])

    def _can_pickup(self, db, project, harness, role, session):
        return self._roles(db, project, harness).get(role) == session and self._enabled(db, project, harness)

    @staticmethod
    def _pending_rows(db, project, harness, role, now, message_id=None, *, wake_session=None,
                      notifications_only=False):
        first_stage = "outbound" if role == "inbox" else "coordinator"
        wake_filter = """
            AND NOT EXISTS (SELECT 1 FROM mhb_wake_attempts AS wake
            WHERE wake.notification_id=messages.id AND wake.project=messages.project
            AND wake.harness=? AND wake.role=? AND wake.session=?
            AND ((wake.state IN ('sending','uncertain','sent') AND wake.updated_at > ?)
                 OR (wake.state='retryable' AND wake.updated_at + CASE WHEN wake.attempts<=0 THEN 0
                     ELSE MIN(?, ? * (1 << MIN(wake.attempts-1, 6))) END > ?)))
            """ if wake_session is not None else ""
        wake_params = (harness, role, wake_session, now - WAKE_STALE_SECONDS,
                       WAKE_RETRY_MAX_SECONDS, WAKE_RETRY_BASE_SECONDS, now) if wake_session is not None else ()
        fields, limit = ("id,attempts", "") if notifications_only else ("*", "LIMIT 1")
        return db.execute(
            f"""SELECT {fields} FROM messages WHERE project=? AND pickup_harness=? AND stage IN (?,?)
            AND (lease IS NULL OR deadline<=?) AND (? IS NULL OR id=?) {wake_filter}
            ORDER BY created,id {limit}""",
            (project, harness, first_stage, role, now, message_id, message_id, *wake_params),
        )

    @staticmethod
    def _message_payload(row):
        result = {field: row[field] for field in ("id", "body", "sender", "recipient", "reply_to")}
        result["attempt"] = row["attempts"] + 1
        result["updated"] = row["updated"]
        if row["request_id"] is not None:
            result.update(request_id=row["request_id"], stage=row["stage"])
        return result

    def claim(self, project, harness, role, session, *, message_id=None):
        self._validate_claim(project, harness, role, session)
        lease = secrets.token_urlsafe(24)
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_wake_sessions(db)
            if not self._can_pickup(db, project, harness, role, session):
                return None
            now = time.time()
            row = self._pending_rows(db, project, harness, role, now, message_id).fetchone()
            if row is None:
                return None
            db.execute(
                """UPDATE messages SET lease=?,lease_role=?,lease_session=?,deadline=?,
                attempts=attempts+1,updated=? WHERE id=?""",
                (lease, role, session, now + LEASE_SECONDS, now, row["id"]),
            )
            db.execute("""INSERT INTO mhb_wake_sessions
                (project,harness,role,session,notification_id,state,updated_at)
                VALUES(?,?,?,?,?,'claimed',?) ON CONFLICT(project,harness,role,session)
                DO UPDATE SET state='claimed',updated_at=excluded.updated_at
                WHERE mhb_wake_sessions.notification_id=excluded.notification_id""",
                (project, harness, role, session, row["id"], now))
            return {**self._message_payload(row), "lease": lease}

    def pending(self, project, harness, role, session, *, message_id=None, for_wake=False):
        """Read the next eligible notification without claiming or acknowledging it."""
        self._validate_claim(project, harness, role, session)
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN")
            if not self._can_pickup(db, project, harness, role, session):
                return None
            row = self._pending_rows(db, project, harness, role, time.time(), message_id,
                                     wake_session=session if for_wake else None).fetchone()
            return self._message_payload(row) if row else None

    def pending_notifications(self, project, harness, role, session):
        """Read ID and attempt hints for every currently available message for this role."""
        self._validate_claim(project, harness, role, session)
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN")
            if not self._can_pickup(db, project, harness, role, session):
                return []
            rows = self._pending_rows(db, project, harness, role, time.time(), notifications_only=True)
            return [(row["id"], row["attempts"] + 1) for row in rows]

    def pending_details(self, project, harness, role, session):
        """Read body-free hint metadata for every available message for this role."""
        self._validate_claim(project, harness, role, session)
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN")
            if not self._can_pickup(db, project, harness, role, session):
                return []
            first_stage = "outbound" if role == "inbox" else "coordinator"
            rows = db.execute(
                """SELECT id,attempts,sender,reply_to,key,updated FROM messages WHERE project=?
                AND pickup_harness=? AND stage IN (?,?) AND (lease IS NULL OR deadline<=?)
                ORDER BY created,id""", (project, harness, first_stage, role, time.time()))
            return [{"id": row["id"], "attempt": row["attempts"] + 1, "sender": row["sender"],
                     "kind": ("reply" if row["reply_to"] else
                              "notice" if row["key"].startswith("mhb-orchestrator-") else "request"),
                     "updated": row["updated"]}
                    for row in rows]

    def finish(self, project, harness, role, session, id, lease):
        self._validate_claim(project, harness, role, session)
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_wake_sessions(db)
            finished = self._finish_lease(db, project, harness, role, session, id, lease)
            if finished:
                db.execute("""UPDATE mhb_wake_sessions SET state='cooldown',updated_at=?
                    WHERE project=? AND harness=? AND role=? AND session=? AND notification_id=?""",
                    (time.time(), project, harness, role, session, id))
            return finished

    @staticmethod
    def _ensure_wake_sessions(db):
        db.execute("""CREATE TABLE IF NOT EXISTS mhb_wake_sessions (
            project TEXT NOT NULL, harness TEXT NOT NULL, role TEXT NOT NULL,
            session TEXT NOT NULL, notification_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('sending','sent','uncertain','claimed','cooldown')),
            updated_at REAL NOT NULL,
            PRIMARY KEY(project,harness,role,session))""")

    def finish_and_reply(self, project, sender, session, message_id, lease, body, key):
        """Complete an exact coordinator lease and queue its reply in one commit."""
        self._validate_free_text(project, sender, body, key, session)
        self._name(message_id, "message_id")
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_wake_sessions(db)
            if not self._finish_lease(db, project, sender, "coordinator", session, message_id, lease):
                return None
            db.execute("""UPDATE mhb_wake_sessions SET state='cooldown',updated_at=?
                WHERE project=? AND harness=? AND role='coordinator' AND session=? AND notification_id=?""",
                (time.time(), project, sender, session, message_id))
            return self._reply_free_text(db, project, sender, message_id, body, key, session)

    def _finish_lease(self, db, project, harness, role, session, id, lease):
        if self._roles(db, project, harness).get(role) != session:
            return False
        now = time.time()
        row = db.execute(
            """SELECT stage FROM messages WHERE id=? AND project=? AND pickup_harness=?
            AND lease=? AND lease_role=? AND lease_session=? AND deadline>?""",
            (id, project, harness, lease, role, session, now),
        ).fetchone()
        if row is None:
            return False
        # A stop blocks new work; an already-issued lease remains finishable.
        next_stage = {"outbound": "inbox", "inbox": "coordinator", "coordinator": "done"}[row["stage"]]
        db.execute(
            """UPDATE messages SET stage=?,lease=NULL,lease_role=NULL,lease_session=NULL,
            deadline=NULL,updated=? WHERE id=?""",
            (next_stage, now, id),
        )
        return True

    def forward_pull(self, project, harness, *, limit=100):
        """Advance this harness's inbox stages without a model turn.

        Used only when the harness collects coordinator mail itself
        (``coordinator_pickup = pull``). Unleased ``outbound`` items sent by
        this harness move to ``inbox`` and unleased ``inbox`` items addressed to
        it move to ``coordinator``, in one transaction and without a lease, so a
        role handover is never blocked. Both roles must be bound and pickup
        enabled, as for an inbox claim. Returns the forwarded message IDs.
        """
        project, harness = self._name(project, "project"), self._name(harness, "harness")
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            roles = self._roles(db, project, harness)
            if not all(roles.get(role) for role in ROLES) or not self._enabled(db, project, harness):
                return []
            now = time.time()
            rows = db.execute(
                """SELECT id,stage FROM messages WHERE project=? AND pickup_harness=?
                AND stage IN ('outbound','inbox') AND (lease IS NULL OR deadline<=?)
                ORDER BY created,id LIMIT ?""", (project, harness, now, limit)).fetchall()
            moved = []
            for row in rows:
                next_stage = "inbox" if row["stage"] == "outbound" else "coordinator"
                changed = db.execute(
                    """UPDATE messages SET stage=?,lease=NULL,lease_role=NULL,lease_session=NULL,
                    deadline=NULL,updated=? WHERE id=? AND stage=? AND (lease IS NULL OR deadline<=?)""",
                    (next_stage, now, row["id"], row["stage"], now)).rowcount
                if changed:
                    moved.append({"id": row["id"], "stage": next_stage})
            return moved

    def status(self, project, harness):
        with self._route_guard(project), closing(self._connect()) as db, db:
            db.execute("BEGIN")
            enabled = self._enabled(db, project, harness)
            counts = {"outbound": 0, "inbox": 0, "coordinator": 0, "done": 0, "leased": 0}
            for row in db.execute(
                "SELECT stage,COUNT(*) n FROM messages WHERE project=? AND pickup_harness=? GROUP BY stage",
                (project, harness),
            ):
                counts[row["stage"]] = row["n"]
            counts["leased"] = db.execute(
                "SELECT COUNT(*) FROM messages WHERE project=? AND pickup_harness=? AND lease IS NOT NULL AND deadline>?",
                (project, harness, time.time()),
            ).fetchone()[0]
            return {"enabled": enabled, **counts}

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
                    """SELECT id,project,sender,recipient,stage,created,updated,attempts,request_id
                    FROM messages WHERE project=? AND stage<>'done' ORDER BY created,id LIMIT ?""",
                    (project, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT id,project,sender,recipient,stage,created,updated,attempts,request_id
                    FROM messages WHERE project=? AND pickup_harness=? AND stage<>'done'
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
                attempts,lease_role,deadline,reply_to,request_id FROM messages WHERE project=? AND id=?""",
                (project, message_id),
            ).fetchone()
            if row is None:
                return None
            return {**dict(row),
                    "events": [dict(event) for event in db.execute(
                        "SELECT at,event,attempt FROM delivery_events WHERE message_id=? ORDER BY rowid", (message_id,))],
                    "replies": [r[0] for r in db.execute(
                        "SELECT id FROM messages WHERE project=? AND reply_to=? ORDER BY created,id", (project,message_id))]}

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
