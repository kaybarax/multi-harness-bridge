"""Role-scoped access to one project over MCP or a loopback A2A connection."""
import hashlib
import os
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .config import identifier, open_store


@dataclass(frozen=True)
class Principal:
    project: str
    harness: str
    role: str
    session: str


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _credentials(config):
    db = sqlite3.connect(config.root / "credentials.sqlite3")
    db.execute("""CREATE TABLE IF NOT EXISTS credentials(
        digest TEXT PRIMARY KEY, project TEXT NOT NULL, harness TEXT NOT NULL,
        role TEXT NOT NULL, session TEXT NOT NULL)""")
    return db


def verify_role(store, principal):
    if store.roles(principal.project, principal.harness).get(principal.role) != principal.session:
        raise PermissionError("Credential belongs to a retired or unbound session")


def issue(config, principal, output):
    config.require(principal.project)
    identifier(principal.harness)
    if principal.role not in ("inbox", "coordinator"):
        raise ValueError("Unknown role")
    verify_role(open_store(config), principal)
    token = secrets.token_urlsafe(32)
    output = Path(output).expanduser()
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        with closing(_credentials(config)) as db, db:
            db.execute("INSERT INTO credentials VALUES(?,?,?,?,?)",
                       (_digest(token), principal.project, principal.harness, principal.role, principal.session))
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def authenticate(config, token):
    if not isinstance(token, str) or not token.isascii() or len(token) != 43:
        raise PermissionError("Invalid bearer credential")
    with closing(_credentials(config)) as db:
        row = db.execute("SELECT project,harness,role,session FROM credentials WHERE digest=?",
                         (_digest(token),)).fetchone()
    if row is None:
        raise PermissionError("Invalid bearer credential")
    principal = Principal(*row)
    config.require(principal.project)
    return principal


def revoke(config, token):
    with closing(_credentials(config)) as db, db:
        return db.execute("DELETE FROM credentials WHERE digest=?", (_digest(token),)).rowcount


def dispatch(config, token, command, *, remote=False):
    if not isinstance(command, dict):
        raise ValueError("Expected a JSON object")
    owner = authenticate(config, token)
    project, harness, session = owner.project, owner.harness, owner.session
    store = open_store(config)
    verify_role(store, owner)
    op = command.get("op")
    required = {"status": (), "claim": (), "finish": ("id", "lease"), "send": ("recipient", "body", "key"),
                "send_fanout": ("recipients", "body", "key"), "reply_free_text": ("reply_to", "body", "key"),
                "stop": (), "resume": ()}
    optional = {"claim": ("notification_id",), "send": ("reply_to",)}
    if not isinstance(op, str) or op not in required or set(command) - {"op", *required[op], *optional.get(op, ())}:
        raise ValueError("Unknown operation or unexpected arguments")
    for key in required[op]:
        if key not in command:
            raise ValueError(f"Missing {key}")
    from . import skills, wake_policy
    action = {"status": "read", "claim": "read", "finish": "finish", "send": "send",
              "send_fanout": "send", "reply_free_text": "reply"}.get(op, "settings")
    if op == "send" and command.get("reply_to"):
        action = "reply"
    if remote:
        # A network caller's turn cannot be classified: it may message (counted toward the
        # loop cap) but never change settings.
        kind = wake_policy.UNKNOWN
        wake_policy.check(config.root, project, harness, kind, action)
    else:
        kind, _ = skills.enforce_caller(config, project, action, reply_to=command.get("reply_to"))
    sent_from = {"origin": wake_policy.origin(kind)}
    if op == "status":
        return store.status(project, harness)
    if op == "claim":
        return {"message": store.claim(project, harness, owner.role, session, message_id=command.get("notification_id"))}
    if op == "finish":
        return {"result": store.finish(project, harness, owner.role, session, command["id"], command["lease"])}
    if owner.role != "coordinator":
        raise PermissionError("Coordinator ownership is required for this operation")
    if op == "send":
        return store.send(project, harness, command["recipient"], command["body"], command["key"],
                          reply_to=command.get("reply_to"), session=session, **sent_from)
    if op == "send_fanout":
        return store.send_fanout(project, harness, command["recipients"], command["body"], command["key"],
                                 session=session, **sent_from)
    if op == "reply_free_text":
        return store.reply_free_text(project, harness, command["reply_to"], command["body"], command["key"],
                                     session=session, **sent_from)
    return {"result": store.set_enabled(project, harness, op == "resume", session=session)}


def serve_mcp(config, credential):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("Install the mcp extra to run this server") from exc
    path = Path(credential).expanduser()
    server = FastMCP("Multi Harness Bridge")

    def run(op, **fields):
        return dispatch(config, path.read_text(encoding="ascii").strip(), {"op": op, **fields})

    @server.tool()
    def bridge_status() -> dict:
        """Read this project and harness's current roles and pickup state."""
        return run("status")

    @server.tool()
    def receive_message(notification_id: str | None = None) -> dict:
        """Lease this role's pending message. Pass the exact notification ID when woken."""
        return run("claim", notification_id=notification_id)

    @server.tool()
    def finish_message(id: str, lease: str) -> dict:
        """Inbox forwards to the coordinator. Coordinator acknowledges its own lease."""
        return run("finish", id=id, lease=lease)

    @server.tool()
    def send_message(recipient: str, body: str, key: str, reply_to: str | None = None) -> dict:
        """Send through this coordinator's project bridge using a stable idempotency key."""
        return run("send", recipient=recipient, body=body, key=key, reply_to=reply_to)

    @server.tool()
    def send_fanout(recipients: list[str], body: str, key: str) -> dict:
        """Send one free-text request to all named peer harnesses in this project. Reuse the key on retry."""
        return run("send_fanout", recipients=recipients, body=body, key=key)

    @server.tool()
    def reply_free_text(reply_to: str, body: str, key: str) -> dict:
        """Send a correlated reply using the received request's delivery ID. Reuse the reply key on retry."""
        return run("reply_free_text", reply_to=reply_to, body=body, key=key)

    @server.tool()
    def set_pickup(enabled: bool) -> dict:
        """Coordinator stops or resumes this harness for its own project."""
        return run("resume" if enabled else "stop")

    server.run(transport="stdio")
