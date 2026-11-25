"""Portable Telegram Bot API conduit for the public multi-harness queue.

Bot tokens are read from environment variables. Route configuration contains
only numeric chat/user allowlists. Provider calls are deliberately at-most-once:
an interrupted send remains ``uncertain`` and is never retried automatically.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
import tomllib
import urllib.error
import urllib.request

from .store import Store

logger = logging.getLogger("multi_harness_bridge.telegram")
BRIDGE_HEADING = "Mhb: Bridge Message"


def _bridge_message(text, project, harness):
    """Replace only a leading bridge heading and retain every other body byte."""
    text = str(text)
    heading_prefix = BRIDGE_HEADING + "\n"
    legacy_prefix = "Bridge test message\n"
    while text.startswith(heading_prefix):
        text = text[len(heading_prefix):]
    if text.startswith(legacy_prefix):
        text = text[len(legacy_prefix):]
    route = f"[{project}/{harness}]"
    body = text if text.startswith(route) else f"{route} {text}"
    return f"{BRIDGE_HEADING}\n{body}"


def _canonical_bridge_message(text, project, harness):
    """Normalize legacy and current forms for idempotency comparisons."""
    return _bridge_message(text, project, harness)


@dataclass(frozen=True)
class Route:
    bot: str
    chat: str
    users: frozenset[str]
    project: str
    harness: str


class TelegramAPI:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org", timeout: int = 35):
        if not token:
            raise ValueError("Telegram bot token is required")
        self.base, self.token, self.timeout = api_base.rstrip("/"), token, timeout

    def call(self, method: str, payload: dict | None = None):
        request = urllib.request.Request(
            f"{self.base}/bot{self.token}/{method}",
            data=json.dumps(payload or {}).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            raise RuntimeError("Telegram Bot API request failed") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Telegram Bot API rejected the request")
        return result.get("result")

    def get_updates(self, offset=None, timeout=20):
        result = self.call("getUpdates", {"timeout": timeout, "allowed_updates": ["message"], **(
            {"offset": offset} if offset is not None else {})})
        if not isinstance(result, list):
            raise RuntimeError("Telegram returned an invalid update list")
        return result

    def send_message(self, chat, text, reply_to=None):
        payload = {"chat_id": chat, "text": text}
        if reply_to is not None:
            payload.update(reply_to_message_id=reply_to, allow_sending_without_reply=False)
        result = self.call("sendMessage", payload)
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise RuntimeError("Telegram send receipt is missing")
        return result["message_id"]


class RejectedUpdate(ValueError):
    """A malformed, unauthorized, or ambiguous Telegram update."""


def load_config(path, environ=None):
    env = os.environ if environ is None else environ
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    bots = {}
    for alias, item in data.get("bots", {}).items():
        name = item.get("token_env")
        token = env.get(name, "") if name else ""
        if not token:
            raise ValueError(f"Missing Telegram bot token environment variable: {name}")
        bots[alias] = TelegramAPI(token, item.get("api_base", "https://api.telegram.org"))
    if not bots:
        raise ValueError("At least one Telegram bot must be configured")
    routes = []
    for item in data.get("routes", []):
        bot, chat, project, harness = (item.get(k) for k in ("bot", "chat_id", "project", "harness"))
        users = item.get("allowed_users", [])
        if bot not in bots or not all(isinstance(v, str) and v for v in (chat, project, harness)):
            raise ValueError("Telegram route requires a configured bot, chat, project and harness")
        if not re.fullmatch(r"-?[0-9]+", chat) or not users or any(
                not isinstance(user, (str, int)) or not re.fullmatch(r"[0-9]+", str(user)) for user in users):
            raise ValueError("Telegram routes require numeric chat and non-empty user allowlists")
        routes.append(Route(bot, chat, frozenset(map(str, users)), project, harness))
    if not routes:
        raise ValueError("At least one Telegram route must be configured")
    selectors = [(r.bot, r.chat, r.project, r.harness) for r in routes]
    if len(set(selectors)) != len(selectors):
        raise ValueError("Duplicate Telegram route")
    destinations = [(r.project, r.harness) for r in routes]
    if len(set(destinations)) != len(destinations):
        raise ValueError("Configure one Telegram destination per project and harness")
    return bots, tuple(routes)


class TelegramBridge:
    def __init__(self, path, store: Store, routes, apis, *, clock=time.time):
        self.path, self.store, self.routes, self.apis, self.clock = Path(path), store, tuple(routes), apis, clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS telegram_updates (
                    bot TEXT NOT NULL, update_id INTEGER NOT NULL, chat TEXT NOT NULL,
                    message_id INTEGER NOT NULL, project TEXT NOT NULL, harness TEXT NOT NULL,
                    inbound_key TEXT NOT NULL, fingerprint TEXT, queue_id TEXT,
                    source_queue_id TEXT, allowed_users TEXT,
                    PRIMARY KEY(bot, update_id),
                    UNIQUE(bot, chat, message_id));
                CREATE TABLE IF NOT EXISTS telegram_polling (
                    bot TEXT PRIMARY KEY, next_offset INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS telegram_forwards (
                    queue_id TEXT PRIMARY KEY, project TEXT NOT NULL, harness TEXT NOT NULL,
                    bot TEXT NOT NULL, chat TEXT NOT NULL, message_id INTEGER NOT NULL,
                    allowed_users TEXT);
                CREATE TABLE IF NOT EXISTS telegram_sends (
                    project TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
                    key TEXT NOT NULL, body TEXT NOT NULL, bot TEXT, chat TEXT NOT NULL,
                    reply_to INTEGER, state TEXT NOT NULL, message_id INTEGER,
                    route_harness TEXT, source_queue_id TEXT, reply_queue_id TEXT,
                    allowed_users TEXT,
                    PRIMARY KEY(project, sender, recipient, key));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(telegram_updates)")}
            if "fingerprint" not in columns:
                db.execute("ALTER TABLE telegram_updates ADD COLUMN fingerprint TEXT")
            if "queue_id" not in columns:
                db.execute("ALTER TABLE telegram_updates ADD COLUMN queue_id TEXT")
            if "source_queue_id" not in columns:
                db.execute("ALTER TABLE telegram_updates ADD COLUMN source_queue_id TEXT")
            if "allowed_users" not in columns:
                db.execute("ALTER TABLE telegram_updates ADD COLUMN allowed_users TEXT")
            columns = {row[1] for row in db.execute("PRAGMA table_info(telegram_sends)")}
            if "route_harness" not in columns:
                db.execute("ALTER TABLE telegram_sends ADD COLUMN route_harness TEXT")
            if "source_queue_id" not in columns:
                db.execute("ALTER TABLE telegram_sends ADD COLUMN source_queue_id TEXT")
            if "bot" not in columns:
                db.execute("ALTER TABLE telegram_sends ADD COLUMN bot TEXT")
            if "reply_queue_id" not in columns:
                db.execute("ALTER TABLE telegram_sends ADD COLUMN reply_queue_id TEXT")
            if "allowed_users" not in columns:
                db.execute("ALTER TABLE telegram_sends ADD COLUMN allowed_users TEXT")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS telegram_provider_receipt
                ON telegram_sends(bot, chat, message_id)
                WHERE state='sent' AND message_id IS NOT NULL""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS telegram_source_queue
                ON telegram_sends(project, source_queue_id)
                WHERE source_queue_id IS NOT NULL""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(telegram_forwards)")}
            if "allowed_users" not in columns:
                db.execute("ALTER TABLE telegram_forwards ADD COLUMN allowed_users TEXT")
        for project in sorted({route.project for route in routes}):
            sessions = {"inbox": f"pma-inbox-{project}",
                        "coordinator": f"pma-coordinator-{project}"}
            bound = store.roles(project, "pma")
            for role, session in sessions.items():
                if bound.get(role) != session:
                    store.bind(project, "pma", role, session)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def _route_for_incoming(self, bot, chat, user, text, reply_to):
        eligible = [r for r in self.routes if r.bot == bot and r.chat == str(chat) and str(user) in r.users]
        # A native reply is authoritative only when its referenced message has
        # a persisted project/harness receipt. Otherwise require [project].
        project = harness = source_queue_id = None
        if reply_to is not None:
            with self.connect() as db:
                row = db.execute("""SELECT project,harness,allowed_users FROM telegram_updates
                    WHERE bot=? AND chat=? AND message_id=?""", (bot, str(chat), reply_to)).fetchone()
            if row:
                project, harness = row["project"], row["harness"]
            else:
                with self.connect() as db:
                    row = db.execute("""SELECT project,route_harness AS harness,source_queue_id,allowed_users FROM telegram_sends
                        WHERE bot=? AND chat=? AND message_id=? AND state='sent'""", (bot, str(chat), reply_to)).fetchone()
                if row:
                    project, harness = row["project"], row["harness"]
                    source_queue_id = row["source_queue_id"]
            if row:
                try:
                    allowed = frozenset(json.loads(row["allowed_users"] or "[]"))
                except (TypeError, ValueError):
                    allowed = frozenset()
                if str(user) in allowed:
                    eligible = [Route(bot, str(chat), allowed, project, harness)]
        if not eligible:
            raise RejectedUpdate("Telegram chat or user is not allowlisted")
        match = re.match(r"^\[([a-z][a-z0-9_-]*)(?:/([a-z][a-z0-9_-]*))?\]\s+", text)
        hinted = match.group(1) if match else None
        hinted_harness = match.group(2) if match else None
        candidates = [r for r in eligible if (project is None or r.project == project)
                      and (harness is None or r.harness == harness)
                      and (hinted is None or r.project == hinted)
                      and (hinted_harness is None or r.harness == hinted_harness)]
        projects = {(r.project, r.harness) for r in candidates}
        if len(projects) != 1:
            raise RejectedUpdate("Telegram project route is ambiguous or unauthorized")
        route = candidates[0]
        body = text[match.end():] if match else text
        if not body.strip():
            raise RejectedUpdate("Telegram message text is empty")
        return route, body, source_queue_id

    def receive(self, bot, update):
        """Validate and enqueue one Bot API update; duplicate updates are harmless."""
        if bot not in self.apis or not isinstance(update, dict) or type(update.get("update_id")) is not int:
            raise RejectedUpdate("Invalid Telegram update")
        message = update.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("text"), str):
            raise RejectedUpdate("Only Telegram text messages are supported")
        chat, mid = message.get("chat", {}).get("id"), message.get("message_id")
        sender = message.get("from", {}).get("id")
        if type(chat) is not int or type(mid) is not int or type(sender) is not int:
            raise RejectedUpdate("Telegram message is missing numeric identity fields")
        reply = message.get("reply_to_message", {}).get("message_id")
        try:
            route, body, source_queue_id = self._route_for_incoming(bot, chat, sender, message["text"], reply)
        except RejectedUpdate:
            raise
        except ValueError as exc:
            raise RejectedUpdate(str(exc)) from None
        key = f"telegram:{bot}:{chat}:{mid}"
        envelope = json.dumps({"text": body, "telegram": {"bot": bot, "chat_id": str(chat),
                               "user_id": str(sender), "message_id": mid,
                               "reply_to_message_id": reply}, "harness": route.harness,
                               "source_queue_id": source_queue_id},
                              ensure_ascii=False, separators=(",", ":"))
        fingerprint = hashlib.sha256(envelope.encode("utf-8")).hexdigest()
        with self.connect() as db:
            old = db.execute("""SELECT * FROM telegram_updates WHERE bot=? AND
                (update_id=? OR (chat=? AND message_id=?)) LIMIT 1""",
                             (bot, update["update_id"], str(chat), mid)).fetchone()
            if old:
                if (old["chat"], old["message_id"], old["project"], old["harness"]) != (
                        str(chat), mid, route.project, route.harness) or (
                            old["fingerprint"] is not None and old["fingerprint"] != fingerprint):
                    raise RejectedUpdate("Telegram update ID conflicts with stored provenance")
                return False
        receipt = self.store.send(route.project, "telegram", "pma", envelope, key, origin="user")
        with self.connect() as db:
            db.execute("""INSERT OR IGNORE INTO telegram_updates
                (bot,update_id,chat,message_id,project,harness,inbound_key,fingerprint,queue_id,source_queue_id,allowed_users)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (bot, update["update_id"], str(chat), mid, route.project, route.harness,
                        key, fingerprint, receipt["id"], source_queue_id,
                        json.dumps(sorted(route.users), separators=(",", ":"))))
            stored = db.execute("SELECT fingerprint FROM telegram_updates WHERE bot=? AND update_id=?",
                                (bot, update["update_id"])).fetchone()
            if stored[0] is not None and stored[0] != fingerprint:
                raise RejectedUpdate("Telegram update ID conflicts with its stored content")
        return receipt

    def receive_hermes_event(self, bot, event):
        """Adapt an already-authorized Hermes Telegram event into the same queue path."""
        source = getattr(event, "source", None)
        platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
        if platform != "telegram":
            return None
        message_id = getattr(event, "message_id", None)
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            raise RejectedUpdate("Hermes Telegram event has no numeric message ID") from None
        chat = getattr(source, "chat_id", None)
        user = getattr(event, "user_id", None) or getattr(source, "user_id", None)
        try:
            chat, user = int(chat), int(user)
        except (TypeError, ValueError):
            raise RejectedUpdate("Hermes Telegram event has no numeric chat or user ID") from None
        if not any(route.bot == bot and route.chat == str(chat) for route in self.routes):
            return None
        text = getattr(event, "text", "")
        if not isinstance(text, str):
            return {"rejected": True}
        reply = getattr(event, "reply_to_message_id", None)
        if reply is not None:
            try:
                reply = int(reply)
            except (TypeError, ValueError):
                raise RejectedUpdate("Hermes Telegram reply has an invalid message ID") from None
        update_id = int.from_bytes(hashlib.sha256(f"{chat}:{message_id}".encode()).digest()[:8], "big") & ((1 << 63) - 1)
        message = {"message_id": message_id, "chat": {"id": chat}, "from": {"id": user}, "text": text}
        if reply is not None:
            message["reply_to_message"] = {"message_id": reply}
        try:
            return self.receive(bot, {"update_id": update_id, "message": message})
        except RejectedUpdate:
            return {"rejected": True}

    def register_hermes_hook(self, ctx, bot):
        """Register a Hermes pre-dispatch hook; use this instead of Bot API polling."""
        async def hook(event, gateway, **kwargs):
            try:
                result = self.receive_hermes_event(bot, event)
            except RejectedUpdate:
                return {"action": "skip", "reason": "multi-harness-telegram-rejected"}
            if result is not None:
                reason = ("multi-harness-telegram-rejected" if isinstance(result, dict) and result.get("rejected")
                          else "multi-harness-telegram-routed")
                return {"action": "skip", "reason": reason, "receipt": result}
            return None
        ctx.register_hook("pre_gateway_dispatch", hook)
        return hook

    async def hermes_tick(self):
        """Flush Telegram-bound queue messages when called by a Hermes task loop."""
        return await asyncio.to_thread(self.tick)

    def send(self, project, sender, recipient, body, key, reply_to=None, source_queue_id=None):
        """Send one queued notification and retain the provider message ID.

        ``reply_to`` is the source queue UUID. The exact provider destination,
        thread ID and provenance are persisted before sending. An uncertain
        provider call is never repeated automatically.
        """
        if not all(isinstance(v, str) and v.strip() for v in (project, sender, recipient, body, key)):
            raise ValueError("project, sender, recipient, body and key are required")
        with self.connect() as db:
            old = db.execute("SELECT * FROM telegram_sends WHERE project=? AND sender=? AND recipient=? AND key=?",
                             (project, sender, recipient, key)).fetchone()
        if old:
            route_harness = old["route_harness"]
            visible_body = _bridge_message(body, project, route_harness)
            if (_canonical_bridge_message(old["body"], project, route_harness),
                    old["source_queue_id"], old["reply_queue_id"]) != (
                    visible_body, source_queue_id, reply_to):
                raise ValueError("Telegram send key was reused with different content or provenance")
            if old["state"] != "unsent":
                return dict(old)
            bot, chat, reply_mid = old["bot"], old["chat"], old["reply_to"]
            if bot not in self.apis:
                raise ValueError("The bot that owns this persisted send is not configured")
            with self.connect() as db:
                db.execute("UPDATE telegram_sends SET state='uncertain' WHERE project=? AND sender=? AND recipient=? AND key=? AND state='unsent'",
                           (project, sender, recipient, key))
            api = self.apis[bot]
        else:
            reply_mid = None
            route_harness = sender
            persisted_route = None
            if reply_to:
                with self.connect() as db:
                    row = db.execute("SELECT bot,chat,message_id,project,harness,allowed_users FROM telegram_updates WHERE queue_id=?", (reply_to,)).fetchone()
                    if row is None:
                        row = db.execute("SELECT bot,chat,message_id,project,harness,allowed_users FROM telegram_forwards WHERE queue_id=?", (reply_to,)).fetchone()
                if not row or row["project"] != project:
                    raise ValueError("Reply provenance does not identify a message in this project")
                if row["harness"] != sender:
                    raise ValueError("Reply provenance does not match the originating harness")
                reply_mid = row["message_id"]
                try:
                    users = frozenset(json.loads(row["allowed_users"] or "[]"))
                except (TypeError, ValueError):
                    users = frozenset()
                persisted_route = Route(row["bot"], row["chat"], users, project, route_harness)
            routes = [r for r in self.routes if r.project == project and r.harness == route_harness]
            if len(routes) == 1:
                route = routes[0]
            elif persisted_route is not None and persisted_route.bot in self.apis:
                route = persisted_route
            else:
                raise ValueError("No unique Telegram destination is configured for this project and harness")
            visible_body = _bridge_message(body, project, route_harness)
            bot, chat, api = route.bot, route.chat, self.apis[route.bot]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if old is None:
                db.execute("""INSERT INTO telegram_sends
                (project,sender,recipient,key,body,bot,chat,reply_to,state,message_id,route_harness,source_queue_id,reply_queue_id,allowed_users)
                VALUES(?,?,?,?,?,?,?,?,'uncertain',NULL,?,?,?,?)""",
                           (project, sender, recipient, key, visible_body, bot, chat, reply_mid,
                            route_harness, source_queue_id, reply_to,
                            json.dumps(sorted(route.users), separators=(",", ":"))))
        # Commit uncertain before the external side effect. A timeout can mean
        # Telegram accepted the message, so recovery must not make another send.
        message_id = api.send_message(chat, visible_body, reply_mid)
        if type(message_id) is not int or message_id <= 0:
            raise RuntimeError("Telegram send outcome is uncertain; reconcile manually")
        with self.connect() as db:
            db.execute("UPDATE telegram_sends SET state='sent',message_id=? WHERE project=? AND sender=? AND recipient=? AND key=? AND state='uncertain'",
                       (message_id, project, sender, recipient, key))
            return dict(db.execute("SELECT * FROM telegram_sends WHERE project=? AND sender=? AND recipient=? AND key=?",
                                  (project, sender, recipient, key)).fetchone())

    def claim(self, project, harness, role, session):
        return self.store.claim(project, harness, role, session)

    def finish(self, project, harness, role, session, message_id, lease):
        return self.store.finish(project, harness, role, session, message_id, lease)

    def _process_coordinator_item(self, project, item):
        coordinator_session = f"pma-coordinator-{project}"
        if item["sender"] == "telegram":
            envelope = json.loads(item["body"])
            target = envelope.get("harness")
            body = envelope.get("text")
            if not isinstance(body, str) or not isinstance(target, str):
                raise ValueError("Telegram queue envelope has no text or harness destination")
            source = envelope.get("source_queue_id")
            forwarded = self.store.send(project, "pma", target, body, f"telegram-forward:{item['id']}", source,
                                        session=coordinator_session, origin="user")
            with self.connect() as db:
                source_row = db.execute("SELECT bot,chat,message_id,allowed_users FROM telegram_updates WHERE queue_id=?",
                                        (item["id"],)).fetchone()
                if source_row is None:
                    raise RuntimeError("PMA input lost its Telegram provenance")
                db.execute("""INSERT OR IGNORE INTO telegram_forwards
                    (queue_id,project,harness,bot,chat,message_id,allowed_users) VALUES(?,?,?,?,?,?,?)""",
                           (forwarded["id"], project, target, source_row["bot"], source_row["chat"],
                            source_row["message_id"], source_row["allowed_users"]))
        else:
            configured = any(route.project == project and route.harness == item["sender"] for route in self.routes)
            if not configured:
                with self.connect() as db:
                    saved = db.execute("SELECT 1 FROM telegram_sends WHERE project=? AND source_queue_id=?",
                                       (project, item["id"])).fetchone()
                if not saved:
                    raise ValueError("PMA message sender has no allowlisted Telegram project route")
            sent = self.send(project, item["sender"], "pma", item["body"], f"queue:{item['id']}",
                             item.get("reply_to"), item["id"])
            if sent["state"] != "sent":
                # Keep the coordinator lease and the durable queue item in place
                # until a human reconciles a potentially accepted provider send.
                return False
        if not self.store.finish(project, "pma", "coordinator", coordinator_session, item["id"], item["lease"]):
            raise RuntimeError("PMA coordinator delivery could not be acknowledged")
        return True

    def deliver_pending(self, project, limit=50):
        """Advance PMA inbox and independently drain recoverable coordinator work."""
        inbox_session = f"pma-inbox-{project}"
        coordinator_session = f"pma-coordinator-{project}"
        processed = 0
        for _ in range(limit):
            incoming = self.store.claim(project, "pma", "inbox", inbox_session)
            if incoming is None:
                break
            if not self.store.finish(project, "pma", "inbox", inbox_session,
                                     incoming["id"], incoming["lease"]):
                raise RuntimeError("PMA inbox message could not be advanced")
            # Inbox advances are not yet completed deliveries; coordinator
            # work below is the durable unit counted by this tick.
        # This claim runs even with an empty inbox so coordinator-stage messages
        # left by a restart can be completed after their lease expires.
        for _ in range(limit):
            item = self.store.claim(project, "pma", "coordinator", coordinator_session)
            if item is None:
                break
            if not self._process_coordinator_item(project, item):
                break
            processed += 1
        return processed

    def tick(self, *, isolate_errors=False):
        delivered = 0
        for project in sorted({route.project for route in self.routes}):
            try:
                delivered += self.deliver_pending(project)
            except Exception as exc:
                if not isolate_errors:
                    raise
                logger.warning("Telegram project tick failed: project=%s error=%s", project, type(exc).__name__)
        return delivered

    def poll_all(self, *, isolate_errors=False):
        total = 0
        for bot in self.apis:
            try:
                total += self.poll_once(bot, isolate_project_errors=isolate_errors)
            except Exception as exc:
                if not isolate_errors:
                    raise
                logger.warning("Telegram bot cycle failed: bot=%s error=%s", bot, type(exc).__name__)
        return total

    @staticmethod
    def reconcile_send(path, project, queue_id, outcome, provider_message_id=None, queue_path=None):
        """Resolve one uncertain provider result by exact project and source queue UUID."""
        if not all(isinstance(value, str) and value.strip() for value in (project, queue_id)):
            raise ValueError("project and queue ID are required")
        if outcome not in {"confirmed-sent", "verified-unsent"}:
            raise ValueError("outcome must be confirmed-sent or verified-unsent")
        if outcome == "confirmed-sent" and (type(provider_message_id) is not int or provider_message_id <= 0):
            raise ValueError("confirmed-sent requires a positive Telegram message ID")
        if outcome == "verified-unsent" and provider_message_id is not None:
            raise ValueError("verified-unsent does not accept a Telegram message ID")
        db = sqlite3.connect(path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM telegram_sends WHERE project=? AND source_queue_id=?",
                             (project, queue_id)).fetchone()
            if not row:
                raise ValueError("No Telegram send is recorded for that project and queue ID")
            desired = "sent" if outcome == "confirmed-sent" else "unsent"
            if row["state"] == desired:
                if desired == "sent" and row["message_id"] != provider_message_id:
                    raise ValueError("Confirmed provider ID conflicts with the stored receipt")
                db.commit()
                TelegramBridge._release_reconciled_lease(queue_path, project, queue_id)
                return dict(row)
            if row["state"] != "uncertain":
                raise ValueError(f"Cannot reconcile a Telegram send in state {row['state']}")
            db.execute("UPDATE telegram_sends SET state=?,message_id=? WHERE project=? AND source_queue_id=? AND state='uncertain'",
                       (desired, provider_message_id, project, queue_id))
            db.commit()
            TelegramBridge._release_reconciled_lease(queue_path, project, queue_id)
            result = db.execute("SELECT * FROM telegram_sends WHERE project=? AND source_queue_id=?",
                                (project, queue_id)).fetchone()
            return dict(result)
        except sqlite3.IntegrityError:
            raise ValueError("Telegram provider message ID is already recorded for this bot and chat") from None
        finally:
            db.close()

    @staticmethod
    def _release_reconciled_lease(queue_path, project, queue_id):
        if queue_path is None:
            return
        with sqlite3.connect(queue_path, timeout=5) as queue_db:
            changed = queue_db.execute("""UPDATE messages SET lease=NULL,lease_role=NULL,
                lease_session=NULL,deadline=NULL WHERE project=? AND id=? AND stage='coordinator'""",
                                       (project, queue_id)).rowcount
            if changed != 1:
                row = queue_db.execute("SELECT stage FROM messages WHERE project=? AND id=?",
                                       (project, queue_id)).fetchone()
                if not row or row[0] != "done":
                    raise ValueError("The matching PMA coordinator queue item was not found")

    def poll_once(self, bot, *, isolate_project_errors=False):
        if bot not in self.apis:
            raise ValueError("Unknown Telegram bot")
        with self.connect() as db:
            row = db.execute("SELECT next_offset FROM telegram_polling WHERE bot=?", (bot,)).fetchone()
        offset = row[0] if row else None
        updates = self.apis[bot].get_updates(offset, timeout=0)
        for update in updates:
            try:
                self.receive(bot, update)
            except RejectedUpdate:
                # Reject unauthorized or ambiguous updates and advance past them.
                pass
            next_offset = update.get("update_id", 0) + 1
            with self.connect() as db:
                db.execute("INSERT INTO telegram_polling VALUES(?,?) ON CONFLICT(bot) DO UPDATE SET next_offset=MAX(next_offset,excluded.next_offset)",
                           (bot, next_offset))
        self.tick(isolate_errors=isolate_project_errors)
        return len(updates)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the multi-harness Telegram Bot API conduit")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--database", type=Path, help="shared queue database path")
    parser.add_argument("--telegram-state", type=Path)
    parser.add_argument("--mode", choices=("poll", "queue-only"), default="poll",
                        help="poll owns Telegram updates; queue-only is for Hermes-owned polling")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--reconcile-project")
    parser.add_argument("--reconcile-queue-id")
    parser.add_argument("--reconcile-outcome", choices=("confirmed-sent", "verified-unsent"))
    parser.add_argument("--provider-message-id", type=int)
    args = parser.parse_args(argv)
    reconcile_requested = any((args.reconcile_project, args.reconcile_queue_id, args.reconcile_outcome,
                               args.provider_message_id is not None))
    if reconcile_requested:
        if not (args.telegram_state and args.database and args.reconcile_project and args.reconcile_queue_id and args.reconcile_outcome):
            parser.error("reconciliation requires --database, --telegram-state, --reconcile-project, --reconcile-queue-id and --reconcile-outcome")
        result = TelegramBridge.reconcile_send(args.telegram_state, args.reconcile_project,
                                              args.reconcile_queue_id, args.reconcile_outcome,
                                              args.provider_message_id, args.database)
        print(f"Reconciled project={result['project']} queue_id={args.reconcile_queue_id} state={result['state']}")
        return 0
    if not (args.config and args.database and args.telegram_state):
        parser.error("--config, --database, and --telegram-state are required to run the service")
    from .config import load
    config = load()
    store = Store(args.database)
    apis, routes = load_config(args.config)
    for route in routes:
        config.require(route.project)
    bridge = TelegramBridge(args.telegram_state, store, routes, apis)
    while True:
        if args.mode == "poll":
            bridge.poll_all(isolate_errors=not args.once)
        else:
            bridge.tick(isolate_errors=not args.once)
        if args.once:
            return 0
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
