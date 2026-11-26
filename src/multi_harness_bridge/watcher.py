"""Shared per-project watcher for coordinator pull mode.

One detached process per project, owned by the OS service manager rather
than by any harness app. Each pass it

* forwards the inbox stages of every ``pull`` harness (no model turn),
* runs the wake dispatcher for ``pull`` harnesses only (non-navigating nudges
  and the one logged fallback; see ``pickup``),
* appends new deliveries to the project's history feed,
* marks a digest ready for each bridge chat whose digest is on,
* checks coordinator listener health and raises one alert per failure,
* and writes its heartbeat.

It never claims or acknowledges coordinator mail. Harnesses on ``push`` are
left to the original wake service. A single OS file lock beside the store
keeps one watcher per project; the kernel releases it when the process dies,
so a leftover file never blocks a start.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import time

from . import config
from . import pickup, wake
from .store import SCHEMA_VERSION

POLL_SECONDS = 5.0
HEARTBEAT_STALE_SECONDS = 30.0
LISTENER_STALE_SECONDS = 30.0
FEED_EXCERPT = 160


def _version():
    try:
        from importlib.metadata import version
        return version("multi-harness-bridge")
    except Exception:  # pragma: no cover - source checkout without metadata
        return "unknown"


def paths(root, project):
    base = Path(root) / "watch"
    return {"lock": base / f"{project}.lock", "heartbeat": base / f"{project}.json",
            "state": base / f"{project}-state.json", "feed": base / f"{project}-feed.jsonl",
            "log": base / f"{project}.log"}


# Single instance ------------------------------------------------------------

class Lock:
    """Non-blocking exclusive OS lock; released by the kernel on process exit."""

    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows only
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def lock_held(root, project):
    probe = Lock(paths(root, project)["lock"])
    if probe.acquire():
        probe.release()
        return False
    return True


def _pid_running(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def health(root, project, *, clock=time.time):
    """Read-only: is the project's watcher alive, hung, or stopped?

    Never touches the lock, so a check can't make a starting watcher lose it.
    """
    try:
        beat = json.loads(paths(root, project)["heartbeat"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        beat = {}
    age = clock() - float(beat.get("beat_at", 0)) if beat else None
    held = bool(beat) and beat.get("stopped") is not True and _pid_running(beat.get("pid"))
    if held and age is not None and age <= HEARTBEAT_STALE_SECONDS:
        status = "alive"
    elif held:
        status = "hung"
    else:
        status = "stopped"
    return {"status": status, "pid": beat.get("pid"), "version": beat.get("version"),
            "token": beat.get("token"), "started_at": beat.get("started_at"),
            "beat_age_seconds": None if age is None else round(age, 1)}


def _service_label(project):
    return f"org.multi-harness-bridge.watch.{project}"


def ensure(root, project, *, executable=None, start=None):
    """Start the watcher only if no live one holds the lock; report its health.

    A hung watcher (lock held, heartbeat stale) is restarted through the service
    manager when one is installed, never by starting a second process.
    """
    state = health(root, project)
    if state["status"] == "alive":
        return {**state, "action": "already-running"}
    label = _service_label(project)
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if start is None and sys.platform == "darwin" and plist.is_file():
        domain = f"gui/{os.getuid()}"
        loaded = subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True,
                                text=True, check=False).returncode == 0
        if not loaded:
            subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, check=False)
        subprocess.run(["launchctl", "kickstart", *(["-k"] if state["status"] == "hung" else []),
                        f"{domain}/{label}"], capture_output=True, check=False)
        action = "service-restarted" if state["status"] == "hung" else "service-started"
    elif state["status"] == "hung":
        return {**state, "action": "hung-needs-service-restart"}
    else:
        argv = [executable or sys.executable, "-m", "multi_harness_bridge.watcher", "--project", project]
        (start or _spawn_detached)(argv, root)
        action = "started"
    for _ in range(50):
        state = health(root, project)
        if state["status"] == "alive":
            break
        time.sleep(0.1)
    return {**state, "action": action}


def _spawn_detached(argv, root):
    env = dict(os.environ, MHB_HOME=str(root))
    log = paths(root, project_from(argv))["log"]
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(log, "ab") as out:
        kwargs = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env,
                         close_fds=True, **kwargs)


def project_from(argv):
    return argv[argv.index("--project") + 1]


# Store change signal ----------------------------------------------------------

class StoreWatch:
    """Wake early on writes to the store or its WAL; a timeout poll is the backstop."""

    def __init__(self, database):
        self.files = [Path(database), Path(str(database) + "-wal")]
        self.kq = None
        self.fds = []
        self._arm()

    def _arm(self):
        self.close()
        try:
            import select
            if not hasattr(select, "kqueue"):
                return
            self.kq = select.kqueue()
            events = []
            for path in self.files:
                try:
                    fd = os.open(path, os.O_RDONLY)
                except OSError:
                    continue
                self.fds.append(fd)
                events.append(select.kevent(fd, select.KQ_FILTER_VNODE,
                                            select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                            select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND |
                                            select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME))
            self.kq.control(events, 0)
        except OSError:
            self.close()

    def wait(self, timeout, stopped=lambda: False):
        deadline = time.monotonic() + timeout
        if self.kq is None:
            while time.monotonic() < deadline and not stopped():
                time.sleep(0.1)
            return
        import select
        while not stopped():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            events = self.kq.control(None, 8, min(remaining, 0.5))
            if events:
                if any(ev.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME) for ev in events):
                    self._arm()  # the WAL was recreated after a checkpoint
                return
        if len(self.fds) < len(self.files):
            self._arm()  # pick up a WAL that did not exist at start

    def close(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []
        if self.kq is not None:
            self.kq.close()
            self.kq = None


# Persistent watcher state -------------------------------------------------------

def _load_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path, data):
    Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.atomic_json(Path(path), data)


# Feed and digest --------------------------------------------------------------

def append_feed(store, project, feed_path, state):
    """Append new delivery events (with a short excerpt) to the history feed."""
    cursor = int(state.get("feed_rowid", 0))
    with closing(sqlite3.connect(store.path, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""SELECT e.rowid AS rowid,e.at,e.event,m.id,m.sender,m.recipient,
            m.reply_to,m.key,m.body FROM delivery_events e JOIN messages m ON m.id=e.message_id
            WHERE e.rowid>? AND m.project=? ORDER BY e.rowid LIMIT 1000""", (cursor, project)).fetchall()
        top = db.execute("SELECT COALESCE(MAX(rowid),0) FROM delivery_events").fetchone()[0]
        audited = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_forwards'").fetchone()
        forwards = {}
        if audited and rows:
            ids = sorted({row["id"] for row in rows})
            marks = ",".join("?" * len(ids))
            for f in db.execute(f"""SELECT message_id,to_stage,actor FROM stage_forwards
                    WHERE message_id IN ({marks})""", ids):
                forwards[(f[0], f[1])] = f[2]
    if rows:
        feed_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(feed_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as out:
            for row in rows:
                if row["event"] not in {"enqueued", "routed", "acknowledged"}:
                    continue
                first_line = (row["body"] or "").strip().splitlines()[0] if (row["body"] or "").strip() else ""
                kind = ("reply" if row["reply_to"] else
                        "notice" if (row["key"] or "").startswith("mhb-orchestrator-") else "request")
                entry = {"at": row["at"], "event": row["event"], "id": row["id"],
                         "sender": row["sender"], "recipient": row["recipient"],
                         "kind": kind, "excerpt": first_line[:FEED_EXCERPT]}
                if row["event"] == "routed":
                    entry["by"] = forwards.get((row["id"], "inbox"), "bridge chat")
                out.write(json.dumps(entry) + "\n")
        state["feed_rowid"] = rows[-1]["rowid"]
    elif top < cursor:
        state["feed_rowid"] = 0  # a replaced database restarts the feed
    return len(rows)


def _per_message(entries):
    """One digest line per message: its latest state, first excerpt and time."""
    merged = {}
    for item in entries:
        current = merged.setdefault(item.get("id"), dict(item))
        current.update({k: v for k, v in item.items() if v not in (None, "")})
        current["state"] = {"enqueued": "sent", "routed": "on its way",
                            "acknowledged": "handled"}.get(item.get("event"), item.get("event"))
    return sorted(merged.values(), key=lambda item: item.get("at", 0))


def read_feed(feed_path, since):
    entries = []
    try:
        with open(feed_path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("at", 0) > since:
                    entries.append(item)
    except OSError:
        pass
    return entries


def digest_path(root, project, harness):
    return Path(root) / "pickup" / f"digest-{project}-{harness}.json"


def schedule_digests(root, project, harnesses, feed_path, state, *, clock=time.time):
    """Mark a digest ready for each harness whose digest is on and has news.

    The ready marker is read by that harness's bridge listener or nudge. A
    digest never opens a chat; a cold bridge chat sees it when it is opened.
    """
    now = clock()
    ready = []
    for harness in harnesses:
        opts = pickup.settings(root, project, harness)
        marks = state.setdefault("digest", {}).setdefault(harness, {})
        last = float(marks.get("scheduled_at", 0) or 0)
        if not last:
            marks["scheduled_at"] = now
            marks.setdefault("shown_until", now)
            continue
        if opts["digest"] != "on" or now - last < opts["digest_minutes"] * 60:
            continue
        marks["scheduled_at"] = now
        news = _per_message([item for item in read_feed(feed_path, float(marks.get("shown_until", 0)))
                             if harness in (item.get("sender"), item.get("recipient"))])
        if not news:
            continue
        seq = int(marks.get("seq", 0)) + 1
        marks["seq"] = seq
        _save_state(digest_path(root, project, harness),
                    {"project": project, "harness": harness, "seq": seq, "ready_at": now,
                     "count": len(news), "since": float(marks.get("shown_until", 0))})
        ready.append({"harness": harness, "seq": seq, "count": len(news)})
    return ready


def take_digest(root, project, harness, *, all_since_last=False, clock=time.time):
    """Return feed entries for a bridge chat and mark them shown."""
    paths_ = paths(root, project)
    with _state_lock(root, project):
        state = _load_state(paths_["state"])
        marks = state.setdefault("digest", {}).setdefault(harness, {})
        since = float(marks.get("shown_until", 0))
        entries = _per_message([item for item in read_feed(paths_["feed"], since)
                                if harness in (item.get("sender"), item.get("recipient"))])
        now = clock()
        marks["shown_until"] = now
        _save_state(paths_["state"], state)
    opts = pickup.settings(root, project, harness)
    footer = ("Reply 'stop digests' to pause them." if opts["digest"] == "on"
              else "Digests are off. Turn the 30-minute digests back on?")
    return {"project": project, "harness": harness, "since": since, "until": now,
            "count": len(entries), "entries": entries, "footer": footer}


def digest_command(root, project, harness, action="show"):
    """Bridge-chat digest control: show what is new, or switch digests on or off."""
    if action == "show":
        return take_digest(root, project, harness)
    if action not in ("on", "off"):
        raise ValueError("action must be show, on or off")
    config.set_project_option(root, project, harness, "digest", action)
    note = ("Digests paused. Ask for a digest at any time to see what arrived."
            if action == "off" else "30-minute digests are on again.")
    return {"project": project, "harness": harness, "digest": action, "note": note}


class _state_lock:
    """Serialize watcher-state writes between the watcher and a bridge chat command."""

    def __init__(self, root, project):
        self.path = Path(root) / "watch" / f"{project}-state.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        if os.name != "nt":
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.handle.close()


# Alerts ---------------------------------------------------------------------

def _alert_command():
    try:
        commands = wake._read_commands()
    except wake.WakeError:
        return None
    argv = commands.get("alert") if isinstance(commands, dict) else None
    if isinstance(argv, list) and argv and all(isinstance(part, str) for part in argv):
        return argv
    return None


def _alert_record(value):
    return value if isinstance(value, dict) else {"first_at": float(value or 0), "escalated": True}


def raise_alert(project, key, text, state, *, harness=None, message_id=None, run=None,
                root=None, clock=time.time, external_text=None):
    """Open one alert episode per stuck message.

    The bridge chat hears first: the alert goes into the history feed and a
    digest is marked ready at once (even with digests off). The configured
    alert command (for example a Telegram relay) is used only by
    ``escalate_alerts`` if the message stays stuck past the escalation delay.
    With no ``root`` (no bridge to tell), the alert command runs at once.
    """
    alerts = state.setdefault("alerts", {})
    if key in alerts:
        return False
    # Text for an outside channel never carries a chat title; callers that put
    # a title in ``text`` pass a title-free ``external_text``.
    record = {"first_at": clock(), "text": text,
              "external_text": external_text or _generic_alert(harness, message_id),
              "harness": harness, "id": message_id, "escalated": root is None}
    if root is None:
        alerts[key] = record
        _run_alert_command(project, record["external_text"], run)
        return True
    if harness:
        marks = state.setdefault("digest", {}).setdefault(harness, {})
        seq = int(marks.get("seq", 0)) + 1
        marks["seq"] = seq
        marks["nudge_seq"] = seq
        marks.pop("nudge_tried_at", None)
        _save_state(digest_path(root, project, harness),
                    {"project": project, "harness": harness, "seq": seq, "ready_at": clock(),
                     "count": 1, "alert": True})
    feed = paths(root, project)["feed"]
    feed.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(feed, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as out:
        out.write(json.dumps({"at": clock(), "event": "alert", "id": f"alert-{key}", "kind": "alert",
                              "sender": "bridge", "recipient": harness, "excerpt": text}) + "\n")
    alerts[key] = record  # only once the bridge chat has been told; a failed write retries next pass
    return True


def _generic_alert(harness, message_id):
    who = f"A {harness} message" if harness else "A message"
    ref = f" ({message_id[:8]})" if message_id else ""
    return f"{who}{ref} is stuck; see the bridge chat for the fix."


def _run_alert_command(project, text, run=None):
    argv = _alert_command()
    run = run or subprocess.run
    if not argv:
        return False
    try:
        run([part.format_map({"project": project, "text": text}) for part in argv],
            capture_output=True, timeout=20, check=False, shell=False)
        return True
    except (OSError, subprocess.SubprocessError, KeyError, ValueError):
        return False


def escalate_alerts(root, project, state, log, *, run=None, clock=time.time):
    """Send a still-open alert through the alert command once, after its delay."""
    for key, value in state.get("alerts", {}).items():
        record = _alert_record(value)
        if record.get("escalated"):
            continue
        harness = record.get("harness") or "codex"
        delay = pickup.settings(root, project, harness)["alert_escalate_minutes"] * 60
        if clock() - float(record.get("first_at", 0)) < delay:
            continue
        minutes = int((clock() - float(record["first_at"])) // 60)
        text = record.get("external_text") or _generic_alert(record.get("harness"), record.get("id"))
        _run_alert_command(project, f"{text} (still waiting after {minutes} min)", run)
        record["escalated"] = True
        state["alerts"][key] = record
        log({"event": "alert-escalated", "key": key})


def clear_alert(key, state):
    state.setdefault("alerts", {}).pop(key, None)


def _clear_resolved_alerts(store, project, state):
    for key, value in list(state.get("alerts", {}).items()):
        record = _alert_record(value)
        message_id, harness = record.get("id"), record.get("harness")
        if record.get("expires_at") and time.time() > float(record["expires_at"]):
            clear_alert(key, state)
            continue
        if not message_id:
            continue
        trace = store.trace(project, message_id)
        if not trace or trace["stage"] == "done" or harness not in (trace["recipient"], trace["sender"]):
            clear_alert(key, state)


# Main loop --------------------------------------------------------------------

def pull_harnesses(cfg, project):
    return [name for name in sorted(wake.HARNESS_NAMES) if pickup.is_pull(cfg.root, project, name)]


def run_pass(cfg, store, project, state, *, log):
    """One watcher pass. Errors in one part never stop the others."""
    harnesses = pull_harnesses(cfg, project)

    def guarded(part, call, *args):
        try:
            return call(*args)
        except Exception as exc:  # each part fails alone; the pass goes on
            log({"event": f"{part}-held", "reason": f"{type(exc).__name__}: {exc}"})
            return None

    for harness in harnesses:
        moved = guarded("forward", store.forward_pull, project, harness) or []
        for item in moved:
            log({"event": "forwarded", "harness": harness, **item})
    if harnesses:
        try:
            seen = state.setdefault("_dispatch_seen", {})
            reports = wake.cycle(project, pickup_mode="pull", cfg=cfg, store=store)
            current = {}
            for report in reports:
                key = f"{report.get('harness')}/{report.get('role')}"
                signature = json.dumps({k: v for k, v in report.items() if k != "at"}, sort_keys=True)
                current[key] = signature
                if seen.get(key) != signature:  # log changes only, not a steady state every pass
                    log({"event": "dispatch", **report})
            state["_dispatch_seen"] = current
        except Exception as exc:
            log({"event": "dispatch-held", "reason": f"{type(exc).__name__}: {exc}"})
    feed = paths(cfg.root, project)["feed"]
    with _state_lock(cfg.root, project):
        current = _load_state(paths(cfg.root, project)["state"])
        current.setdefault("alerts", state.get("alerts", {}))
        current["_dispatch_seen"] = state.get("_dispatch_seen", {})
        state.clear()
        state.update(current)
        try:
            guarded("feed", append_feed, store, project, feed, state)
            for ready in guarded("digest", schedule_digests, cfg.root, project, harnesses, feed, state) or []:
                log({"event": "digest-ready", **ready})
                state["digest"][ready["harness"]]["nudge_seq"] = ready["seq"]
            guarded("alert-clear", _clear_resolved_alerts, store, project, state)
            guarded("listener-check", _check_listeners, cfg, store, project, harnesses, state, log)
            guarded("alert", _alert_pickup_events, cfg, store, project, state, log)
            guarded("alert-escalate", escalate_alerts, cfg.root, project, state, log)
            guarded("digest-nudge", _nudge_digests, store, project, harnesses, state, log)
        finally:
            _save_state(paths(cfg.root, project)["state"],
                        {k: v for k, v in state.items() if not k.startswith("_")})


DIGEST_RETRY_SECONDS = 60.0


def _nudge_digests(store, project, harnesses, state, log):
    """Tell each bridge chat that a digest is ready, once, without opening it."""
    now = time.time()
    for harness in harnesses:
        marks = state.get("digest", {}).get(harness, {})
        seq = marks.get("nudge_seq")
        port = pickup.adapter(harness)
        if not seq or port is None or port.self_listening:
            marks.pop("nudge_seq", None)  # a listening bridge chat reads the ready marker itself
            continue
        if now - float(marks.get("nudge_tried_at", 0)) < DIGEST_RETRY_SECONDS:
            continue
        marks["nudge_tried_at"] = now
        session = store.roles(project, harness).get("inbox")
        if not session:
            continue
        try:
            sent = port.digest_nudge(project, session, seq, marks.get("nudged_seq"))
        except Exception as exc:
            log({"event": "digest-nudge-held", "harness": harness, "seq": seq, "reason": str(exc)})
            continue
        if sent:
            marks["nudged_seq"] = seq
            marks.pop("nudge_seq", None)
            log({"event": "digest-nudged", "harness": harness, "seq": seq})


def _check_listeners(cfg, store, project, harnesses, state, log):
    for harness in harnesses:
        port = pickup.adapter(harness)
        if port is None or not port.self_listening:
            continue
        session = store.roles(project, harness).get("coordinator")
        pending = store.pending_details(project, harness, "coordinator", session) if session else []
        listener = pickup.listener_health(cfg.root, project, harness, stale_seconds=LISTENER_STALE_SECONDS)
        if not session or (listener["attached"] and listener.get("session") == session):
            continue
        grace = pickup.settings(cfg.root, project, harness)["fallback_grace_seconds"]
        for item in pending:
            if time.time() - float(item.get("updated") or 0) < grace:
                continue
            # One alert per stuck message, never one per pass.
            if raise_alert(project, f"stuck-{harness}-{item['id']}", (
                    f"A {harness} message from {item['sender']} ({item['id'][:8]}) is waiting because the "
                    f"{harness} coordinator listener is not attached. Fix: retype take coordinator in "
                    f"the {_chat_label(harness, 'coordinator', session)}."), state,
                    harness=harness, message_id=item["id"], root=cfg.root,
                    external_text=(f"A {harness} message ({item['id'][:8]}) is waiting because the "
                                   f"{harness} coordinator listener is not attached. Fix: retype take "
                                   f"coordinator in the {_chat_label(harness, 'coordinator', session, with_title=False)}.")):
                log({"event": "fallback-report", "harness": harness, "id": item["id"]})


ALERT_EVENTS = {
    "held-paused": ("its last turn was interrupted, so its queue is paused", "click Resume in the {chat}"),
    "fallback-undelivered": ("it was opened once and still did not take the hint", "open the {chat}"),
    "fallback-unavailable": ("the bridge cannot read the chat's state, so it will not open it",
                             "open the {chat} and check it is idle"),
}


def _chat_label(harness, role, session, *, with_title=True):
    """The chat as the user knows it. Titles stay local: an outside alert gets the plain label."""
    label = f"{harness} {role} chat"
    if not with_title:
        return label
    port = pickup.adapter(harness)
    name = port.chat_name(session) if port is not None and session else None
    return f'{label} "{name}"' if name else label


def _alert_loop_cap(cfg, project, event, state, log):
    """One alert per pair per window when wake-started messages hit the loop cap."""
    try:
        detail = json.loads(event.get("detail") or "{}")
    except json.JSONDecodeError:
        detail = {}
    sender, recipient = event["harness"], str(detail.get("recipient") or "a peer")
    window = max(60, int(detail.get("window_seconds") or 1800))
    limit = detail.get("limit")
    pair = "-".join(sorted((sender, recipient)))
    key = f"loop-{pair}"  # one open alert per pair; it clears when its window has passed
    text = (f"Messages between {pair.replace('-', ' and ')} reached the loop cap ({limit} wake-started "
            f"messages in {window // 60} minutes). The bridge refuses more from bridge-started turns "
            "until the window passes. Fix: look at the exchange in both coordinator chats; a turn you "
            "start there can still send, or raise loop_cap_messages.")
    if raise_alert(project, key, text, state, harness=sender, root=cfg.root, external_text=text):
        state["alerts"][key]["expires_at"] = event["at"] + window
        log({"event": "alert", "reason": "loop-cap", "harness": sender, "pair": pair})


def _alert_pickup_events(cfg, store, project, state, log):
    """Turn new pull-mode problems recorded by the dispatcher into one alert each."""
    since = float(state.get("pickup_events_at", 0) or 0)
    try:
        events = wake.pickup_events(store, project, since)
    except (OSError, sqlite3.Error):
        return
    for event in events:
        state["pickup_events_at"] = max(float(state.get("pickup_events_at", 0) or 0), event["at"])
        if event["event"] == "loop-cap":
            _alert_loop_cap(cfg, project, event, state, log)
            continue
        reason = ALERT_EVENTS.get(event["event"])
        if not reason:
            continue
        why, fix = reason

        def text(chat):
            return (f"A {event['harness']} message ({event['notification_id'][:8]}) is waiting for the "
                    f"{chat}: {why}. Fix: {fix.format(chat=chat)}.")
        # Keyed by message, so a paused queue and a later undelivered open stay one alert.
        if raise_alert(project, f"stuck-{event['harness']}-{event['notification_id']}",
                       text(_chat_label(event["harness"], event["role"], event["session"])), state,
                       harness=event["harness"], message_id=event["notification_id"], root=cfg.root,
                       external_text=text(_chat_label(event["harness"], event["role"], event["session"],
                                                      with_title=False))):
            log({"event": "alert", "reason": event["event"], "harness": event["harness"],
                 "id": event["notification_id"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    parser.add_argument("--status", action="store_true", help="print watcher health and exit")
    parser.add_argument("--ensure", action="store_true", help="start the watcher if it is not running")
    args = parser.parse_args(argv)
    cfg = config.load()
    project = cfg.require(args.project)
    if args.status:
        print(json.dumps(health(cfg.root, project)), flush=True)
        return 0
    if args.ensure:
        print(json.dumps(ensure(cfg.root, project)), flush=True)
        return 0
    lock = Lock(paths(cfg.root, project)["lock"])
    for _ in range(10):
        if lock.acquire():
            break
        time.sleep(0.1)
    else:
        print(json.dumps({"event": "already-running", "project": project}), flush=True)
        return 0  # a second start is a no-op, not a crash loop
    store = config.open_store(cfg)
    with closing(sqlite3.connect(store.path, timeout=10)) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
            print(json.dumps({"event": "refused", "reason": "store schema is newer than this watcher"}), flush=True)
            return 1
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    token = secrets.token_hex(8)
    started = time.time()
    heartbeat = paths(cfg.root, project)["heartbeat"]

    def log(entry):
        print(json.dumps({"at": round(time.time(), 2), "project": project, **entry}, sort_keys=True), flush=True)

    watch = StoreWatch(cfg.database)
    state = {}
    log({"event": "started", "pid": os.getpid(), "version": _version(), "token": token})
    try:
        while not stopped:
            _save_state(heartbeat, {"project": project, "pid": os.getpid(), "version": _version(),
                                    "token": token, "started_at": started, "beat_at": time.time()})
            try:
                run_pass(cfg, store, project, state, log=log)
            except Exception as exc:  # one bad pass must never end the watcher
                log({"event": "pass-held", "reason": f"{type(exc).__name__}: {exc}"})
            if args.once:
                break
            watch.wait(POLL_SECONDS, lambda: stopped)
    finally:
        watch.close()
        try:
            _save_state(heartbeat, {"project": project, "pid": os.getpid(), "token": token,
                                    "started_at": started, "beat_at": time.time(), "stopped": True})
        except OSError:
            pass
        lock.release()
        log({"event": "stopped"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
