import sqlite3
import threading
import time
import uuid

import pytest

from multi_harness_bridge import config, wake
from multi_harness_bridge import devin_acp
from multi_harness_bridge.store import Store


class FakeStore:
    def __init__(self, path):
        self.path = path
        self.message = {"id": str(uuid.uuid4()), "body": "Check the inbox"}
        self.claims = 0
        self.finishes = 0
        self.acks = 0

    def roles(self, project, harness):
        return {"inbox": "native-session-1", "coordinator": "native-session-2"}

    def status(self, project, harness):
        return {"enabled": True}

    def pending(self, project, harness, role, session, *, message_id=None, for_wake=False):
        return self.message if message_id in (None, self.message["id"]) else None


def test_wake_one_wakes_bound_session_and_never_claims_or_acknowledges(tmp_path, monkeypatch):
    store = FakeStore(tmp_path / "store.sqlite")
    called = []
    monkeypatch.setattr(wake, "_verify", lambda *_, **__: None)
    monkeypatch.setattr(wake, "_launch", lambda entry, values: called.append(values.copy()))
    entry = {"verify": ["fake-verify"], "wake": ["fake-wake"]}
    result = wake.wake_one(store, "sample", "codex", "inbox", "native-session-1",
                           commands={"codex": entry})
    assert result == {"id": store.message["id"], "status": "sent"}
    assert called[0]["session"] == "native-session-1"
    assert "Check the inbox" not in called[0]["body"]
    assert (store.claims, store.finishes, store.acks) == (0, 0, 0)
    assert wake.wake_one(store, "sample", "codex", "inbox", "native-session-1",
                         commands={"codex": entry})["status"] == "sent"
    assert len(called) == 1
    assert wake.wake_one(store, "sample", "codex", "inbox", "native-session-1",
                         commands={"codex": entry})["status"] == "sent"
    assert len(called) == 1


def test_mismatched_binding_does_not_dispatch(tmp_path, monkeypatch):
    store = FakeStore(tmp_path / "store.sqlite")
    monkeypatch.setattr(wake, "_wake", lambda *_: pytest.fail("unexpected wake"))
    assert wake.wake_one(store, "sample", "codex", "inbox", "wrong-session",
                         commands={"codex": {}}) == {
        "status": "held", "reason": "session-not-bound"}


def test_stopped_store_does_not_dispatch(tmp_path, monkeypatch):
    store = FakeStore(tmp_path / "store.sqlite")
    store.status = lambda *_: {"enabled": False}
    monkeypatch.setattr(wake, "_wake", lambda *_: pytest.fail("unexpected wake"))
    assert wake.wake_one(store, "sample", "codex", "inbox", "native-session-1",
                         commands={"codex": {}}) == {"status": "stopped"}


def test_identity_must_match_every_component(monkeypatch):
    monkeypatch.setattr(wake, "_run", lambda argv, timeout=8: type("R", (), {
        "returncode": 0, "stdout": '{"project":"sample","harness":"codex",'
        '"role":"coordinator","session":"wrong"}'})())
    with pytest.raises(wake.WakeDeferred, match="exactly match"):
        wake._verify({"verify": ["verifier"]}, {
            "project": "sample", "harness": "codex", "role": "coordinator",
            "session": "expected"})


def test_uncertain_wake_is_not_retried(tmp_path, monkeypatch):
    store = FakeStore(tmp_path / "store.sqlite")
    calls = []
    monkeypatch.setattr(wake, "_verify", lambda *_: None)
    def fail(*_):
        calls.append(1)
        raise wake.WakeError("delivery state is uncertain")
    monkeypatch.setattr(wake, "_launch", fail)
    entry = {"verify": ["fake-verify"], "wake": ["fake-wake"]}
    with pytest.raises(wake.WakeError):
        wake.wake_one(store, "sample", "claude", "inbox", "native-session-1",
                      commands={"claude": entry})
    result = wake.wake_one(store, "sample", "claude", "inbox", "native-session-1",
                           commands={"claude": entry})
    assert result["status"] == "uncertain"
    assert calls == [1]


def test_wake_ledger_makes_uncertain_permanent(tmp_path):
    db = wake._ledger(tmp_path / "store.sqlite")
    item = {"id": str(uuid.uuid4())}
    try:
        assert wake._reserve(db, item, "sample", "codex", "inbox", "s1")[0]
        with db:
            db.execute("UPDATE mhb_wake_attempts SET state='uncertain'")
        assert not wake._reserve(db, item, "sample", "codex", "inbox", "s1")[0]
    finally:
        db.close()


def test_explicit_host_rejection_retries_at_most_three_times(tmp_path, monkeypatch):
    store = FakeStore(tmp_path / "store.sqlite")
    calls = []
    monkeypatch.setattr(wake, "_verify", lambda *_: None)
    def reject(*_):
        calls.append(1)
        raise wake.WakeRejected("not accepted")
    monkeypatch.setattr(wake, "_launch", reject)
    entry = {"verify": ["fake-verify"], "wake": ["fake-wake"]}
    for _ in range(3):
        with pytest.raises(wake.WakeRejected):
            wake.wake_one(store, "sample", "codex", "inbox", "native-session-1",
                          commands={"codex": entry})
    result = wake.wake_one(store, "sample", "codex", "inbox", "native-session-1",
                           commands={"codex": entry})
    assert result["status"] == "retryable"
    assert calls == [1, 1, 1]


def test_safe_defer_refunds_attempt_budget(tmp_path, monkeypatch):
    store = FakeStore(tmp_path / "store.sqlite")
    monkeypatch.setattr(wake, "_verify", lambda *_: None)
    monkeypatch.setattr(wake, "_launch", lambda *_: (_ for _ in ()).throw(wake.WakeDeferred("busy")))
    entry = {"verify": ["fake-verify"], "wake": ["fake-wake"]}
    with pytest.raises(wake.WakeDeferred):
        wake.wake_one(store, "sample", "codex", "inbox", "native-session-1",
                      commands={"codex": entry})
    db = sqlite3.connect(store.path)
    try:
        state, attempts = db.execute("SELECT state,attempts FROM mhb_wake_attempts").fetchone()
    finally:
        db.close()
    assert (state, attempts) == ("retryable", 0)


@pytest.mark.parametrize("state, attempts", [("sent", 1), ("uncertain", 1), ("retryable", 3)])
def test_wake_skips_held_predecessors_without_resending_them(tmp_path, monkeypatch, state, attempts):
    store = Store(tmp_path / "bridge.sqlite")
    for harness in ("codex", "devin", "antigravity"):
        store.bind("sample", harness, "inbox", f"{harness}-inbox")
        store.bind("sample", harness, "coordinator", f"{harness}-coordinator")
    route = ("sample", "codex", "inbox", "codex-inbox")
    request = store.send_fanout("sample", "codex", ["devin", "antigravity"], "Two deliveries",
                                "request", session="codex-coordinator")
    older = store.pending(*route)
    next_id = next(item["id"] for item in request["deliveries"] if item["id"] != older["id"])
    db = wake._ledger(store.path)
    try:
        assert wake._reserve(db, older, *route)[0]
        with db:
            db.execute("UPDATE mhb_wake_attempts SET state=?, attempts=?", (state, attempts))
            if state == "retryable":
                db.execute("DELETE FROM mhb_wake_sessions")
            else:
                db.execute("UPDATE mhb_wake_sessions SET state=?", (state,))
    finally:
        db.close()

    calls = []

    def launch(_harness, values, *, before_prompt):
        assert before_prompt()
        calls.append(values["notification_id"])

    monkeypatch.setattr(wake, "_default_wake", launch)
    first_result = wake.wake_one(store, *route, commands={})
    if state in {"sent", "uncertain"}:
        assert first_result == {"id": next_id, "status": "held", "reason": "session-busy"}
        assert calls == []
    else:
        assert first_result == {"id": next_id, "status": "sent"}
        assert calls == [next_id]
    assert store.pending(*route)["id"] == older["id"]
    if state in {"sent", "uncertain"}:
        assert wake.wake_one(store, *route, commands={}) == {
            "id": next_id, "status": "held", "reason": "session-busy"}
    else:
        assert wake.wake_one(store, *route, commands={}) == {"id": older["id"], "status": state}


def _fake_devin(tmp_path, monkeypatch, *, refuse_args=False, eof=False, minimal=False):
    import json
    import time
    from multi_harness_bridge.store import Store
    session_id = "session-123"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "mhb-home"
    home.mkdir()
    (home / "projects.json").write_text(json.dumps({"version": 1, "projects": {"sample": [str(workspace)]}}))
    store = Store(home / "bridge.sqlite3")
    store.bind("sample", "devin", "coordinator", session_id)
    store.bind("sample", "devin", "inbox", "inbox-session")
    monkeypatch.setenv("MHB_HOME", str(home))
    notification_id = "11111111-2222-4333-8444-555555555555"
    native_db = tmp_path / "native.sqlite"
    with sqlite3.connect(native_db) as db:
        db.executescript("CREATE TABLE sessions(id TEXT,working_directory TEXT);"
                         "CREATE TABLE message_nodes(row_id INTEGER,session_id TEXT,chat_message TEXT,created_at REAL);")
        db.execute("INSERT INTO sessions VALUES(?,?)", (session_id, str(workspace)))
        args = {"notification_id": notification_id}
        native_call = {"role": "assistant", "tool_calls": [{"id": "call-1", "name": "mcp_call_tool",
            "arguments": {"server_name": "mhb_roles_sample", "tool_name": "pickup_wake",
                          "arguments": args}}]}
        db.executemany("INSERT INTO message_nodes VALUES(?,?,?,?)",
                       [(row_id, session_id, json.dumps(native_call), time.time()) for row_id in (0, 1)])
    executable = tmp_path / "fake-devin"
    executable.write_text("""#!/usr/bin/env python3
import json,os,sys
for line in sys.stdin:
 request=json.loads(line)
 with open(os.environ['MHB_CAPTURE'],'a') as capture: capture.write(json.dumps(request)+'\\n')
 method=request['method']
 if method=='initialize': result={'agentCapabilities':{'loadSession':True}}
 elif method=='session/load': result={}
 elif method=='session/prompt':
  if os.environ.get('MHB_FAKE_EOF')=='1': sys.exit(0)
  call={'sessionUpdate':'tool_call','toolCallId':'call-1'}
  if os.environ.get('MHB_FAKE_MINIMAL')!='1':
   call.update({'title':'Calling pickup_wake from mhb_roles_sample',
                'rawInput':{'notification_id':os.environ['MHB_NOTIFICATION_ID']}})
  if os.environ.get('MHB_REFUSE_ARGS')=='1': call['rawInput']={'notification_id':'99999999-2222-4333-8444-555555555555'}
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'session-123','update':call}}),flush=True)
  print(json.dumps({'jsonrpc':'2.0','id':77,'method':'session/request_permission','params':{
    'sessionId':'session-123','toolCallId':'call-1','options':[{'kind':'allow_once','optionId':'allow-1'}]}}),flush=True)
  answer=json.loads(sys.stdin.readline())
  with open(os.environ['MHB_CAPTURE'],'a') as capture: capture.write(json.dumps(answer)+'\\n')
  print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'stopReason':'end_turn'}}),flush=True)
  break
 print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
""")
    executable.chmod(0o755)
    capture = tmp_path / "acp-calls.jsonl"
    monkeypatch.setenv("MHB_CAPTURE", str(capture))
    monkeypatch.setenv("MHB_DEVIN_BIN", str(executable))
    monkeypatch.setenv("MHB_NOTIFICATION_ID", notification_id)
    monkeypatch.setenv("MHB_REFUSE_ARGS", "1" if refuse_args else "0")
    monkeypatch.setenv("MHB_FAKE_EOF", "1" if eof else "0")
    monkeypatch.setenv("MHB_FAKE_MINIMAL", "1" if minimal else "0")
    return session_id, workspace, notification_id, native_db, capture


def test_devin_acp_exact_duplex_permission_and_id_only_prompt(tmp_path, monkeypatch):
    session, workspace, notification_id, native_db, capture = _fake_devin(tmp_path, monkeypatch)
    result = devin_acp.wake("sample", "coordinator", session, notification_id,
                            workspace, native_db=native_db)
    assert result["stopReason"] == "end_turn"
    import json
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    assert calls[1]["params"]["sessionId"] == session
    assert calls[1]["params"]["mcpServers"] == []
    prompt = calls[2]["params"]["prompt"][0]["text"]
    assert notification_id in prompt
    assert "Check the inbox" not in prompt
    permission_reply = calls[3]
    assert permission_reply["id"] == 77
    assert permission_reply["result"]["outcome"] == {"outcome": "selected", "optionId": "allow-1"}


def test_devin_acp_resolves_call_id_only_update_from_native_transcript(tmp_path, monkeypatch):
    session, workspace, notification_id, native_db, _ = _fake_devin(tmp_path, monkeypatch, minimal=True)
    result = devin_acp.wake("sample", "coordinator", session, notification_id,
                            workspace, native_db=native_db)
    assert result["stopReason"] == "end_turn"


def test_devin_acp_cancels_mismatched_tool_args(tmp_path, monkeypatch):
    session, workspace, notification_id, native_db, capture = _fake_devin(tmp_path, monkeypatch, refuse_args=True)
    with pytest.raises(wake.WakeRejected):
        devin_acp.wake("sample", "coordinator", session, notification_id,
                       workspace, native_db=native_db)
    import json
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    assert calls[-1]["result"]["outcome"] == {"outcome": "cancelled"}


def test_devin_permission_refuses_wrong_server_session_options_and_repeat(tmp_path, monkeypatch):
    session, workspace, notification_id, native_db, _ = _fake_devin(tmp_path, monkeypatch)
    handler_state = {"allowed": 0}
    pending = {"call-1": {"session_id": session, "call": {
        "toolCallId": "call-1", "title": "Calling pickup_wake from mhb_roles_sample",
        "rawInput": {"notification_id": notification_id}}}}
    handler = devin_acp._permission_handler(server_name="mhb_roles_sample",
        expected_session=session, workspace=workspace, notification_id=notification_id,
        pending_calls=pending, state=handler_state, native_db=native_db)
    def request(**overrides):
        params = {"sessionId": session, "toolCallId": "call-1",
                  "options": [{"kind": "allow_once", "optionId": "allow-1"}]}
        params.update(overrides)
        return {"method": "session/request_permission", "params": params}
    wrong_session = handler(request(sessionId="other-session"))
    assert wrong_session["outcome"]["outcome"] == "cancelled"
    pending["call-1"] = {"session_id": session, "call": {
        "toolCallId": "call-1", "title": "Calling pickup_wake from other-server",
        "rawInput": {"notification_id": notification_id}}}
    wrong_server = handler(request())
    assert wrong_server["outcome"]["outcome"] == "cancelled"
    pending["call-1"] = {"session_id": session, "call": {
        "toolCallId": "call-1", "title": "Calling pickup_wake from mhb_roles_sample",
        "rawInput": {"notification_id": notification_id}}}
    no_allow_once = handler(request(options=[{"kind": "allow_always", "optionId": "always"}]))
    assert no_allow_once["outcome"]["outcome"] == "cancelled"
    allowed = handler(request())
    assert allowed["outcome"]["outcome"] == "selected"
    assert handler(request())["outcome"]["outcome"] == "cancelled"


def test_devin_acp_eof_after_prompt_is_uncertain(tmp_path, monkeypatch):
    session, workspace, notification_id, native_db, _ = _fake_devin(tmp_path, monkeypatch, eof=True)
    with pytest.raises(TimeoutError, match="uncertain"):
        devin_acp.wake("sample", "coordinator", session, notification_id,
                       workspace, native_db=native_db)


def test_devin_routed_finish_permission_requires_a_persisted_pickup(tmp_path, monkeypatch):
    import json
    import time

    session, workspace, notification_id, native_db, _ = _fake_devin(tmp_path, monkeypatch)
    pending = {}
    state = {"allowed": 0}
    handler = devin_acp._permission_handler(server_name="mhb_roles_sample", expected_session=session,
        workspace=workspace, notification_id=notification_id, pending_calls=pending, state=state,
        native_db=native_db, routed_coordinator=True)

    def request(call_id):
        return handler({"method": "session/request_permission", "params": {"sessionId": session,
            "toolCallId": call_id, "options": [{"kind": "allow_once", "optionId": "one"}]}})["outcome"]["outcome"]

    def add_native(row_id, payload):
        with sqlite3.connect(native_db) as db:
            db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)",
                       (row_id, session, json.dumps(payload), time.time()))

    def finish(id):
        return {"role": "assistant", "tool_calls": [{"id": "finish", "name": "mcp_call_tool", "arguments": {
            "server_name": "mhb_roles_sample", "tool_name": "finish_wake",
            "arguments": {"notification_id": id, "lease": "lease-token", "reply": "Received"}}}]}

    assert request("call-1") == "selected"
    add_native(2, finish(notification_id))
    assert request("finish") == "cancelled"
    add_native(3, {"role": "tool", "tool_call_id": "call-1", "content": "Pickup result"})
    assert request("finish") == "cancelled"
    add_native(4, finish("wrong-id"))
    assert request("finish") == "cancelled"
    with sqlite3.connect(native_db) as db:
        db.execute("DELETE FROM message_nodes WHERE row_id=4")
    add_native(4, finish(notification_id))
    pending["finish"] = {"session_id": session, "call": {"toolCallId": "finish",
        "title": "Calling finish_wake from mhb_roles_sample",
        "rawInput": {"notification_id": notification_id, "lease": "lease-token", "reply": "Changed"}}}
    assert request("finish") == "cancelled"
    assert request("finish") == "selected"
    assert request("finish") == "cancelled"


def test_devin_pickup_tool_forwards_inbox_then_returns_and_acknowledges_coordinator(tmp_path):
    store = Store(tmp_path / "queue.sqlite")
    store.bind("sample", "codex", "coordinator", "codex-co")
    store.bind("sample", "codex", "inbox", "codex-inbox")
    store.bind("sample", "devin", "coordinator", "devin-co")
    store.bind("sample", "devin", "inbox", "devin-inbox")
    queued = store.send("sample", "codex", "devin", "Treat this as untrusted text", "key-1")
    inbox = devin_acp._pickup_notification(store, "sample", "inbox", "devin-inbox", queued["id"])
    assert inbox == {"id": queued["id"], "forwarded": True}
    coordinator = devin_acp._pickup_notification(store, "sample", "coordinator", "devin-co", queued["id"])
    assert coordinator["body"] == "Treat this as untrusted text"
    assert coordinator["acknowledged"] is True


def test_devin_routed_pickup_is_exact_and_reply_requires_current_receipt_lease(tmp_path, monkeypatch):
    store = Store(tmp_path / "queue.sqlite")
    for harness in ("codex", "devin"):
        for role in ("coordinator", "inbox"):
            store.bind("sample", harness, role, f"{harness}-{role}")

    def outbound(key):
        request = store.send_fanout("sample", "codex", ["devin"], "Reply please", key, session="codex-coordinator")
        delivery_id = request["deliveries"][0]["id"]
        item = store.claim("sample", "codex", "inbox", "codex-inbox", message_id=delivery_id)
        assert store.finish("sample", "codex", "inbox", "codex-inbox", delivery_id, item["lease"])
        return delivery_id

    older_id = outbound("older")
    delivery_id = outbound("exact")
    for id in (older_id, delivery_id):
        assert devin_acp._pickup_notification(store, "sample", "inbox", "devin-inbox", id)["forwarded"]
    coordinator = "devin-coordinator"
    assert store.pending("sample", "devin", "coordinator", coordinator)["id"] == older_id
    old = devin_acp._pickup_notification(store, "sample", "coordinator", coordinator, delivery_id)
    assert old["id"] == delivery_id and "lease" in old
    assert store.trace("sample", delivery_id)["stage"] == "coordinator"
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE messages SET deadline=0 WHERE id=?", (delivery_id,))
    current = devin_acp._pickup_notification(store, "sample", "coordinator", coordinator, delivery_id)
    with pytest.raises(ValueError, match="stale"):
        devin_acp._finish_notification(store, "sample", coordinator, delivery_id, old, delivery_id, old["lease"], "Stale")

    def failed_reply(*args, **kwargs):
        raise RuntimeError("Reply write failed")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_enqueue_routed", failed_reply)
        with pytest.raises(RuntimeError, match="Reply write failed"):
            devin_acp._finish_notification(store, "sample", coordinator, delivery_id, current,
                                           delivery_id, current["lease"], "Received")
    after_failure = store.trace("sample", delivery_id)
    assert after_failure["stage"] == "coordinator" and not after_failure["replies"]

    result = devin_acp._finish_notification(store, "sample", coordinator, delivery_id, current,
                                            delivery_id, current["lease"], "Received")
    assert result["acknowledged"]
    assert result["reply"]["request_id"] == current["request_id"]
    trace = store.trace("sample", result["reply"]["id"])
    assert trace["reply_to"] == delivery_id and trace["stage"] == "outbound"
    assert store.status("sample", "devin")["done"] == 1


def test_reconcile_requires_one_unresolved_nonactive_attempt(tmp_path):
    store = FakeStore(tmp_path / "store.sqlite")
    item_id = store.message["id"]
    db = wake._ledger(store.path)
    now = 100
    with db:
        db.execute("""INSERT INTO mhb_wake_attempts
            (notification_id,project,harness,role,session,state,attempts,started_at,updated_at)
            VALUES(?,?,?,?,?,'uncertain',1,?,?)""",
            (item_id, "sample", "devin", "coordinator", "session-a", now, now))
    db.close()
    assert wake.reconcile(store, "sample", item_id, "not-sent")["status"] == "retryable"
    db = wake._ledger(store.path)
    with db:
        db.execute("UPDATE mhb_wake_attempts SET state='uncertain' WHERE notification_id=?", (item_id,))
        db.execute("""INSERT INTO mhb_wake_attempts
            (notification_id,project,harness,role,session,state,attempts,started_at,updated_at)
            VALUES(?,?,?,?,?,'uncertain',1,?,?)""",
            (item_id, "sample", "devin", "inbox", "session-b", now, now))
    db.close()
    with pytest.raises(ValueError, match="multiple unresolved"):
        wake.reconcile(store, "sample", item_id, "sent")


def test_reconcile_refuses_active_wake_and_recovers_stale_sending(tmp_path):
    store = FakeStore(tmp_path / "store.sqlite")
    item_id = store.message["id"]
    db = wake._ledger(store.path)
    now = wake.time.time()
    with db:
        db.execute("""INSERT INTO mhb_wake_attempts
            (notification_id,project,harness,role,session,state,attempts,started_at,updated_at)
            VALUES(?,?,?,?,?,'sending',1,?,?)""",
            (item_id, "sample", "devin", "coordinator", "session-a", now, now))
    db.close()
    with pytest.raises(ValueError, match="still be active"):
        wake.reconcile(store, "sample", item_id, "not-sent")
    db = wake._ledger(store.path)
    with db:
        db.execute("UPDATE mhb_wake_attempts SET updated_at=? WHERE notification_id=?",
                   (now - wake.MAX_COMMAND_TIMEOUT - 20, item_id))
    db.close()
    assert wake.reconcile(store, "sample", item_id, "not-sent")["status"] == "retryable"


def _codex_relay_store(tmp_path, *, updated=None):
    """Store with a fresh codex coordinator-pending message and agent-relay on."""
    store = Store(tmp_path / "bridge.sqlite3")
    store.bind("sample", "codex", "coordinator", "codex-coordinator")
    store.bind("sample", "codex", "inbox", "codex-inbox")
    message = store.send("sample", "devin", "codex", "queued work", "stable-key")
    claimed = store.claim("sample", "codex", "inbox", "codex-inbox",
                          message_id=message["id"])
    store.finish("sample", "codex", "inbox", "codex-inbox",
                 message["id"], claimed["lease"])
    config.set_project_option(tmp_path, "sample", "codex", "coordinator_wake", "agent-relay")
    if updated is not None:
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE messages SET updated=? WHERE id=?",
                       (updated, message["id"]))
    return store, message["id"]


def test_codex_agent_relay_holds_fresh_coordinator_wake(tmp_path, monkeypatch):
    store, message_id = _codex_relay_store(tmp_path)
    monkeypatch.setattr(wake, "_default_wake",
                        lambda *_, **__: pytest.fail("fallback dispatched during grace"))
    result = wake.wake_one(store, "sample", "codex", "coordinator",
                           "codex-coordinator", commands={})
    assert result == {"status": "relay-pending", "id": message_id}
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM mhb_wake_attempts").fetchone()[0] == 0


def test_codex_agent_relay_falls_back_to_queue_after_grace(tmp_path, monkeypatch):
    store, message_id = _codex_relay_store(
        tmp_path, updated=time.time() - wake.RELAY_GRACE_SECONDS - 1)
    calls = []
    monkeypatch.setattr(wake, "_default_wake",
                        lambda harness, values, *, before_prompt: calls.append(values["notification_id"]))
    result = wake.wake_one(store, "sample", "codex", "coordinator",
                           "codex-coordinator", commands={})
    assert result == {"id": message_id, "status": "sent"}
    assert calls == [message_id]


def test_codex_coordinator_wake_without_option_dispatches_immediately(tmp_path, monkeypatch):
    store = Store(tmp_path / "bridge.sqlite3")
    store.bind("sample", "codex", "coordinator", "codex-coordinator")
    store.bind("sample", "codex", "inbox", "codex-inbox")
    message = store.send("sample", "devin", "codex", "queued work", "stable-key")
    claimed = store.claim("sample", "codex", "inbox", "codex-inbox",
                          message_id=message["id"])
    store.finish("sample", "codex", "inbox", "codex-inbox",
                 message["id"], claimed["lease"])
    calls = []
    monkeypatch.setattr(wake, "_default_wake",
                        lambda harness, values, *, before_prompt: calls.append(values["notification_id"]))
    result = wake.wake_one(store, "sample", "codex", "coordinator",
                           "codex-coordinator", commands={})
    assert result == {"id": message["id"], "status": "sent"}
    assert calls == [message["id"]]


def test_codex_agent_relay_only_covers_coordinator_role(tmp_path, monkeypatch):
    store = Store(tmp_path / "bridge.sqlite3")
    store.bind("sample", "codex", "coordinator", "codex-coordinator")
    store.bind("sample", "codex", "inbox", "codex-inbox")
    message = store.send("sample", "devin", "codex", "queued work", "stable-key")
    config.set_project_option(tmp_path, "sample", "codex", "coordinator_wake", "agent-relay")
    calls = []
    monkeypatch.setattr(wake, "_default_wake",
                        lambda harness, values, *, before_prompt: calls.append(values["notification_id"]))
    result = wake.wake_one(store, "sample", "codex", "inbox",
                           "codex-inbox", commands={})
    assert result == {"id": message["id"], "status": "sent"}
    assert calls == [message["id"]]


def test_agent_relay_record_suppresses_fallback_wake(tmp_path, monkeypatch):
    store, message_id = _codex_relay_store(tmp_path)
    assert wake.reserve_agent_relay(store, "sample", "codex", "coordinator",
                                    "codex-coordinator", message_id) == (True, None)
    wake.record_agent_relay(store, "sample", "codex", "coordinator",
                            "codex-coordinator", message_id)
    monkeypatch.setattr(wake, "_default_wake",
                        lambda *_, **__: pytest.fail("duplicate wake dispatched"))
    result = wake.wake_one(store, "sample", "codex", "coordinator",
                           "codex-coordinator", commands={})
    assert result == {"id": message_id, "status": "sent"}


def test_one_session_gets_one_wake_until_claim_finish_and_cooldown(tmp_path, monkeypatch):
    store = Store(tmp_path / "bridge.sqlite3")
    route = ("sample", "codex", "inbox", "codex-inbox")
    store.bind("sample", "codex", "inbox", "codex-inbox")
    store.bind("sample", "codex", "coordinator", "codex-coordinator")
    first = store.send("sample", "devin", "codex", "first", "first")
    second = store.send("sample", "devin", "codex", "second", "second")
    calls = []
    monkeypatch.setattr(wake, "_default_wake",
                        lambda _harness, values, *, before_prompt: calls.append(values["notification_id"]))

    assert wake.wake_one(store, *route, commands={}) == {"id": first["id"], "status": "sent"}
    # A dispatcher restart reopens the same durable session lane.
    store = Store(store.path)
    assert wake.wake_one(store, *route, commands={}) == {
        "id": second["id"], "status": "held", "reason": "session-busy"}
    assert calls == [first["id"]]

    claimed = store.claim(*route, message_id=first["id"])
    assert wake.wake_one(store, *route, commands={}) == {
        "id": second["id"], "status": "held", "reason": "session-busy"}
    assert store.finish(*route, first["id"], claimed["lease"])
    assert wake.wake_one(store, *route, commands={}) == {
        "id": second["id"], "status": "held", "reason": "session-busy"}

    monkeypatch.setattr(wake, "TURN_COOLDOWN_SECONDS", 0)
    assert wake.wake_one(store, *route, commands={}) == {"id": second["id"], "status": "sent"}
    assert calls == [first["id"], second["id"]]


def test_concurrent_dispatchers_share_one_session_lane(tmp_path, monkeypatch):
    store = Store(tmp_path / "bridge.sqlite3")
    route = ("sample", "codex", "inbox", "codex-inbox")
    store.bind("sample", "codex", "inbox", "codex-inbox")
    store.bind("sample", "codex", "coordinator", "codex-coordinator")
    first = store.send("sample", "devin", "codex", "first", "first")
    second = store.send("sample", "devin", "codex", "second", "second")
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def launch(_harness, values, *, before_prompt):
        assert before_prompt()
        calls.append(values["notification_id"])
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(wake, "_default_wake", launch)
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(
        wake.wake_one(store, *route, commands={})))
    worker.start()
    assert entered.wait(2)
    competing = wake.wake_one(store, *route, commands={})
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert outcomes == [{"id": first["id"], "status": "sent"}]
    assert competing == {"id": second["id"], "status": "held", "reason": "session-busy"}
    assert calls == [first["id"]]


def test_expired_lease_does_not_release_wake_lane_without_exact_recovery(tmp_path, monkeypatch):
    store = Store(tmp_path / "bridge.sqlite3")
    route = ("sample", "codex", "inbox", "codex-inbox")
    store.bind("sample", "codex", "inbox", "codex-inbox")
    store.bind("sample", "codex", "coordinator", "codex-coordinator")
    store.bind("sample", "devin", "inbox", "devin-inbox")
    store.bind("sample", "devin", "coordinator", "devin-coordinator")
    first = store.send_fanout("sample", "codex", ["devin"], "first", "first",
                              session="codex-coordinator")["deliveries"][0]
    second = store.send_fanout("sample", "codex", ["devin"], "second", "second",
                               session="codex-coordinator")["deliveries"][0]
    calls = []
    monkeypatch.setattr(wake, "_default_wake",
                        lambda _harness, values, *, before_prompt: calls.append(values["notification_id"]))
    assert wake.wake_one(store, *route, commands={}) == {"id": first["id"], "status": "sent"}
    claimed = store.claim(*route, message_id=first["id"])
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE messages SET deadline=? WHERE id=?", (time.time() - 1, first["id"]))
    assert wake.wake_one(store, *route, commands={}) == {
        "id": second["id"], "status": "held", "reason": "session-busy"}
    assert wake.renotify(store, "sample", first["id"], harness="codex", role="inbox",
                         session="codex-inbox")["status"] == "retry-authorized"
    with sqlite3.connect(store.path) as db:
        assert db.execute("""SELECT prior_state,action FROM mhb_wake_history
            WHERE notification_id=?""", (first["id"],)).fetchone() == ("sent", "renotify")
    assert wake.wake_one(store, *route, commands={}) == {"id": first["id"], "status": "sent"}
    assert calls == [first["id"], first["id"]]


def test_renotify_rechecks_pending_inside_transaction_and_preserves_finish_lane(tmp_path, monkeypatch):
    store = Store(tmp_path / "bridge.sqlite3")
    route = ("sample", "codex", "inbox", "codex-inbox")
    store.bind("sample", "codex", "inbox", "codex-inbox")
    store.bind("sample", "codex", "coordinator", "codex-coordinator")
    store.bind("sample", "devin", "inbox", "devin-inbox")
    store.bind("sample", "devin", "coordinator", "devin-coordinator")
    first = store.send_fanout("sample", "codex", ["devin"], "first", "first",
                              session="codex-coordinator")["deliveries"][0]
    second = store.send_fanout("sample", "codex", ["devin"], "second", "second",
                               session="codex-coordinator")["deliveries"][0]
    monkeypatch.setattr(wake, "_default_wake", lambda *_args, **_kwargs: None)
    assert wake.wake_one(store, *route, commands={}) == {"id": first["id"], "status": "sent"}
    monkeypatch.setattr(wake, "TURN_COOLDOWN_SECONDS", 60)

    real_ledger = wake._ledger
    injected = False

    class InterleavingConnection:
        def __init__(self, db):
            self.db = db

        @property
        def row_factory(self):
            return self.db.row_factory

        @row_factory.setter
        def row_factory(self, factory):
            self.db.row_factory = factory

        def execute(self, statement, *parameters):
            nonlocal injected
            if statement == "BEGIN IMMEDIATE" and not injected:
                injected = True
                claimed = store.claim(*route, message_id=first["id"])
                assert claimed
                assert store.finish(*route, first["id"], claimed["lease"])
            return self.db.execute(statement, *parameters)

        def commit(self):
            return self.db.commit()

        def close(self):
            return self.db.close()

    monkeypatch.setattr(wake, "_ledger", lambda path: InterleavingConnection(real_ledger(path)))
    with pytest.raises(ValueError, match="not pending and unclaimed"):
        wake.renotify(store, "sample", first["id"], harness="codex", role="inbox",
                      session="codex-inbox")
    assert injected
    with sqlite3.connect(store.path) as db:
        stage = db.execute("SELECT stage FROM messages WHERE id=?", (first["id"],)).fetchone()
        attempt = db.execute("SELECT state FROM mhb_wake_attempts WHERE notification_id=?",
                             (first["id"],)).fetchone()
        lane = db.execute("SELECT notification_id,state FROM mhb_wake_sessions").fetchone()
    assert stage == ("inbox",)
    assert attempt == ("sent",)
    assert lane == (first["id"], "cooldown")
    monkeypatch.setattr(wake, "_ledger", real_ledger)
    assert wake.wake_one(store, *route, commands={}) == {
        "id": second["id"], "status": "held", "reason": "session-busy"}


def test_fast_native_claim_and_finish_preserve_cooldown_lane(tmp_path, monkeypatch):
    store = Store(tmp_path / "bridge.sqlite3")
    route = ("sample", "codex", "inbox", "codex-inbox")
    store.bind("sample", "codex", "inbox", "codex-inbox")
    store.bind("sample", "codex", "coordinator", "codex-coordinator")
    store.bind("sample", "devin", "inbox", "devin-inbox")
    store.bind("sample", "devin", "coordinator", "devin-coordinator")
    first = store.send_fanout("sample", "codex", ["devin"], "first", "first",
                              session="codex-coordinator")["deliveries"][0]
    second = store.send_fanout("sample", "codex", ["devin"], "second", "second",
                               session="codex-coordinator")["deliveries"][0]

    def fast_host(_harness, values, *, before_prompt):
        assert before_prompt()
        message_id = values["notification_id"]
        claimed = store.claim(*route, message_id=message_id)
        assert store.finish(*route, message_id, claimed["lease"])

    monkeypatch.setattr(wake, "_default_wake", fast_host)
    assert wake.wake_one(store, *route, commands={}) == {"id": first["id"], "status": "sent"}
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT state FROM mhb_wake_sessions").fetchone() == ("cooldown",)
    assert wake.wake_one(store, *route, commands={}) == {
        "id": second["id"], "status": "held", "reason": "session-busy"}


def test_legacy_sent_backlog_does_not_resend_or_block_new_stage_work(tmp_path, monkeypatch):
    store = Store(tmp_path / "bridge.sqlite3")
    route = ("sample", "codex", "inbox", "codex-inbox")
    store.bind("sample", "codex", "inbox", "codex-inbox")
    store.bind("sample", "codex", "coordinator", "codex-coordinator")
    store.bind("sample", "devin", "inbox", "devin-inbox")
    store.bind("sample", "devin", "coordinator", "devin-coordinator")
    old_outbound1 = store.send_fanout("sample", "codex", ["devin"], "old outbound 1", "old-1",
                                      session="codex-coordinator")["deliveries"][0]
    old_outbound2 = store.send_fanout("sample", "codex", ["devin"], "old outbound 2", "old-2",
                                      session="codex-coordinator")["deliveries"][0]
    old_inbox = store.send("sample", "devin", "codex", "old inbox", "old-inbox")
    old_coordinator = store.send("sample", "devin", "codex", "old coordinator", "old-coordinator")
    claimed = store.claim("sample", "codex", "inbox", "codex-inbox",
                          message_id=old_coordinator["id"])
    assert store.finish("sample", "codex", "inbox", "codex-inbox",
                        old_coordinator["id"], claimed["lease"])
    new_inbox = store.send_fanout("sample", "codex", ["devin"], "new inbox", "new-inbox",
                                  session="codex-coordinator")["deliveries"][0]
    new_coordinator = store.send("sample", "devin", "codex", "new coordinator", "new-coordinator")
    claimed = store.claim("sample", "codex", "inbox", "codex-inbox",
                          message_id=new_coordinator["id"])
    assert store.finish("sample", "codex", "inbox", "codex-inbox",
                        new_coordinator["id"], claimed["lease"])
    db = wake._ledger(store.path)
    with db:
        now = time.time()
        for message in (old_outbound1, old_outbound2, old_inbox):
            db.execute("""INSERT INTO mhb_wake_attempts
                (notification_id,project,harness,role,session,state,attempts,started_at,updated_at)
                VALUES(?,?,?,?,?,'sent',1,?,?)""",
                (message["id"], "sample", "codex", "inbox", "codex-inbox", now, now))
        db.execute("""INSERT INTO mhb_wake_attempts
            (notification_id,project,harness,role,session,state,attempts,started_at,updated_at)
            VALUES(?,?,?,?,?,'sent',1,?,?)""",
            (old_coordinator["id"], "sample", "codex", "coordinator", "codex-coordinator", now, now))
    db.close()
    calls = []
    monkeypatch.setattr(wake, "TURN_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(wake, "_default_wake",
                        lambda _harness, values, *, before_prompt: calls.append(values["notification_id"]))
    assert wake.wake_one(store, *route, commands={}) == {"id": new_inbox["id"], "status": "sent"}
    assert calls == [new_inbox["id"]]
    claimed = store.claim(*route, message_id=new_inbox["id"])
    assert store.finish(*route, new_inbox["id"], claimed["lease"])
    monkeypatch.setattr(wake, "TURN_COOLDOWN_SECONDS", 0)
    assert wake.wake_one(store, "sample", "codex", "coordinator", "codex-coordinator",
                         commands={}) == {"id": new_coordinator["id"], "status": "sent"}
    assert calls == [new_inbox["id"], new_coordinator["id"]]
    old_pending = [old_outbound1, old_outbound2, old_inbox, old_coordinator]
    with sqlite3.connect(store.path) as db:
        old_states = [db.execute("SELECT state FROM mhb_wake_attempts WHERE notification_id=?",
                                 (message["id"],)).fetchone()[0] for message in old_pending]
        old_stages = [db.execute("SELECT stage FROM messages WHERE id=?",
                                 (message["id"],)).fetchone()[0] for message in old_pending]
    assert old_states == ["sent"] * 4
    assert old_stages == ["outbound", "outbound", "inbox", "coordinator"]


def test_legacy_coordinator_sent_record_does_not_block_new_coordinator_work(tmp_path, monkeypatch):
    store = Store(tmp_path / "bridge.sqlite3")
    for harness in ("codex", "devin"):
        store.bind("sample", harness, "inbox", f"{harness}-inbox")
        store.bind("sample", harness, "coordinator", f"{harness}-coordinator")
    first = store.send("sample", "devin", "codex", "first", "first")
    second = store.send("sample", "devin", "codex", "second", "second")
    for message in (first, second):
        claimed = store.claim("sample", "codex", "inbox", "codex-inbox",
                              message_id=message["id"])
        assert store.finish("sample", "codex", "inbox", "codex-inbox",
                            message["id"], claimed["lease"])
    db = wake._ledger(store.path)
    with db:
        now = time.time()
        db.execute("""INSERT INTO mhb_wake_attempts
            (notification_id,project,harness,role,session,state,attempts,started_at,updated_at)
            VALUES(?,?,?,?,?,'sent',1,?,?)""",
            (first["id"], "sample", "codex", "coordinator", "codex-coordinator", now, now))
    db.close()
    calls = []
    monkeypatch.setattr(wake, "_default_wake",
                        lambda _harness, values, *, before_prompt: calls.append(values["notification_id"]))
    assert wake.wake_one(store, "sample", "codex", "coordinator", "codex-coordinator",
                         commands={}) == {"id": second["id"], "status": "sent"}
    assert calls == [second["id"]]
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT stage FROM messages WHERE id=?", (first["id"],)).fetchone() == ("coordinator",)
        assert db.execute("SELECT state FROM mhb_wake_attempts WHERE notification_id=?",
                          (first["id"],)).fetchone() == ("sent",)
    # The same database's Devin role remains independently dispatchable.
    unrelated = store.send("sample", "codex", "devin", "unrelated", "unrelated")
    result = wake.wake_one(store, "sample", "devin", "inbox", "devin-inbox", commands={})
    assert result == {"id": unrelated["id"], "status": "sent"}
    assert calls == [second["id"], unrelated["id"]]


def test_codex_thread_load_is_darwin_best_effort(monkeypatch):
    calls = []
    monkeypatch.setattr(wake.sys, "platform", "darwin")
    monkeypatch.setattr(wake, "_run", lambda argv, timeout=8: calls.append((argv, timeout)))
    wake._load_codex_thread("thread-123")
    assert calls == [(["open", "codex://threads/thread-123"], 5)]
    calls.clear()
    monkeypatch.setattr(wake.sys, "platform", "linux")
    wake._load_codex_thread("thread-123")
    assert calls == []
    monkeypatch.setattr(wake.sys, "platform", "darwin")

    def boom(*_, **__):
        raise wake.WakeError("unavailable")

    monkeypatch.setattr(wake, "_run", boom)
    wake._load_codex_thread("thread-123")


def test_default_wake_codex_loads_thread_then_queues(monkeypatch):
    order = []
    monkeypatch.setattr(wake.shutil, "which", lambda name: "/fake/codex")
    monkeypatch.setattr(wake, "_load_codex_thread", lambda session: order.append(("load", session)))

    def fake_run(argv, timeout=8):
        order.append(("run", argv))
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(wake, "_run", fake_run)
    wake._default_wake("codex", {"project": "sample", "harness": "codex",
                                 "role": "coordinator", "session": "sess-1",
                                 "notification_id": "nid-1", "body": "wake"})
    assert order[0] == ("load", "sess-1")
    assert order[1][0] == "run"
    argv = order[1][1]
    assert argv[1:4] == ["queue", "--thread", "sess-1"]
    assert "nid-1" in argv[-1]
