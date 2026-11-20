import io
import json

from multi_harness_bridge.config import register
from multi_harness_bridge.monitor import subscribe
from multi_harness_bridge.store import Store


def test_existing_chat_subscription_emits_id_only_and_exits_on_rotation(tmp_path, monkeypatch):
    root = tmp_path / "orchard"
    root.mkdir()
    cfg = register("orchard", [root], tmp_path/"state")
    store = Store(cfg.database)
    store.bind("orchard", "claude", "coordinator", "one")
    store.bind("orchard", "claude", "inbox", "two")
    expected = store.send("orchard", "codex", "claude", "Private body", "key")
    monkeypatch.chdir(root)
    waits = []
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            store.bind("orchard", "claude", "inbox", "three")
    out = io.StringIO()
    subscribe(cfg, store, "orchard", "claude", "inbox", "two", output=out, wait=wait)
    assert json.loads(out.getvalue()) == {"project":"orchard","harness":"claude","role":"inbox",
                                          "session":"two","id":expected["id"]}
    assert store.status("orchard", "claude")["leased"] == 0
