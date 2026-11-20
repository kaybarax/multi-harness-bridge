"""Bounded Devin ACP wake and per-session mailbox MCP tool."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from . import config
from .store import Store
from .wake import WAKE_PROMPT, WakeDeferred, WakeError, WakeRejected

NATIVE_DB = Path.home() / ".local" / "share" / "devin" / "cli" / "sessions.db"
WAKE_TIMEOUT = 20.0


class ACPError(RuntimeError):
    pass


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


def _tool_and_args(call, server_name):
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
        if nested_server != server_name or nested_tool != "pickup_notification":
            return None, None
        args = raw.get("arguments", raw.get("args", raw.get("input")))
        return nested_tool, args if isinstance(args, dict) else None
    title = call.get("title")
    if title == f"Calling pickup_notification from {server_name}":
        return "pickup_notification", raw
    name = call.get("name", call.get("toolName", call.get("tool_name")))
    if name in {f"{server_name}_pickup_notification", f"{server_name}__pickup_notification",
                f"mcp__{server_name}__pickup_notification"}:
        return "pickup_notification", raw
    return None, None


def _call_conflicts(call, server_name):
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
        if tool is not None and tool != "pickup_notification":
            return True
    title = call.get("title") if isinstance(call, dict) else None
    if title is not None and title != f"Calling pickup_notification from {server_name}":
        return True
    name = call.get("name", call.get("toolName", call.get("tool_name"))) if isinstance(call, dict) else None
    if name is not None and name not in {"mcp_call_tool", "pickup_notification",
            f"{server_name}_pickup_notification", f"{server_name}__pickup_notification",
            f"mcp__{server_name}__pickup_notification"}:
        return True
    return False


def _native_call_matches(session, workspace, server_name, tool_call_id, expected_args,
                         *, native_db=None, now=None):
    """Prove the pending native assistant call behind an ACP permission ID."""
    db_path = Path(native_db or os.environ.get("MHB_DEVIN_SESSIONS_DB") or NATIVE_DB).expanduser()
    now = time.time() if now is None else now
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            columns = {row[1] for row in db.execute("PRAGMA table_info(message_nodes)")}
            required = {"row_id", "session_id", "chat_message", "created_at"}
            if not required <= columns:
                return False
            rows = db.execute("""SELECT session_id,row_id,chat_message FROM message_nodes
                WHERE created_at>=? ORDER BY row_id DESC""", (now - 120,)).fetchall()
            native_sessions = {r["id"]: r["working_directory"] for r in db.execute(
                "SELECT id,working_directory FROM sessions")}
    except (OSError, sqlite3.Error):
        return False
    expected_native_id = session.removeprefix("acp/devin-cli/")
    if native_sessions.get(expected_native_id) is None:
        return False
    try:
        if Path(native_sessions[expected_native_id]).resolve() != Path(workspace).resolve():
            return False
    except OSError:
        return False
    completed = set()
    messages = []
    for row in rows:
        try:
            message = json.loads(row["chat_message"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str):
            completed.add((row["session_id"], message["tool_call_id"]))
        if row["session_id"] == expected_native_id:
            messages.append(message)
    found = 0
    for message in messages:
        if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
            continue
        for call in message["tool_calls"]:
            if not isinstance(call, dict) or call.get("name") != "mcp_call_tool":
                continue
            if call.get("id") != tool_call_id or (expected_native_id, tool_call_id) in completed:
                continue
            args = call.get("arguments")
            if not isinstance(args, dict) or args.get("server_name") != server_name or \
                    args.get("tool_name") != "pickup_notification":
                continue
            native_args = args.get("arguments")
            if not isinstance(native_args, dict):
                native_args = {}
            if _json_exact(native_args, expected_args):
                found += 1
    return found == 1


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
                    raise ACPError("Devin ACP refused the requested operation")
                result = event.get("result")
                if not isinstance(result, dict):
                    raise ACPError("Devin ACP returned no result object")
                return result
        raise TimeoutError("Devin ACP request timed out")

    def close(self):
        # The process is terminated by the owner before the reader is joined.
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
                        pending_calls, state, native_db=None):
    expected_args = {"notification_id": notification_id}

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
        tool, args = None, None
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
            tool, args = _tool_and_args(call, server_name)
            if tool is None:
                if _call_conflicts(call, server_name):
                    return {"outcome": {"outcome": "cancelled"}}
                # Some ACP builds provide only the call ID in the session
                # update. The native transcript check below proves the rest.
                tool, args = "pickup_notification", expected_args
        else:
            # Permission-only envelopes can omit call details. Native transcript
            # lookup must bind this call ID to this exact server/tool/input.
            tool = "pickup_notification"
            args = expected_args
        if tool != "pickup_notification" or not _json_exact(args, expected_args):
            return {"outcome": {"outcome": "cancelled"}}
        if not _native_call_matches(expected_session, workspace, server_name, call_id,
                                    expected_args, native_db=native_db):
            return {"outcome": {"outcome": "cancelled"}}
        options = params.get("options")
        allowed = [item.get("optionId") for item in options if isinstance(item, dict)
                   and item.get("kind") == "allow_once"] if isinstance(options, list) else []
        if len(allowed) != 1 or not isinstance(allowed[0], str) or not allowed[0]:
            return {"outcome": {"outcome": "cancelled"}}
        if state["allowed"]:
            return {"outcome": {"outcome": "cancelled"}}
        state["allowed"] += 1
        return {"outcome": {"outcome": "selected", "optionId": allowed[0]}}
    return handle


def _native_server(project, role, session, *, root=None, db_path=None):
    cfg = config.load(root)
    cfg.require(project)
    if role not in {"inbox", "coordinator"}:
        raise ValueError("Unsupported Devin role")
    store = Store(db_path or cfg.database)
    if store.roles(project, "devin").get(role) != session:
        raise ValueError("Devin server session is no longer bound to this role")
    server_name = f"mhb_devin_{project}"
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("Install the MCP extra to start Devin's mailbox tool") from exc
    server = FastMCP(server_name)

    @server.tool()
    def pickup_notification(notification_id: str) -> dict:
        """Process only the exact pending notification this role was woken for."""
        return _pickup_notification(store, project, role, session, notification_id)

    server.run(transport="stdio")


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
    pending = store.pending(project, "devin", role, session)
    if not pending or pending["id"] != notification_id:
        raise ValueError("This notification is no longer the next pending item")
    item = store.claim(project, "devin", role, session, message_id=notification_id)
    if not item:
        raise ValueError("The exact notification could not be claimed")
    if item["id"] != notification_id:
        raise RuntimeError("Store returned a different notification ID")
    if role == "inbox":
        if not store.finish(project, "devin", role, session, notification_id, item["lease"]):
            raise RuntimeError("Inbox could not durably forward the notification")
        return {"id": notification_id, "forwarded": True}
    receipt = {key: item[key] for key in ("id", "sender", "recipient", "body", "reply_to", "attempt")}
    if not store.finish(project, "devin", role, session, notification_id, item["lease"]):
        raise RuntimeError("Coordinator could not durably acknowledge the notification")
    return {**receipt, "acknowledged": True}


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
         native_db=None, timeout=WAKE_TIMEOUT):
    """Load exact Devin session; permit one verified pickup_notification call."""
    executable = os.environ.get("MHB_DEVIN_BIN") or shutil.which("devin")
    if not executable:
        raise WakeRejected("Devin Local CLI is unavailable")
    cfg = config.load()
    if cfg.require(project) != project or Store(cfg.database).roles(project, "devin").get(role) != session:
        raise WakeRejected("Devin role is not the current project binding")
    notification_id = str(uuid.UUID(notification_id))
    server_name = f"mhb_devin_{project}"
    server = {"name": server_name, "command": sys.executable,
              "args": ["-m", "multi_harness_bridge.devin_acp", "serve", "--home",
                       str(cfg.root), project, role, session],
              "env": []}
    native_session = session.removeprefix("acp/devin-cli/")
    proc = _start(executable, workspace)
    client = ACPClient(proc, timeout=timeout)
    try:
        try:
            init = client.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
            if not init.get("agentCapabilities", {}).get("loadSession"):
                raise WakeRejected("Installed Devin ACP cannot load an existing bound session")
            client.call("session/load", {"sessionId": native_session,
                                         "cwd": str(Path(workspace).resolve()),
                                         "mcpServers": [server]})
        except WakeRejected:
            raise
        except Exception as exc:
            raise WakeDeferred(f"Devin rejected the bound session before prompt submission ({type(exc).__name__})") from None
        if before_prompt is not None and not before_prompt():
            raise WakeDeferred("Devin wake was stopped or rebound before prompt submission")
        prompt = ("Use only the configured native bridge tool "
                  f"{server_name}. Call pickup_notification exactly once with "
                  f"notification_id {notification_id}, then stop. Do not call any other tool.")
        state, pending = {"allowed": 0}, {}
        handler = _permission_handler(server_name=server_name, expected_session=native_session,
                                      workspace=workspace, notification_id=notification_id,
                                      pending_calls=pending, state=state, native_db=native_db)
        try:
            result = client.call("session/prompt", {"sessionId": native_session,
                "prompt": [{"type": "text", "text": prompt}]}, server_request=handler,
                notification=lambda params: _remember_tool_update(pending, params, native_session))
        except TimeoutError:
            raise
        except EOFError:
            raise TimeoutError("Devin ACP exited after prompt submission; outcome is uncertain") from None
        except ACPError as exc:
            if state["allowed"]:
                raise TimeoutError("Devin ACP failed after allowing the exact mailbox tool") from None
            raise WakeDeferred("Devin ACP refused the exact mailbox prompt") from None
        if state["allowed"] != 1:
            raise WakeRejected("Devin did not request the one authorized mailbox notification tool")
        return result
    finally:
        client.close()
        _stop_process(proc)


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


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("serve", help="start the bound one-tool Devin mailbox server")
    server.add_argument("--home", type=Path)
    server.add_argument("project")
    server.add_argument("role", choices=("inbox", "coordinator"))
    server.add_argument("session")
    args = parser.parse_args(argv)
    if args.command == "serve":
        _native_server(args.project, args.role, args.session, root=args.home)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
