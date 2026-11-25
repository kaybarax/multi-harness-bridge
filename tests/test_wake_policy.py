"""The shared wake-turn policy: classification, the policy table, the loop guard and every host's adapter."""
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from itertools import permutations

import pytest

from multi_harness_bridge import cli, config, role_adapters, skills, wake, wake_policy, watcher
from multi_harness_bridge.config import Config
from multi_harness_bridge.store import LoopCapReached, Store

HOSTS = ("codex", "claude", "devin")
UUID = "0e1f2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5b"


def _store(tmp_path, harnesses=HOSTS):
    cfg = Config(tmp_path / "state", {"sample": (tmp_path,)})
    cfg.root.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.database)
    for harness in harnesses:
        store.bind("sample", harness, "coordinator", f"{harness}-coordinator")
        store.bind("sample", harness, "inbox", f"{harness}-inbox")
        store.set_enabled("sample", harness, True)
    return cfg, store


def _deliver(store, delivery_id, recipient, sender):
    """Move one free-text delivery through both bridge stages to the recipient's coordinator."""
    for harness in (sender, recipient):
        item = store.claim("sample", harness, "inbox", f"{harness}-inbox", message_id=delivery_id)
        assert store.finish("sample", harness, "inbox", f"{harness}-inbox", delivery_id, item["lease"])


# --- classification ------------------------------------------------------------

def test_every_bridge_text_is_recognised_and_everything_else_is_the_users():
    act = wake_policy.wake_text(wake_policy.ACT, UUID)
    ack = wake_policy.wake_text(wake_policy.ACKNOWLEDGE, UUID)
    assert wake_policy.classify_text(act) == (wake_policy.WAKE, UUID)
    assert wake_policy.classify_text(ack) == (wake_policy.WAKE, UUID)
    assert wake_policy.classify_text(f"Multi Harness Bridge native wake. call pickup_wake {UUID}")[0] == "wake"
    assert wake_policy.classify_text(f"MHB bridge notification {UUID} pending")[0] == "wake"
    assert wake_policy.classify_text("Multi Harness Bridge digest 3 is ready")[0] == "digest"
    assert wake_policy.classify_text("please set the option") == (wake_policy.USER, None)
    assert wake_policy.classify_text(None) == (wake_policy.UNKNOWN, None)


@pytest.mark.parametrize(("text", "trigger", "kind"), [
    ("take coordinator", "composer", "user"),
    ("<context source=goal> continue", "goal", "user"),
    (wake_policy.wake_text("act", UUID), "queue", "wake"),
    ("an older tool's queued notice", "queue", "unknown"),
    ("a message from another thread", "app_tool_send_message", "unknown"),
    ("typed text", None, "unknown"),
])
def test_codex_turns_are_users_only_on_positive_evidence(text, trigger, kind):
    assert wake_policy.classify("codex", text=text, trigger=trigger)[0] == kind


def test_codex_transcript_reader_takes_the_latest_turns_trigger(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    rollout = home / "sessions" / "r.jsonl"
    rollout.parent.mkdir(parents=True)
    thread = "019a0000-0000-7000-8000-000000000001"
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT)")
        db.execute("INSERT INTO threads VALUES(?,?)", (thread, str(rollout)))

    def turn(trigger, text):
        return [json.dumps({"type": "event_msg", "payload": {"type": "task_started",
                                                              "turn_attribution": {"turn_trigger": trigger}}}),
                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                                                                 "content": [{"type": "input_text", "text": text}]}})]
    rollout.write_text("\n".join(turn("composer", "take coordinator")
                                 + turn("queue", wake_policy.wake_text("act", UUID))) + "\n")
    text, trigger = role_adapters.codex_turn(thread_id=thread, home=home)
    assert trigger == "queue" and wake_policy.classify("codex", text=text, trigger=trigger)[0] == "wake"


def test_claude_turn_is_the_users_only_when_typed_after_the_last_hint(tmp_path):
    root = tmp_path / "state"
    args = {"root": root, "project": "sample", "harness": "claude", "session": "s"}
    assert wake_policy.classify("claude", prompt="hello", prompt_at=time.time(), **args)[0] == "user"
    wake_policy.record_hint(root, "sample", "claude", "s", [UUID], clock=lambda: 200.0)
    assert wake_policy.classify("claude", prompt="hello", prompt_at=100.0, **args) == ("wake", (UUID,))
    assert wake_policy.classify("claude", prompt="hello", prompt_at=300.0, **args)[0] == "user"
    assert wake_policy.classify("claude", prompt="hello", prompt_at=None, **args)[0] == "unknown"


def test_a_failing_turn_source_fails_closed(monkeypatch):
    def broken(**_):
        raise OSError("transcript gone")
    monkeypatch.setitem(wake_policy._TURN_SOURCES, "codex", broken)
    assert wake_policy.classify("codex") == (wake_policy.UNKNOWN, None)
    assert wake_policy.classify("unregistered-host") == (wake_policy.UNKNOWN, None)


# --- the policy table ------------------------------------------------------------

@pytest.mark.parametrize("kind", ["wake", "digest", "unknown"])
@pytest.mark.parametrize("action", ["role", "settings", "propagate"])
@pytest.mark.parametrize("mode", ["act", "acknowledge"])
def test_role_settings_and_skills_need_a_turn_the_user_started(kind, action, mode):
    ok, reason = wake_policy.allowed(mode, kind, action)
    assert not ok and "never the user's approval" in reason


@pytest.mark.parametrize("kind", ["user", "operator"])
@pytest.mark.parametrize("action", wake_policy.ACTIONS)
def test_user_and_operator_turns_are_unchanged(kind, action):
    assert wake_policy.allowed("acknowledge", kind, action)[0]


def test_act_lets_a_wake_read_reply_and_send_while_acknowledge_keeps_one_answer():
    for action in ("read", "finish", "reply", "send"):
        assert wake_policy.allowed("act", "wake", action)[0]
    assert wake_policy.allowed("acknowledge", "wake", "finish")[0]
    assert not wake_policy.allowed("acknowledge", "wake", "send")[0]
    assert wake_policy.allowed("acknowledge", "wake", "reply", reply_to=UUID, woken=UUID)[0]
    assert wake_policy.allowed("acknowledge", "wake", "reply", reply_to=UUID, woken=(UUID,))[0]
    assert not wake_policy.allowed("acknowledge", "wake", "reply", reply_to="other", woken=UUID)[0]
    assert wake_policy.allowed("act", "digest", "read")[0]
    assert not wake_policy.allowed("act", "digest", "send")[0]


def test_settings_default_to_act_for_classified_hosts_and_acknowledge_elsewhere(tmp_path):
    root = tmp_path
    for host in HOSTS:
        assert wake_policy.mode(root, "sample", host) == "act"
    assert wake_policy.mode(root, "sample", "antigravity") == "acknowledge"
    config.set_project_option(root, "sample", "codex", "wake_actions", "acknowledge")
    config.set_project_option(root, "sample", "antigravity", "wake_actions", "act")
    assert wake_policy.mode(root, "sample", "codex") == "acknowledge"
    assert wake_policy.mode(root, "sample", "antigravity") == "acknowledge"
    config.set_project_option(root, "sample", "devin", "loop_cap_messages", "3")
    config.set_project_option(root, "sample", "codex", "loop_cap_minutes", "60")
    assert wake_policy.loop_cap(root, "sample", "codex", "devin") == (3, 3600)
    config.set_project_option(root, "sample", "codex", "loop_cap_messages", "0")
    assert wake_policy.settings(root, "sample", "codex")["loop_cap_messages"] == 10


# --- the loop guard (store) --------------------------------------------------------

def test_loop_cap_trips_for_wake_messages_in_both_directions_and_records_an_event(tmp_path):
    cfg, store = _store(tmp_path)
    config.set_project_option(cfg.root, "sample", "codex", "loop_cap_messages", "3")
    store.send_fanout("sample", "codex", ["devin"], "one", "k1", session="codex-coordinator", origin="wake")
    store.send_fanout("sample", "devin", ["codex"], "two", "k2", session="devin-coordinator", origin="wake")
    store.send_fanout("sample", "codex", ["devin"], "typed", "k3", session="codex-coordinator", origin="user")
    store.send_fanout("sample", "codex", ["devin"], "three", "k4", session="codex-coordinator")  # unstated: wake
    with pytest.raises(LoopCapReached, match="Loop cap reached"):
        store.send_fanout("sample", "devin", ["codex"], "four", "k5", session="devin-coordinator", origin="wake")
    # Another pair, the user, and a retry of an accepted key are not affected.
    store.send_fanout("sample", "codex", ["claude"], "other pair", "k6", session="codex-coordinator", origin="wake")
    store.send_fanout("sample", "devin", ["codex"], "typed", "k7", session="devin-coordinator", origin="user")
    assert store.send_fanout("sample", "codex", ["devin"], "one", "k1", session="codex-coordinator",
                             origin="wake")["deliveries"]
    events = wake.pickup_events(store, "sample")
    assert [e["event"] for e in events] == ["loop-cap"]
    assert json.loads(events[0]["detail"]) == {"recipient": "codex", "limit": 3, "window_seconds": 1800}


def test_ack_only_reply_is_done_at_once_and_wakes_nobody(tmp_path):
    cfg, store = _store(tmp_path)
    request = store.send_fanout("sample", "codex", ["devin"], "Please confirm", "req",
                                session="codex-coordinator", origin="user")
    delivery = request["deliveries"][0]["id"]
    _deliver(store, delivery, "devin", "codex")
    item = store.claim("sample", "devin", "coordinator", "devin-coordinator", message_id=delivery)
    assert store.finish("sample", "devin", "coordinator", "devin-coordinator", delivery, item["lease"])
    reply = store.reply_free_text("sample", "devin", delivery, "Received.", "ack", session="devin-coordinator",
                                  origin="wake", ack_only=True)
    assert store.trace("sample", reply["id"])["stage"] == "done"
    for harness, role in (("devin", "inbox"), ("codex", "inbox"), ("codex", "coordinator")):
        assert store.pending_details("sample", harness, role, f"{harness}-{role}") == []
    assert store.message_meta("sample", reply["id"]) == {"origin": "wake", "ack_only": True}


def test_ack_only_is_a_short_reply_only(tmp_path):
    cfg, store = _store(tmp_path)
    with pytest.raises(ValueError, match="reply that only confirms receipt"):
        store.send_fanout("sample", "codex", ["devin"], "hi", "k", session="codex-coordinator", ack_only=True)
    request = store.send_fanout("sample", "codex", ["devin"], "Q", "q", session="codex-coordinator", origin="user")
    delivery = request["deliveries"][0]["id"]
    _deliver(store, delivery, "devin", "codex")
    item = store.claim("sample", "devin", "coordinator", "devin-coordinator", message_id=delivery)
    with pytest.raises(ValueError, match="at most 280"):
        store.finish_and_reply("sample", "devin", "devin-coordinator", delivery, item["lease"], "x" * 281, "r",
                               ack_only=True)


# --- key cases (a)-(e) through the hosts' own entry points ------------------------

def _codex_turn(monkeypatch, text, trigger):
    monkeypatch.setattr(role_adapters, "codex_turn", lambda **_: (text, trigger))


def _bind_cli(monkeypatch, cfg, host, session):
    monkeypatch.setattr(skills.config, "load", lambda: cfg)
    monkeypatch.setattr(skills, "invoking_harness", lambda: host)
    monkeypatch.setattr(skills, "_native_invocation", lambda h, hint=None, **_: role_adapters.Invocation(
        "sample", host, session, "", cfg.projects["sample"][0], proof="fixture"))


def _run_cli(capsys, argv):
    skills.main(argv)
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(("asker", "answerer"), list(permutations(HOSTS, 2)))
def test_b_every_ordered_pair_answers_and_messages_from_a_wake_turn(tmp_path, monkeypatch, capsys, asker, answerer):
    """(a)/(b): the asker sends while nobody types at the answerer; the answerer's wake turn
    claims, replies and sends new work to a third harness, all counted as wake-started."""
    cfg, store = _store(tmp_path)
    request = store.send_fanout("sample", asker, [answerer], "What is the status?", "ask",
                                session=f"{asker}-coordinator", origin="user")
    delivery = request["deliveries"][0]["id"]
    _deliver(store, delivery, answerer, asker)
    third = next(h for h in HOSTS if h not in (asker, answerer))
    item = store.claim("sample", answerer, "coordinator", f"{answerer}-coordinator", message_id=delivery)
    assert store.finish("sample", answerer, "coordinator", f"{answerer}-coordinator", delivery, item["lease"])
    kind = wake_policy.WAKE
    for action in ("reply", "send"):
        wake_policy.check(cfg.root, "sample", answerer, kind, action)
    reply = store.reply_free_text("sample", answerer, delivery, "Green; continuing.", "answer",
                                  session=f"{answerer}-coordinator", origin=wake_policy.origin(kind))
    onward = store.send_fanout("sample", answerer, [third], "Please take the next step.", "onward",
                               session=f"{answerer}-coordinator", origin=wake_policy.origin(kind))
    assert store.trace("sample", reply["id"])["recipient"] == asker
    assert store.message_meta("sample", onward["deliveries"][0]["id"])["origin"] == "wake"
    with pytest.raises(wake_policy.WakePolicyRefused):
        wake_policy.check(cfg.root, "sample", answerer, kind, "settings")


def test_a_codex_wake_turn_answers_and_messages_through_the_cli(tmp_path, monkeypatch, capsys):
    cfg, store = _store(tmp_path)
    request = store.send_fanout("sample", "devin", ["codex"], "Question?", "q",
                                session="devin-coordinator", origin="user")
    delivery = request["deliveries"][0]["id"]
    _deliver(store, delivery, "codex", "devin")
    _bind_cli(monkeypatch, cfg, "codex", "codex-coordinator")
    _codex_turn(monkeypatch, wake_policy.wake_text("act", delivery), "queue")
    claim = _run_cli(capsys, ["--mailbox", "claim", "--message-id", delivery])
    _run_cli(capsys, ["--mailbox", "finish", "--message-id", delivery, "--lease", claim["message"]["lease"]])
    reply = _run_cli(capsys, ["--mailbox", "reply", "--reply-to", delivery, "--body", "Answer", "--key", "a"])
    sent = _run_cli(capsys, ["--mailbox", "send-fanout", "--recipient", "claude", "--body", "Next", "--key", "n"])
    assert store.message_meta("sample", reply["id"])["origin"] == "wake"
    assert store.message_meta("sample", sent["deliveries"][0]["id"])["origin"] == "wake"


@pytest.mark.parametrize("argv", [["--mailbox", "stop"], ["--mailbox", "resume"],
                                  ["--mailbox", "digest", "--digest-action", "off"]])
def test_c_a_peer_cannot_change_settings_from_a_codex_wake(tmp_path, monkeypatch, capsys, argv):
    cfg, store = _store(tmp_path)
    _bind_cli(monkeypatch, cfg, "codex", "codex-coordinator" if "digest" not in argv else "codex-inbox")
    _codex_turn(monkeypatch, wake_policy.wake_text("act", UUID), "queue")
    with pytest.raises(SystemExit):
        skills.main(argv)
    assert "wake policy" in capsys.readouterr().err
    _codex_turn(monkeypatch, "stop the bridge", "composer")
    skills.main(argv)  # the same request typed by the person goes through


def test_c_propagation_and_takeover_are_refused_from_wakes(tmp_path, monkeypatch, capsys):
    cfg, _ = _store(tmp_path)
    monkeypatch.setattr(skills.config, "load", lambda: cfg)
    monkeypatch.setattr(skills, "invoking_harness", lambda: "codex")
    _codex_turn(monkeypatch, wake_policy.wake_text("act", UUID), "queue")
    with pytest.raises(wake_policy.WakePolicyRefused):
        skills.enforce(cfg, "codex", "sample", "propagate")
    with pytest.raises(wake_policy.WakePolicyRefused):
        skills.enforce(cfg, "codex", "sample", "role")


def test_c_operator_cli_refuses_rebinding_and_options_from_a_wake(tmp_path, monkeypatch, capsys):
    cfg, store = _store(tmp_path)
    monkeypatch.setattr(cli.settings, "load", lambda: cfg)
    monkeypatch.setattr(skills.config, "load", lambda: cfg)
    monkeypatch.setattr(skills, "invoking_harness", lambda: "codex")
    _codex_turn(monkeypatch, wake_policy.wake_text("act", UUID), "queue")
    parser = cli.build_parser()
    for argv in (["bind", "sample", "codex", "coordinator", "intruder"],
                 ["option", "set", "sample", "codex", "wake_actions", "act"],
                 ["stop", "sample", "codex"]):
        with pytest.raises(wake_policy.WakePolicyRefused):
            cli.run(parser.parse_args(argv))
    assert store.roles("sample", "codex")["coordinator"] == "codex-coordinator"
    # A plain terminal (no host session) is the operator and is unchanged.
    monkeypatch.setattr(skills, "invoking_harness", lambda: (_ for _ in ()).throw(
        ValueError("No trusted native harness identity; invoke through that harness's registered adapter")))
    cli.run(parser.parse_args(["option", "set", "sample", "codex", "loop_cap_messages", "5"]))
    assert config.project_option(cfg.root, "sample", "codex", "loop_cap_messages") == "5"


def test_c_unidentifiable_host_session_cannot_change_settings(tmp_path, monkeypatch):
    cfg, _ = _store(tmp_path)
    monkeypatch.setattr(skills, "invoking_harness", lambda: (_ for _ in ()).throw(
        ValueError("Multiple native harness identities are active")))
    with pytest.raises(wake_policy.WakePolicyRefused):
        skills.enforce_caller(cfg, "sample", "settings")
    assert skills.enforce_caller(cfg, "sample", "send")[0] == "unknown"


def test_acknowledge_mode_keeps_wakes_to_one_answer(tmp_path, monkeypatch, capsys):
    cfg, store = _store(tmp_path)
    config.set_project_option(cfg.root, "sample", "codex", "wake_actions", "acknowledge")
    request = store.send_fanout("sample", "devin", ["codex"], "Question?", "q",
                                session="devin-coordinator", origin="user")
    delivery = request["deliveries"][0]["id"]
    _deliver(store, delivery, "codex", "devin")
    _bind_cli(monkeypatch, cfg, "codex", "codex-coordinator")
    _codex_turn(monkeypatch, wake_policy.wake_text("acknowledge", delivery), "queue")
    claim = _run_cli(capsys, ["--mailbox", "claim", "--message-id", delivery])
    _run_cli(capsys, ["--mailbox", "finish", "--message-id", delivery, "--lease", claim["message"]["lease"]])
    _run_cli(capsys, ["--mailbox", "reply", "--reply-to", delivery, "--body", "Received", "--key", "a",
                      "--ack-only"])
    with pytest.raises(SystemExit):
        skills.main(["--mailbox", "send-fanout", "--recipient", "claude", "--body", "x", "--key", "n"])
    assert "acknowledg" in capsys.readouterr().err


def test_codex_wake_text_follows_the_projects_setting(tmp_path, monkeypatch):
    cfg, _ = _store(tmp_path)
    monkeypatch.setattr(wake.config, "load", lambda: cfg)
    values = {"project": "sample", "role": "coordinator", "notification_id": UUID}
    assert wake._wake_text("codex", values) == wake_policy.wake_text("act", UUID)
    assert wake._wake_text("codex", {**values, "role": "inbox"}) == wake.WAKE_PROMPT.format(notification_id=UUID)
    config.set_project_option(cfg.root, "sample", "codex", "wake_actions", "acknowledge")
    assert wake._wake_text("codex", values) == wake.WAKE_PROMPT.format(notification_id=UUID)
    assert wake.WAKE_PROMPT.startswith("A new Multi Harness Bridge message is pending (notification {")


def test_d_loop_cap_alert_reaches_the_bridge_chat_once_per_window(tmp_path, monkeypatch):
    cfg, store = _store(tmp_path)
    config.set_project_option(cfg.root, "sample", "codex", "loop_cap_messages", "1")
    store.send_fanout("sample", "codex", ["devin"], "one", "k1", session="codex-coordinator", origin="wake")
    for key in ("k2", "k3"):
        with pytest.raises(LoopCapReached):
            store.send_fanout("sample", "codex", ["devin"], "again", key, session="codex-coordinator",
                              origin="wake")
    with closing(sqlite3.connect(store.path)) as db, db:  # trips on both sides of a time boundary
        db.execute("UPDATE mhb_pickup_events SET at=at+1800 WHERE rowid=(SELECT MAX(rowid) FROM mhb_pickup_events)")
    state, logged = {}, []
    watcher._alert_pickup_events(cfg, store, "sample", state, logged.append)
    alerts = [e for e in logged if e.get("reason") == "loop-cap"]
    assert len(alerts) == 1 and alerts[0]["pair"] == "codex-devin"
    feed = watcher.paths(cfg.root, "sample")["feed"].read_text()
    assert "loop cap" in feed and "codex and devin" in feed
    (key, record), = state["alerts"].items()
    assert '"' not in record["external_text"]  # no chat titles leave the machine
    monkeypatch.setattr(watcher.time, "time", lambda: record["expires_at"] + 1)
    watcher._clear_resolved_alerts(store, "sample", state)
    assert state["alerts"] == {}


def test_e_acknowledgements_do_not_wake_the_listener(tmp_path, monkeypatch):
    import io
    from multi_harness_bridge import pickup
    cfg, store = _store(tmp_path)
    request = store.send_fanout("sample", "claude", ["devin"], "Q", "q", session="claude-coordinator",
                                origin="user")
    delivery = request["deliveries"][0]["id"]
    _deliver(store, delivery, "devin", "claude")
    item = store.claim("sample", "devin", "coordinator", "devin-coordinator", message_id=delivery)
    store.finish_and_reply("sample", "devin", "devin-coordinator", delivery, item["lease"], "Got it.", "ack",
                           origin="wake", ack_only=True)
    store.forward_pull("sample", "claude")
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    pickup.listen(cfg, store, "sample", "claude", "claude-coordinator", output=out, max_cycles=1,
                  wait=lambda _: None)
    assert out.getvalue() == ""
    assert wake_policy.last_hint(cfg.root, "sample", "claude", "claude-coordinator") is None


def test_listener_marks_the_hint_before_printing_it(tmp_path, monkeypatch):
    import io
    from multi_harness_bridge import pickup
    cfg, store = _store(tmp_path)
    request = store.send_fanout("sample", "devin", ["claude"], "Q", "q", session="devin-coordinator",
                                origin="user")
    delivery = request["deliveries"][0]["id"]
    _deliver(store, delivery, "claude", "devin")
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    pickup.listen(cfg, store, "sample", "claude", "claude-coordinator", output=out, max_cycles=1,
                  wait=lambda _: None)
    assert delivery in out.getvalue()
    hinted = wake_policy.last_hint(cfg.root, "sample", "claude", "claude-coordinator")
    assert hinted is not None
    kind, woken = wake_policy.classify("claude", root=cfg.root, project="sample", harness="claude",
                                       session="claude-coordinator", prompt="earlier", prompt_at=hinted - 1)
    assert kind == "wake" and woken == (delivery,)


# --- Devin: tool layer, ACP permission handler and the turn runner -----------------

def _devin_rig(tmp_path, monkeypatch):
    from multi_harness_bridge import devin_tools
    pytest.importorskip("mcp")
    cfg, store = _store(tmp_path)
    monkeypatch.setattr(devin_tools.config, "load", lambda: cfg)
    native = tmp_path / "sessions.sqlite3"
    monkeypatch.setenv("MHB_DEVIN_SESSIONS_DB", str(native))
    store.bind("sample", "devin", "coordinator", "acp/devin-cli/dv")
    with sqlite3.connect(native) as db:
        db.execute("CREATE TABLE sessions(id TEXT,working_directory TEXT,workspace_dirs TEXT,title TEXT,hidden INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE message_nodes(session_id TEXT,row_id INTEGER,chat_message TEXT,created_at REAL)")
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,0)", ("dv", str(tmp_path), "[]", "Coordinator"))
    rows = iter(range(1, 10_000))

    def add(payload):
        now = datetime.now(timezone.utc)
        with sqlite3.connect(native) as db:
            db.execute("INSERT INTO message_nodes VALUES(?,?,?,?)", ("dv", next(rows), json.dumps(
                {**payload, "metadata": {"created_at": now.isoformat()}}), int(now.timestamp())))

    def call(call_id, tool, args):
        add({"role": "assistant", "tool_calls": [{"id": call_id, "name": "mcp_call_tool", "arguments": {
            "server_name": "mhb_roles_sample", "tool_name": tool, "arguments": args}}]})

    server = devin_tools.make_role_server(cfg, "sample")
    return cfg, store, server, add, call, native


def test_devin_wake_turn_may_message_after_pickup_but_never_change_settings(tmp_path, monkeypatch):
    from multi_harness_bridge import devin_acp
    cfg, store, server, add, call, _ = _devin_rig(tmp_path, monkeypatch)
    request = store.send_fanout("sample", "codex", ["devin"], "Q", "q", session="codex-coordinator", origin="user")
    delivery = request["deliveries"][0]["id"]
    _deliver(store, delivery, "devin", "codex")
    add({"role": "user", "content": devin_acp._wake_prompt("mhb_roles_sample", delivery, True, "act")})
    fanout = server._tool_manager.get_tool("send_fanout").fn
    set_pickup = server._tool_manager.get_tool("set_pickup").fn
    args = {"recipients": ["claude"], "body": "Next step", "key": "onward"}
    call("c1", "send_fanout", args)
    with pytest.raises(PermissionError, match="pick up this wake's notification"):
        fanout(**args)
    add({"role": "tool", "tool_call_id": "c1", "content": "refused"})
    store.claim("sample", "devin", "coordinator", "acp/devin-cli/dv", message_id=delivery)
    call("c2", "send_fanout", args)
    sent = fanout(**args)
    assert store.message_meta("sample", sent["deliveries"][0]["id"])["origin"] == "wake"
    call("c3", "set_pickup", {"enabled": False})
    with pytest.raises(PermissionError, match="wake policy"):
        set_pickup(enabled=False)
    assert store.status("sample", "devin")["enabled"]


def test_devin_turn_without_a_visible_prompt_cannot_change_settings(tmp_path, monkeypatch):
    cfg, store, server, add, call, _ = _devin_rig(tmp_path, monkeypatch)
    set_pickup = server._tool_manager.get_tool("set_pickup").fn
    call("c1", "set_pickup", {"enabled": False})
    with pytest.raises(PermissionError, match="unknown turn"):
        set_pickup(enabled=False)
    add({"role": "tool", "tool_call_id": "c1", "content": "refused"})
    add({"role": "user", "content": "please pause the bridge"})
    call("c2", "set_pickup", {"enabled": False})
    assert set_pickup(enabled=False) == {"enabled": False}


def _permission_event(call_id, title):
    return {"method": "session/request_permission", "id": 9, "params": {
        "toolCallId": call_id, "toolCall": {"toolCallId": call_id, "title": title},
        "options": [{"optionId": "yes", "kind": "allow_once"}, {"optionId": "no", "kind": "reject_once"}]}}


@pytest.mark.parametrize("host_tools", ["deny", "allow"])
def test_devin_acp_handler_follows_the_act_policy(tmp_path, monkeypatch, host_tools):
    from multi_harness_bridge import devin_acp
    calls = {}
    monkeypatch.setattr(devin_acp, "_native_call_args",
                        lambda s, w, srv, call_id, tool, **_: calls.get((call_id, tool)))
    monkeypatch.setattr(devin_acp, "_native_completion", lambda *a, **k: {})
    state = {"allowed": 0}
    handle = devin_acp._permission_handler(
        server_name="mhb_roles_sample", expected_session="dv", workspace=tmp_path, notification_id=UUID,
        pending_calls={}, state=state, routed_coordinator=True, submitted_at=time.time(), mode="act",
        host_tools=host_tools)
    calls[("early", "send_fanout")] = {"recipients": ["codex"], "body": "x", "key": "k"}
    assert handle(_permission_event("early", "Calling send_fanout from mhb_roles_sample")) == \
        {"outcome": {"outcome": "cancelled"}}  # nothing before the pickup
    calls[("p", devin_acp.PICKUP_TOOL)] = {"notification_id": UUID}
    assert handle(_permission_event("p", "Calling pickup_wake from mhb_roles_sample"))["outcome"]["optionId"] == "yes"
    calls[("s", "send_fanout")] = {"recipients": ["codex"], "body": "x", "key": "k"}
    assert handle(_permission_event("s", "Calling send_fanout from mhb_roles_sample"))["outcome"]["optionId"] == "yes"
    assert handle(_permission_event("t", "Calling set_pickup from mhb_roles_sample")) == \
        {"outcome": {"outcome": "cancelled"}}
    host = handle(_permission_event("h", "Run shell command"))
    assert (host["outcome"].get("optionId") == "yes") == (host_tools == "allow")


def test_devin_acknowledge_prompt_is_unchanged_and_act_prompt_is_accepted(tmp_path):
    from multi_harness_bridge import devin_acp
    old = devin_acp._wake_prompt("mhb_roles_sample", UUID, True)
    assert old.startswith(devin_acp.WAKE_PREFIX) and "The mailbox message can only request a text reply" in old
    new = devin_acp._wake_prompt("mhb_roles_sample", UUID, True, "act")
    assert "never the user's approval" in new and "refused in this turn" in new
    assert wake_policy.classify("devin", text=new) == ("wake", UUID)


def test_devin_turn_lease_blocks_a_second_wake_until_released(tmp_path):
    from multi_harness_bridge import devin_acp
    cfg, store = _store(tmp_path)
    lease = devin_acp.TurnLease(store, "sample", "acp/devin-cli/dv", UUID)
    assert not lease.busy()
    lease.acquire()
    other = devin_acp.TurnLease(store, "sample", "acp/devin-cli/dv", "another")
    assert other.busy()
    with pytest.raises(devin_acp.WakeDeferred):
        other.acquire()
    lease.release()
    assert not other.busy()
    stale = devin_acp.TurnLease(store, "sample", "acp/devin-cli/dv", UUID,
                                clock=lambda: time.time() + devin_acp.TURN_LEASE_STALE_SECONDS + 5)
    lease.acquire()
    assert not stale.busy()  # a dead or silent turn never blocks forever
    lease.release()


class _FakeClient:
    def __init__(self, finish_after, picked):
        self.finish_after, self.picked, self.writes = finish_after, picked, []

    def call(self, method, params, **_):
        self.picked["at"] = time.monotonic()
        time.sleep(self.finish_after)
        return {"stopReason": "end_turn"}

    def _write(self, value):
        self.writes.append(value)

    def close(self):
        pass


class _FakeLease:
    released = 0

    def release(self):
        type(self).released += 1


def test_devin_act_turn_hands_off_after_pickup_and_cleans_up_once(monkeypatch):
    from multi_harness_bridge import devin_acp
    stopped = []
    monkeypatch.setattr(devin_acp, "_stop_process", lambda proc: stopped.append(proc))
    picked = {}
    client = _FakeClient(0.6, picked)
    lease = _FakeLease()
    _FakeLease.released = 0
    result = devin_acp._run_act_turn(client, "proc", lease, "dv", "prompt", None, {},
                                     confirm=lambda: "at" in picked, window=5)
    assert result is None and stopped == []  # still running in its own thread
    deadline = time.monotonic() + 5
    while not stopped and time.monotonic() < deadline:
        time.sleep(0.05)
    assert stopped == ["proc"] and _FakeLease.released == 1


def test_devin_act_turn_without_pickup_is_cancelled(monkeypatch):
    from multi_harness_bridge import devin_acp
    monkeypatch.setattr(devin_acp, "_stop_process", lambda proc: None)
    client = _FakeClient(3.0, {})
    with pytest.raises(TimeoutError):
        devin_acp._run_act_turn(client, "proc", _FakeLease(), "dv", "prompt", None, {},
                                confirm=lambda: False, window=1)
    assert any(w.get("method") == "session/cancel" for w in client.writes)


# --- Antigravity and the credential transport --------------------------------------

def test_antigravity_stays_acknowledge_and_its_wakes_cannot_change_settings(tmp_path):
    cfg, _ = _store(tmp_path, harnesses=("antigravity", "codex"))
    kind, woken = wake_policy.classify("antigravity", text=wake.WAKE_PROMPT.format(notification_id=UUID))
    assert (kind, woken) == ("wake", UUID)
    for action in ("settings", "role", "propagate", "send"):
        with pytest.raises(wake_policy.WakePolicyRefused):
            wake_policy.check(cfg.root, "sample", "antigravity", kind, wake_policy.tool_action(
                {"settings": "set_pickup", "role": "take_coordinator", "propagate": "propagate_skills",
                 "send": "send_fanout"}[action]))
    wake_policy.check(cfg.root, "sample", "antigravity", kind, wake_policy.tool_action(
        "reply_free_text", {"reply_to": UUID}), reply_to=UUID, woken=woken)


def test_remote_transport_callers_may_message_but_never_change_settings(tmp_path, monkeypatch):
    from multi_harness_bridge import transport
    cfg, store = _store(tmp_path)
    monkeypatch.setattr(transport, "open_store", lambda _: store)
    owner = transport.Principal("sample", "codex", "coordinator", "codex-coordinator")
    monkeypatch.setattr(transport, "authenticate", lambda *_: owner)
    monkeypatch.setattr(transport, "verify_role", lambda *_: None)
    with pytest.raises(wake_policy.WakePolicyRefused):
        transport.dispatch(cfg, "t" * 43, {"op": "stop"}, remote=True)
    result = transport.dispatch(cfg, "t" * 43, {"op": "send_fanout", "recipients": ["devin"], "body": "hi",
                                                "key": "k"}, remote=True)
    assert store.message_meta("sample", result["deliveries"][0]["id"])["origin"] == "wake"


def test_devin_finish_records_receipt_even_when_the_reply_hits_the_loop_cap(tmp_path):
    from multi_harness_bridge import devin_acp
    cfg, store = _store(tmp_path)
    store.bind("sample", "devin", "coordinator", "dv")
    config.set_project_option(cfg.root, "sample", "devin", "loop_cap_messages", "1")
    store.send_fanout("sample", "devin", ["codex"], "earlier", "e", session="dv", origin="wake")
    request = store.send_fanout("sample", "codex", ["devin"], "Q", "q", session="codex-coordinator", origin="user")
    delivery = request["deliveries"][0]["id"]
    _deliver(store, delivery, "devin", "codex")
    receipt = devin_acp._pickup_notification(store, "sample", "coordinator", "dv", delivery)
    result = devin_acp._finish_notification(store, "sample", "dv", delivery, receipt, delivery, receipt["lease"],
                                            "Answer", origin="wake")
    assert result["acknowledged"] and "Loop cap reached" in result["reply_refused"]
    assert store.trace("sample", delivery)["stage"] == "done"
