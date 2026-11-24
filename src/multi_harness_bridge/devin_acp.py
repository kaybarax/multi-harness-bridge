"""Bounded Devin ACP wake and per-session mailbox MCP tool."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid

from . import config
from .wake import STALE_WAKE_SECONDS, WakeDeferred, WakeError, WakeRejected

NATIVE_DB = Path.home() / ".local" / "share" / "devin" / "cli" / "sessions.db"
WAKE_TIMEOUT = 20.0
ROUTED_COORDINATOR_TIMEOUT = 45.0
NATIVE_WAKE_SECONDS = 2 * STALE_WAKE_SECONDS
TURN_ACTIVITY_SECONDS = 300.0
SESSION_LIST_TIMEOUT = 2.0
WAKE_PREFIX = "Multi Harness Bridge native wake. "
PICKUP_TOOL = "pickup_wake"
FINISH_TOOL = "finish_wake"


class ACPError(RuntimeError):
    pass


class ACPOperationRejected(ACPError):
    def __init__(self, error):
        super().__init__("Devin ACP refused the requested operation")
        data = error.get("data") if isinstance(error, dict) else None
        self.session_locked = (isinstance(data, dict)
                               and data.get("cognition.ai/errorKind") == "session_locked")


def _same_session(left, right):
    prefix = "acp/devin-cli/"
    return isinstance(left, str) and isinstance(right, str) and \
        left.removeprefix(prefix) == right.removeprefix(prefix)


def _json_exact(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_json_exact(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_json_exact(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def _permission_call_id(params):
    call = params.get("toolCall")
    call = call if isinstance(call, dict) else {}
    value = params.get("toolCallId", call.get("toolCallId"))
    return value if isinstance(value, str) and value else None


def _tool_and_args(call, server_name, tool):
    if not isinstance(call, dict):
        return None, None
    raw = call.get("rawInput", call.get("input"))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
    if not isinstance(raw, dict):
        return None, None
    nested_server = raw.get("server_name", raw.get("serverName"))
    nested_tool = raw.get("tool_name", raw.get("toolName", raw.get("name")))
    if nested_server is not None or nested_tool is not None:
        if nested_server != server_name or nested_tool != tool:
            return None, None
        args = raw.get("arguments", raw.get("args", raw.get("input")))
        return nested_tool, args if isinstance(args, dict) else None
    title = call.get("title")
    if title == f"Calling {tool} from {server_name}":
        return tool, raw
    name = call.get("name", call.get("toolName", call.get("tool_name")))
    if name in {None, "mcp_call_tool", tool, f"{server_name}_{tool}",
                f"{server_name}__{tool}", f"mcp__{server_name}__{tool}"}:
        return tool, raw
    return None, None


def _call_conflicts(call, server_name, expected_tool):
    """Return true only when supplied call identity contradicts our target."""
    raw = call.get("rawInput", call.get("input")) if isinstance(call, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return True
    if isinstance(raw, dict):
        server = raw.get("server_name", raw.get("serverName"))
        tool = raw.get("tool_name", raw.get("toolName", raw.get("name")))
        if server is not None and server != server_name:
            return True
        if tool is not None and tool != expected_tool:
            return True
    title = call.get("title") if isinstance(call, dict) else None
    if title is not None and title != f"Calling {expected_tool} from {server_name}":
        return True
    name = call.get("name", call.get("toolName", call.get("tool_name"))) if isinstance(call, dict) else None
    if name is not None and name not in {"mcp_call_tool", expected_tool, f"{server_name}_{expected_tool}",
            f"{server_name}__{expected_tool}", f"mcp__{server_name}__{expected_tool}"}:
        return True
    return False


def _native_message_time(message, fallback):
    metadata = message.get("metadata")
    created = metadata.get("created_at") if isinstance(metadata, dict) else None
    if isinstance(created, str):
        try:
            timestamp = datetime.fromisoformat(created)
            if timestamp.tzinfo is not None:
                return timestamp.timestamp()
        except ValueError:
            pass
    return fallback


def _native_messages(session, workspace, since, native_db, *, require_readable=False):
    db_path = Path(native_db or os.environ.get("MHB_DEVIN_SESSIONS_DB") or NATIVE_DB).expanduser().resolve()
    expected_native_id = session.removeprefix("acp/devin-cli/")
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            columns = {row[1] for row in db.execute("PRAGMA table_info(message_nodes)")}
            required = {"row_id", "session_id", "chat_message", "created_at"}
            if not required <= columns:
                if require_readable:
                    raise WakeDeferred("Devin session activity is unavailable before prompt submission")
                return []
            sessions = db.execute("SELECT id,working_directory FROM sessions WHERE id IN (?,?)",
                                  (expected_native_id, "acp/devin-cli/" + expected_native_id)).fetchall()
            if len(sessions) != 1 or not isinstance(sessions[0][1], str):
                if require_readable:
                    raise WakeDeferred("Devin session activity cannot be matched to the bound chat")
                return []
            native_id, directory = sessions[0]
            if Path(directory).resolve() != Path(workspace).resolve():
                if require_readable:
                    raise WakeDeferred("Devin session activity does not match the bound workspace")
                return []
            rows = db.execute("""SELECT row_id,chat_message,created_at FROM message_nodes
                WHERE session_id=? AND created_at>=? ORDER BY row_id""",
                (native_id, math.floor(since))).fetchall()
    except (OSError, sqlite3.Error):
        if require_readable:
            raise WakeDeferred("Devin session activity is unreadable before prompt submission") from None
        return []
    messages = []
    for row in rows:
        try:
            message = json.loads(row["chat_message"])
        except (TypeError, json.JSONDecodeError):
            if require_readable:
                raise WakeDeferred("Devin session activity contains an unreadable recent message") from None
            continue
        if isinstance(message, dict) and _native_message_time(message, row["created_at"]) >= since:
            messages.append((row["row_id"], message))
    return messages


def _session_list_state(executable, session, workspace):
    """Read the installed CLI's per-session daemon state for this exact workspace."""
    try:
        result = subprocess.run([executable, "list", "--format", "json", "--process-model", "daemon"],
                                cwd=workspace, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=SESSION_LIST_TIMEOUT, check=False)
        entries = json.loads(result.stdout) if result.returncode == 0 else None
        if not isinstance(entries, list):
            return None
        matches = [entry for entry in entries if isinstance(entry, dict)
                   and _same_session(entry.get("id"), session)]
        if len(matches) != 1:
            return None
        observed = matches[0].get("working_directory")
        if isinstance(observed, str) and Path(observed).resolve() == Path(workspace).resolve():
            return matches[0].get("state")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return None


def _require_idle_turn(executable, session, workspace, native_db):
    state = _session_list_state(executable, session, workspace)
    if state in ("running", "waiting", "busy"):
        raise WakeDeferred(f"Devin native turn is active ({state})")
    # An attached session can be idle. Replay metadata and system context by
    # themselves do not establish that the current turn has finished.
    since = time.time() - TURN_ACTIVITY_SECONDS
    messages = _native_messages(session, workspace, since, native_db, require_readable=True)
    latest = next((message for _, message in reversed(messages)
                   if message.get("role") in {"user", "assistant", "tool"}), None)
    if latest is None:
        return
    metadata = latest.get("metadata") or {}
    if (latest.get("role") == "assistant" and not latest.get("tool_calls")
            and isinstance(metadata, dict) and metadata.get("finish_reason") == "stop"):
        return
    raise WakeDeferred("Devin native transcript has a recent unfinished turn")


def _native_call_args(session, workspace, server_name, tool_call_id, tool, *,
                      previous_call=None, native_db=None):
    """Read one pending native call and, when required, prove the prior result was recorded."""
    messages = _native_messages(session, workspace, time.time() - 120, native_db)
    completed = {}
    for row_id, message in messages:
        if message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str):
            completed.setdefault(message["tool_call_id"], row_id)
    if tool_call_id in completed or previous_call is not None and previous_call not in completed:
        return None
    matched = None
    for row_id, message in messages:
        if previous_call is not None and row_id <= completed[previous_call]:
            continue
        if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
            continue
        for call in message["tool_calls"]:
            if not isinstance(call, dict) or call.get("name") != "mcp_call_tool":
                continue
            if call.get("id") != tool_call_id:
                continue
            args = call.get("arguments")
            if not isinstance(args, dict) or args.get("server_name") != server_name or args.get("tool_name") != tool:
                return None
            native_args = args.get("arguments")
            if not isinstance(native_args, dict) or matched is not None and not _json_exact(matched, native_args):
                return None
            matched = native_args
    return matched


def _native_completion(session, workspace, server_name, prompt, notification_id, since, native_db,
                       *, user_row_id=None):
    """Recognize only results for this wake's exact tool call in its recorded native turn."""
    active = False
    calls = {}
    finished = {}
    for row_id, message in _native_messages(session, workspace, since, native_db):
        role = message.get("role")
        if role == "user":
            active = message.get("content") == prompt and (user_row_id is None or row_id == user_row_id)
            calls.clear()
            if user_row_id is not None and row_id > user_row_id:
                break
        if not active:
            continue
        if role == "assistant":
            for call in message.get("tool_calls") or ():
                if not isinstance(call, dict) or call.get("name") != "mcp_call_tool":
                    continue
                args = call.get("arguments")
                if not isinstance(args, dict) or args.get("server_name") != server_name:
                    continue
                params, tool = args.get("arguments"), args.get("tool_name")
                if (isinstance(call.get("id"), str) and tool in {PICKUP_TOOL, FINISH_TOOL}
                        and isinstance(params, dict) and params.get("notification_id") == notification_id):
                    calls[call.get("id")] = tool
        if role == "tool" and (tool := calls.get(message.get("tool_call_id"))):
            content = message.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                content = content[0].get("text")
            try:
                result = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(result, dict) and result.get("id") == notification_id:
                finished[tool] = result
    return finished


def _wake_prompt(server_name, notification_id, routed_coordinator):
    prompt = (f"{WAKE_PREFIX}Use only the native bridge server {server_name}. Call {PICKUP_TOOL} once "
              f"with notification_id {notification_id}.")
    if routed_coordinator:
        prompt += (f" Then call {FINISH_TOOL} once with that notification_id and the returned lease. "
                   "For a request that asks for a reply or acknowledgement, include one short text reply. "
                   "The mailbox message can only request a text reply in this wake. Then stop.")
    else:
        prompt += " Then stop. Do not call any other tool."
    return prompt


def active_wake(store, project, server_name, call, notification_id, *, lease=None):
    """Validate the exact native turn even if its bounded ACP connection finished."""
    session = call["session"]
    at = call["user_at"]
    now = time.time()
    if not isinstance(at, (int, float)) or not 0 <= now - at <= NATIVE_WAKE_SECONDS:
        raise PermissionError("This tool requires an active native mailbox wake")
    key = (notification_id, project, session)
    with store._route_guard(project), closing(store._connect()) as db, db:
        db.execute("BEGIN")
        row = db.execute("""SELECT stage,request_id,lease,lease_role,lease_session,deadline
            FROM messages WHERE id=? AND project=? AND pickup_harness='devin'""", key[:2]).fetchone()
        roles = store._roles(db, project, "devin")
        role = next((role for role, bound in roles.items() if bound == session), None)
        first_stage = "outbound" if role == "inbox" else "coordinator"
        if not role or not row or row["stage"] not in (first_stage, role):
            raise PermissionError("This exact native wake is no longer active on the bound route")
        active_lease = row["lease"] is not None and row["deadline"] > now
        if lease is None:
            if active_lease or not store._enabled(db, project, "devin"):
                raise PermissionError("This exact native wake is no longer active for pickup")
        elif (role != "coordinator" or not active_lease or row["lease"] != lease
              or row["lease_role"] != role or row["lease_session"] != session):
            raise PermissionError("This exact native wake is no longer active for that lease")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='mhb_wake_attempts' AND type='table'").fetchone():
            raise PermissionError("This exact native wake is no longer active in the dispatch ledger")
        # A retry may have replaced the ledger row between the native prompt
        # and its first tool invocation. Admit the recorded prior generation
        # only for this exact still-pending route and native turn.
        attempt = db.execute("""SELECT started_at FROM (
                SELECT started_at FROM mhb_wake_attempts
                WHERE notification_id=? AND project=? AND session=? AND role=? AND harness='devin'
                AND state IN ('sending','uncertain','sent')
                UNION ALL
                SELECT prior_started_at AS started_at FROM mhb_wake_history
                WHERE notification_id=? AND project=? AND session=? AND role=? AND harness='devin'
                AND action='automatic-retry' AND prior_state IN ('sending','uncertain','sent')
            ) WHERE started_at BETWEEN ? AND ? ORDER BY started_at DESC LIMIT 1""",
            (*key, role, *key, role, at - STALE_WAKE_SECONDS, at)).fetchone()
    if not attempt:
        raise PermissionError("This exact native wake is no longer active in the dispatch ledger")
    routed_coordinator = role == "coordinator" and row["request_id"] is not None
    prompt = _wake_prompt(server_name, notification_id, routed_coordinator)
    if call["user_prompt"] != prompt:
        raise PermissionError("Current native turn does not match this wake")
    return role, attempt[0], prompt


class ACPClient:
    """JSON-RPC pipe client; a reader thread keeps Windows pipes selectable."""
    def __init__(self, process, timeout=WAKE_TIMEOUT):
        self.process = process
        self.events = queue.Queue()
        self.deadline = time.monotonic() + timeout
        self.next_id = 1
        self.pending_calls = {}
        self.reader = threading.Thread(target=self._read, name="mhb-devin-acp-reader", daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in iter(self.process.stdout.readline, b""):
                try:
                    self.events.put(json.loads(line))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.events.put({"_invalid": True})
        finally:
            self.events.put(None)

    def _write(self, value):
        try:
            self.process.stdin.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
            self.process.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise ACPError(f"Devin ACP pipe failed ({type(exc).__name__})") from None

    def call(self, method, params, *, server_request=None, notification=None):
        call_id = self.next_id
        self.next_id += 1
        self._write({"jsonrpc": "2.0", "id": call_id, "method": method, "params": params})
        while time.monotonic() < self.deadline:
            try:
                event = self.events.get(timeout=max(0, self.deadline - time.monotonic()))
            except queue.Empty:
                break
            if event is None:
                raise EOFError("Devin ACP exited before replying")
            if not isinstance(event, dict) or event.get("_invalid"):
                raise ACPError("Devin ACP returned malformed JSON")
            if event.get("method") == "session/update":
                if notification:
                    notification(event.get("params", {}))
                continue
            if event.get("method") and "id" in event:
                request_id = event["id"]
                if event["method"] == "session/request_permission":
                    try:
                        result = server_request(event) if server_request else {"outcome": {"outcome": "cancelled"}}
                        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
                    except Exception:
                        self._write({"jsonrpc": "2.0", "id": request_id,
                                     "result": {"outcome": {"outcome": "cancelled"}}})
                else:
                    self._write({"jsonrpc": "2.0", "id": request_id, "error": {
                        "code": -32601, "message": "Unsupported by bounded Devin wake"}})
                continue
            if event.get("id") == call_id:
                if "error" in event:
                    raise ACPOperationRejected(event["error"])
                result = event.get("result")
                if not isinstance(result, dict):
                    raise ACPError("Devin ACP returned no result object")
                return result
        raise TimeoutError("Devin ACP request timed out")

    def close(self):
        self.reader.join(timeout=0.25)


def _remember_tool_update(pending, params, expected_session):
    update = params.get("update")
    session_id = params.get("sessionId")
    if not isinstance(update, dict) or not _same_session(session_id, expected_session):
        return
    kind = update.get("sessionUpdate", update.get("type"))
    if kind not in {"tool_call", "tool_call_update"}:
        return
    call_id = update.get("toolCallId")
    if not isinstance(call_id, str) or not call_id:
        return
    current = pending.get(call_id)
    if kind == "tool_call_update" and current:
        current["call"].update({k: v for k, v in update.items() if k not in {"sessionUpdate", "type"}})
        current["status"] = update.get("status", current.get("status"))
        if current["status"] in {"completed", "failed", "cancelled", "error"}:
            pending.pop(call_id, None)
    else:
        pending[call_id] = {"session_id": session_id, "call": dict(update),
                            "status": update.get("status")}


def _permission_handler(*, server_name, expected_session, workspace, notification_id,
                        pending_calls, state, native_db=None, routed_coordinator=False, submitted_at=None):

    def handle(event):
        if event.get("method") != "session/request_permission":
            return {"error": {"code": -32601, "message": "Unsupported by the bounded wake client"}}
        params = event.get("params")
        if not isinstance(params, dict):
            return {"outcome": {"outcome": "cancelled"}}
        call_id = _permission_call_id(params)
        if not call_id or ("sessionId" in params and
                           not _same_session(params.get("sessionId"), expected_session)):
            return {"outcome": {"outcome": "cancelled"}}
        tool = PICKUP_TOOL
        args = _native_call_args(expected_session, workspace, server_name, call_id, tool, native_db=native_db)
        if args is None and routed_coordinator:
            prior = state.get("pickup_call")
            observed = submitted_at is not None and _native_completion(
                expected_session, workspace, server_name, _wake_prompt(server_name, notification_id, True),
                notification_id, submitted_at, native_db,
            )
            if prior or observed and PICKUP_TOOL in observed:
                tool = FINISH_TOOL
                args = _native_call_args(expected_session, workspace, server_name, call_id, tool,
                                         previous_call=prior, native_db=native_db)
        if tool in state.get("tools", ()):
            return {"outcome": {"outcome": "cancelled"}}
        if args is None or args.get("notification_id") != notification_id:
            return {"outcome": {"outcome": "cancelled"}}
        if tool == PICKUP_TOOL and set(args) != {"notification_id"}:
            return {"outcome": {"outcome": "cancelled"}}
        if tool == FINISH_TOOL and (set(args) - {"notification_id", "lease", "reply"}
                or not isinstance(args.get("lease"), str) or not args["lease"]
                or args.get("reply") is not None and not isinstance(args["reply"], str)):
            return {"outcome": {"outcome": "cancelled"}}
        pending = pending_calls.pop(call_id, None)
        context = pending.get("call") if pending and _same_session(
            pending.get("session_id"), expected_session) else None
        call = context
        if call is None:
            envelope = params.get("toolCall")
            if isinstance(envelope, dict) and envelope.get("toolCallId", call_id) == call_id:
                call = {**envelope, "toolCallId": call_id}
        if call is not None:
            if call.get("toolCallId", call_id) != call_id:
                return {"outcome": {"outcome": "cancelled"}}
            if _call_conflicts(call, server_name, tool):
                return {"outcome": {"outcome": "cancelled"}}
            _, supplied_args = _tool_and_args(call, server_name, tool)
            if supplied_args is not None and not _json_exact(supplied_args, args):
                return {"outcome": {"outcome": "cancelled"}}
        options = params.get("options")
        allowed = [item.get("optionId") for item in options if isinstance(item, dict)
                   and item.get("kind") == "allow_once"] if isinstance(options, list) else []
        if len(allowed) != 1 or not isinstance(allowed[0], str) or not allowed[0]:
            return {"outcome": {"outcome": "cancelled"}}
        if tool == PICKUP_TOOL:
            state["pickup_call"] = call_id
        state.setdefault("tools", set()).add(tool)
        state["allowed"] += 1
        return {"outcome": {"outcome": "selected", "optionId": allowed[0]}}
    return handle


def _pickup_notification(store, project, role, session, notification_id):
    try:
        canonical = str(uuid.UUID(notification_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("notification_id must be a canonical UUID") from None
    if canonical != notification_id:
        raise ValueError("notification_id must be a canonical UUID")
    if store.roles(project, "devin").get(role) != session:
        raise PermissionError("This Devin role was rebound")
    if not store.status(project, "devin").get("enabled"):
        raise PermissionError("Devin mailbox pickup is stopped")
    item = store.claim(project, "devin", role, session, message_id=notification_id)
    if not item:
        raise ValueError("The exact notification could not be claimed")
    if item["id"] != notification_id:
        raise RuntimeError("Store returned a different notification ID")
    route = {key: item[key] for key in ("request_id", "stage") if key in item}
    if role == "inbox":
        if not store.finish(project, "devin", role, session, notification_id, item["lease"]):
            raise RuntimeError("Inbox could not durably forward the notification")
        return {"id": notification_id, "forwarded": True, **route}
    receipt = {key: item[key] for key in ("id", "sender", "recipient", "body", "reply_to", "attempt")}
    if "request_id" in route:
        return {**receipt, **route, "lease": item["lease"]}
    if not store.finish(project, "devin", role, session, notification_id, item["lease"]):
        raise RuntimeError("Coordinator could not durably acknowledge the notification")
    return {**receipt, **route, "acknowledged": True}


def _finish_notification(store, project, session, expected_id, receipt, notification_id, lease, reply):
    if (not receipt or notification_id != expected_id or receipt.get("id") != notification_id
            or not lease or receipt.get("lease") != lease or "request_id" not in receipt):
        raise PermissionError("Finish must use this wake's exact notification ID and pickup lease")
    if reply is not None and (receipt["reply_to"] is not None or not isinstance(reply, str) or not reply.strip()):
        raise ValueError("Reply text is only available for an incoming free-text request")
    result = {"id": notification_id, "request_id": receipt["request_id"], "acknowledged": True}
    if reply is None:
        finished = store.finish(project, "devin", "coordinator", session, notification_id, lease)
    else:
        finished = store.finish_and_reply(project, "devin", session, notification_id, lease, reply,
                                          f"wake-reply:{notification_id}")
        result["reply"] = finished
    if not finished:
        raise ValueError("The notification lease is stale or belongs to another coordinator")
    return result


def _start(executable, workspace):
    kwargs = {"cwd": str(workspace), "stdin": subprocess.PIPE,
              "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([executable, "acp", "--process-model", "daemon"], **kwargs)


def _stop_process(process):
    if process.poll() is None:
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
    for stream in (process.stdin, process.stdout):
        try:
            if stream:
                stream.close()
        except OSError:
            pass


def wake(project, role, session, notification_id, workspace, *, before_prompt=None,
         native_db=None, timeout=None):
    """Load one native Devin session and permit only the current delivery's operations."""
    from .devin_tools import server_name

    executable = os.environ.get("MHB_DEVIN_BIN") or shutil.which("devin")
    if not executable:
        raise WakeRejected("Devin Local CLI is unavailable")
    cfg = config.load()
    store = config.open_store(cfg)
    if cfg.require(project) != project or store.roles(project, "devin").get(role) != session:
        raise WakeRejected("Devin role is not the current project binding")
    notification_id = str(uuid.UUID(notification_id))
    item = store.pending(project, "devin", role, session, message_id=notification_id)
    routed_coordinator = role == "coordinator" and item is not None and "request_id" in item
    native_server = server_name(project)
    native_session = session.removeprefix("acp/devin-cli/")
    _require_idle_turn(executable, native_session, workspace, native_db)
    try:
        proc = _start(executable, workspace)
    except OSError as exc:
        raise WakeRejected(f"Devin ACP could not start ({type(exc).__name__})") from None
    if timeout is None:
        timeout = ROUTED_COORDINATOR_TIMEOUT if routed_coordinator else WAKE_TIMEOUT
    client = ACPClient(proc, timeout=timeout)
    try:
        try:
            init = client.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
            if not init.get("agentCapabilities", {}).get("loadSession"):
                raise WakeRejected("Installed Devin ACP cannot load an existing bound session")
            client.call("session/load", {"sessionId": native_session,
                                         "cwd": str(Path(workspace).resolve()),
                                         "mcpServers": []})
        except WakeRejected:
            raise
        except ACPOperationRejected as exc:
            reason = "session_locked" if exc.session_locked else "operation-rejected"
            raise WakeDeferred(f"Devin rejected the bound session before prompt submission ({reason})") from None
        except Exception as exc:
            raise WakeDeferred(f"Devin rejected the bound session before prompt submission ({type(exc).__name__})") from None
        if before_prompt is not None and not before_prompt():
            raise WakeDeferred("Devin wake was stopped or rebound before prompt submission")
        prompt = _wake_prompt(native_server, notification_id, routed_coordinator)
        state, pending = {"allowed": 0}, {}
        _require_idle_turn(executable, native_session, workspace, native_db)
        if time.monotonic() >= client.deadline:
            raise WakeDeferred("Devin ACP deadline expired before prompt submission")
        submitted_at = time.time()
        handler = _permission_handler(server_name=native_server, expected_session=native_session,
                                      workspace=workspace, notification_id=notification_id,
                                      pending_calls=pending, state=state, native_db=native_db,
                                      routed_coordinator=routed_coordinator, submitted_at=submitted_at)
        try:
            result = client.call("session/prompt", {"sessionId": native_session,
                "prompt": [{"type": "text", "text": prompt}]}, server_request=handler,
                notification=lambda params: _remember_tool_update(pending, params, native_session))
        except TimeoutError:
            raise
        except EOFError:
            raise TimeoutError("Devin ACP exited after prompt submission; outcome is uncertain") from None
        except ACPOperationRejected:
            persisted = any(message.get("role") == "user" and message.get("content") == prompt
                            for _, message in _native_messages(native_session, workspace, submitted_at, native_db))
            if not persisted and not state["allowed"]:
                raise WakeDeferred("Devin ACP refused the wake before accepting the exact prompt") from None
            raise WakeError("Devin ACP refused after accepting the exact prompt; delivery is uncertain") from None
        except ACPError:
            raise WakeError("Devin ACP transport failed after wake submission; delivery is uncertain") from None
        if item is not None:
            observed = _native_completion(native_session, workspace, native_server, prompt,
                                          notification_id, submitted_at, native_db)
            expected_tools = {PICKUP_TOOL, FINISH_TOOL} if routed_coordinator else {PICKUP_TOOL}
            prior = item.get("stage", role)
            current = (store.trace(project, notification_id) or {}).get("stage")
            stages = ("outbound", "inbox", "coordinator", "done")
            advanced = current in stages[stages.index(prior) + 1:]
            if expected_tools.issubset(observed) and advanced:
                return result
            if observed or advanced or state["allowed"]:
                raise WakeError("Devin mailbox completion could not be confirmed in the native chat and queue")
            raise WakeRejected("Devin did not complete the exact mailbox pickup")
        if not state["allowed"]:
            raise WakeRejected("Devin did not request the one authorized mailbox notification tool")
        return result
    finally:
        _stop_process(proc)
        client.close()


def rename(session, workspace, title, *, native_db=None, timeout=15.0):
    """Use ACP's advertised rename method, then verify the native sessions DB."""
    if not isinstance(title, str) or not title or len(title) > 80 or any(ord(c) < 32 for c in title):
        raise ValueError("Devin session title must contain 1 to 80 printable characters")
    executable = os.environ.get("MHB_DEVIN_BIN") or shutil.which("devin")
    if not executable:
        raise RuntimeError("Devin Local CLI is unavailable")
    session_id = session.removeprefix("acp/devin-cli/")
    db_path = Path(native_db or os.environ.get("MHB_DEVIN_SESSIONS_DB") or NATIVE_DB).expanduser()
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)) as db:
        before = db.execute("SELECT working_directory,title FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not before or Path(before[0]).resolve() != Path(workspace).resolve():
        raise ValueError("Native Devin session does not match the requested workspace")
    if before[1] == title:
        return title
    process = _start(executable, workspace)
    client = ACPClient(process, timeout=timeout)
    try:
        init = client.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        if not init.get("agentCapabilities", {}).get("_meta", {}).get("cognition.ai/sessionRename"):
            raise RuntimeError("Devin ACP does not advertise native session rename")
        client.call("_cognition.ai/session/rename", {"sessionId": session_id, "title": title})
    finally:
        client.close()
        _stop_process(process)
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)) as db:
        after = db.execute("SELECT working_directory,title FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not after or Path(after[0]).resolve() != Path(workspace).resolve() or after[1] != title:
        raise RuntimeError("Devin rename was not confirmed by the native session database")
    return title
