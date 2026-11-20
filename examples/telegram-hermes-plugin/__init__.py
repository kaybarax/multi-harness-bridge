"""Installable Hermes hook adapter for Multi Harness Bridge Telegram routing."""

import os
from pathlib import Path

from multi_harness_bridge.config import load as load_registry
from multi_harness_bridge.store import Store
from multi_harness_bridge.telegram import TelegramBridge, load_config


def register(ctx):
    config_value = os.environ.get("MHB_TELEGRAM_CONFIG")
    state_value = os.environ.get("MHB_TELEGRAM_STATE")
    if not config_value:
        raise RuntimeError("Set MHB_TELEGRAM_CONFIG to the Telegram route TOML")
    if not state_value:
        raise RuntimeError("Set MHB_TELEGRAM_STATE to a writable Telegram state DB path")
    config_path = Path(config_value).expanduser()
    state_path = Path(state_value).expanduser()
    registry = load_registry()
    apis, routes = load_config(config_path)
    bridge = TelegramBridge(state_path, Store(registry.database), routes, apis)
    bot_alias = os.environ.get("MHB_TELEGRAM_HERMES_BOT", "shared")
    if bot_alias not in apis:
        raise RuntimeError("MHB_TELEGRAM_HERMES_BOT does not name a configured Telegram bot")
    bridge.register_hermes_hook(ctx, bot_alias)
    # The bound hook retains the bridge for the gateway lifetime. Hermes owns
    # getUpdates; a queue-only sidecar can drain PMA work without polling.
    return bridge
