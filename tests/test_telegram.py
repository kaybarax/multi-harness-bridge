import asyncio
import json
from types import SimpleNamespace

import pytest

from multi_harness_bridge.store import Store
from multi_harness_bridge.telegram import Route, TelegramBridge, load_config


class FakeAPI:
    def __init__(self, *, failures=0, updates=()):
        self.sent = []
        self.failures = failures
        self.updates = list(updates)

    def send_message(self, chat, text, reply_to=None):
        self.sent.append((chat, text, reply_to))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("simulated timeout after provider acceptance")
        return 900 + len(self.sent)

    def get_updates(self, offset=None, timeout=0):
        assert timeout == 0
        result, self.updates = self.updates, []
        return result


def update(update_id, message_id, text, *, chat=-1001, user=7, reply=None):
    message = {"message_id": message_id, "chat": {"id": chat}, "from": {"id": user}, "text": text}
    if reply is not None:
        message["reply_to_message"] = {"message_id": reply}
    return {"update_id": update_id, "message": message}


def setup(tmp_path, api=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "queue.sqlite3")
    api = api or FakeAPI()
    routes = [Route("shared", "-1001", frozenset({"7"}), "alpha", "claude"),
              Route("shared", "-1001", frozenset({"7"}), "alpha", "codex"),
              Route("shared", "-1001", frozenset({"7"}), "beta", "codex"),
              Route("shared", "-1001", frozenset({"7"}), "beta", "antigravity")]
    bridge = TelegramBridge(tmp_path / "telegram.sqlite3", store, routes, {"shared": api})
    for project, harness in (("alpha", "claude"), ("alpha", "codex"),
                             ("beta", "codex"), ("beta", "antigravity")):
        store.bind(project, harness, "inbox", f"{project}-{harness}-inbox")
        store.bind(project, harness, "coordinator", f"{project}-{harness}-coordinator")
    return bridge, store, api


def claim_roundtrip(store, project, harness):
    inbox = store.claim(project, harness, "inbox", f"{project}-{harness}-inbox")
    assert inbox
    assert store.finish(project, harness, "inbox", f"{project}-{harness}-inbox", inbox["id"], inbox["lease"])
    coordinator = store.claim(project, harness, "coordinator", f"{project}-{harness}-coordinator")
    assert coordinator and coordinator["id"] == inbox["id"]
    assert store.finish(project, harness, "coordinator", f"{project}-{harness}-coordinator",
                        coordinator["id"], coordinator["lease"])
    return coordinator


def test_queue_pma_bot_user_reply_returns_to_origin_harness_with_same_queue_uuid(tmp_path):
    bridge, store, api = setup(tmp_path)

    # Harness sends only to the PMA queue. A regular tick advances PMA's inbox
    # and coordinator stages, then sends the routed notification to Telegram.
    original = store.send("alpha", "codex", "pma", "Build finished", "harness-result")
    assert bridge.deliver_pending("alpha") == 1
    assert api.sent == [("-1001", "[alpha/codex] Build finished", None)]

    # A native Telegram reply to that provider message is recognized by its
    # stored bot/chat/message -> project/harness/queue provenance.
    api.updates = [update(100, 51, "Please continue", reply=901)]
    assert bridge.poll_once("shared") == 1
    reply = claim_roundtrip(store, "alpha", "codex")
    assert reply["sender"] == "pma"
    assert reply["body"] == "Please continue"
    assert reply["reply_to"] == original["id"]


def test_fresh_user_input_routes_to_selected_project_harness_without_human_lease(tmp_path):
    bridge, store, _ = setup(tmp_path)
    assert bridge.receive("shared", update(7, 70, "[beta/antigravity] Review this"))
    assert bridge.deliver_pending("beta") == 1
    delivered = claim_roundtrip(store, "beta", "antigravity")
    assert delivered["sender"] == "pma"
    assert delivered["body"] == "Review this"
    assert delivered["reply_to"] is None
    store.send("beta", "antigravity", "pma", "Review complete", "harness-followup",
               reply_to=delivered["id"])
    assert bridge.deliver_pending("beta") == 1
    assert bridge.apis["shared"].sent[-1] == ("-1001", "[beta/antigravity] Review complete", 70)
    assert store.status("beta", "pma")["inbox"] == 0
    assert store.status("beta", "pma")["coordinator"] == 0


def test_two_projects_shared_chat_dedupe_allowlist_and_ambiguous_routes(tmp_path):
    bridge, store, _ = setup(tmp_path)
    msg = update(40, 50, "[alpha/claude] Please review this")
    assert bridge.receive("shared", msg)
    assert bridge.receive("shared", msg) is False
    assert store.status("alpha", "pma")["inbox"] == 1
    assert store.status("beta", "pma")["inbox"] == 0
    assert bridge.deliver_pending("alpha") == 1
    result = claim_roundtrip(store, "alpha", "claude")
    assert result["body"] == "Please review this"

    with pytest.raises(ValueError, match="allowlisted"):
        bridge.receive("shared", update(41, 51, "[beta/codex] No", user=8))
    with pytest.raises(ValueError, match="ambiguous"):
        bridge.receive("shared", update(42, 52, "[beta] Harness unspecified"))
    bridge.receive("shared", update(43, 53, "[beta/codex] Original"))
    with pytest.raises(ValueError, match="conflicts"):
        bridge.receive("shared", update(43, 53, "[beta/codex] Modified"))


def test_uncertain_provider_send_is_not_acked_or_retried(tmp_path):
    api = FakeAPI(failures=1)
    bridge, store, _ = setup(tmp_path, api)
    store.send("alpha", "codex", "pma", "Maybe delivered", "uncertain")
    with pytest.raises(RuntimeError, match="simulated timeout"):
        bridge.deliver_pending("alpha")
    assert store.status("alpha", "pma")["coordinator"] == 1
    with bridge.connect() as db:
        row = db.execute("SELECT state FROM telegram_sends").fetchone()
    assert row[0] == "uncertain"
    assert bridge.deliver_pending("alpha") == 0
    assert len(api.sent) == 1
    assert store.status("alpha", "pma")["coordinator"] == 1


def test_tick_recovers_coordinator_stage_without_an_inbox_message(tmp_path):
    bridge, store, api = setup(tmp_path)
    queued = store.send("alpha", "codex", "pma", "Recover me", "restart")
    inbox = store.claim("alpha", "pma", "inbox", "pma-inbox-alpha")
    assert inbox["id"] == queued["id"]
    assert store.finish("alpha", "pma", "inbox", "pma-inbox-alpha", inbox["id"], inbox["lease"])
    assert store.status("alpha", "pma")["inbox"] == 0
    assert bridge.tick() == 1
    assert api.sent == [("-1001", "[alpha/codex] Recover me", None)]


def test_poll_advances_rejected_updates_but_propagates_store_value_errors(tmp_path):
    api = FakeAPI(updates=[update(1, 1, "[alpha/codex] Unauthorized", user=8)])
    bridge, store, _ = setup(tmp_path, api)
    assert bridge.poll_once("shared") == 1
    with bridge.connect() as db:
        assert db.execute("SELECT next_offset FROM telegram_polling WHERE bot='shared'").fetchone()[0] == 2

    api.updates = [update(2, 2, "[alpha/codex] Retry me")]
    store.send = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("sender stopped"))
    with pytest.raises(ValueError, match="sender stopped"):
        bridge.poll_once("shared")
    with bridge.connect() as db:
        assert db.execute("SELECT next_offset FROM telegram_polling WHERE bot='shared'").fetchone()[0] == 2


def test_dedicated_bot_configuration_and_hermes_hook(tmp_path):
    config = tmp_path / "telegram.toml"
    config.write_text('''[bots.dedicated]\ntoken_env="DEDICATED_TOKEN"\n\n[[routes]]\nbot="dedicated"\nchat_id="-20"\nallowed_users=["8"]\nproject="beta"\nharness="codex"\n''')
    with pytest.raises(ValueError, match="Missing Telegram"):
        load_config(config, {})
    apis, routes = load_config(config, {"DEDICATED_TOKEN": "not-a-real-token"})
    assert set(apis) == {"dedicated"}
    bridge, store, api = setup(tmp_path / "hermes")

    class Context:
        def register_hook(self, name, hook):
            assert name == "pre_gateway_dispatch"
            self.hook = hook

    context = Context()
    bridge.register_hermes_hook(context, "shared")
    event = SimpleNamespace(source=SimpleNamespace(platform="telegram", chat_id="-1001", user_id="7"),
                            user_id="7", message_id="300", reply_to_message_id=None,
                            text="[alpha/codex] Via Hermes")
    result = asyncio.run(context.hook(event, None))
    assert result["reason"] == "multi-harness-telegram-routed"
    assert store.status("alpha", "pma")["inbox"] == 1
    assert bridge.receive("shared", update(999, 300, "[alpha/codex] Via Hermes")) is False
    assert api.sent == []
