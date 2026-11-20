import sqlite3
import uuid

import pytest

from multi_harness_bridge import wake
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

    def pending(self, project, harness, role, session):
        return self.message


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


def _fake_devin(tmp_path, monkeypatch, *, refuse_args=False, eof=False):
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
            "arguments": {"server_name": "mhb_devin_sample", "tool_name": "pickup_notification",
                          "arguments": args}}]}
        db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)",
                   (1, session_id, json.dumps(native_call), time.time()))
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
  call={'sessionUpdate':'tool_call','toolCallId':'call-1','title':'Calling pickup_notification from mhb_devin_sample',
        'rawInput':{'notification_id':os.environ['MHB_NOTIFICATION_ID']}}
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
    return session_id, workspace, notification_id, native_db, capture


def test_devin_acp_exact_duplex_permission_and_id_only_prompt(tmp_path, monkeypatch):
    session, workspace, notification_id, native_db, capture = _fake_devin(tmp_path, monkeypatch)
    result = devin_acp.wake("sample", "coordinator", session, notification_id,
                            workspace, native_db=native_db)
    assert result["stopReason"] == "end_turn"
    import json
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    assert calls[1]["params"]["sessionId"] == session
    assert calls[1]["params"]["mcpServers"][0]["name"] == "mhb_devin_sample"
    prompt = calls[2]["params"]["prompt"][0]["text"]
    assert notification_id in prompt
    assert "Check the inbox" not in prompt
    permission_reply = calls[3]
    assert permission_reply["id"] == 77
    assert permission_reply["result"]["outcome"] == {"outcome": "selected", "optionId": "allow-1"}


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
        "toolCallId": "call-1", "title": "Calling pickup_notification from mhb_devin_sample",
        "rawInput": {"notification_id": notification_id}}}}
    handler = devin_acp._permission_handler(server_name="mhb_devin_sample",
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
        "toolCallId": "call-1", "title": "Calling pickup_notification from other-server",
        "rawInput": {"notification_id": notification_id}}}
    wrong_server = handler(request())
    assert wrong_server["outcome"]["outcome"] == "cancelled"
    pending["call-1"] = {"session_id": session, "call": {
        "toolCallId": "call-1", "title": "Calling pickup_notification from mhb_devin_sample",
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
