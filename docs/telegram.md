# Telegram and PMA routing

`multi-harness-telegram` joins allowlisted Telegram chats to the shared SQLite
project queue. Its PMA worker consumes both PMA queue stages without a
human-held lease. Harness messages addressed to `pma` appear in Telegram with
the exact first line `Mhb: Bridge Message`, followed by their project and
harness label. Telegram messages enter the PMA inbox and are
forwarded to the selected harness. A reply to a bot message returns to the
original harness with the originating queue UUID in `reply_to`.

## Configure

Choose one complete example: [shared bot](../examples/telegram-shared.toml) or
[dedicated bots](../examples/telegram-dedicated.toml). Replace the sample
project slugs, numeric chat IDs, numeric user IDs and harness names. Set each
`token_env` variable in the service environment; bot tokens never go in TOML.
A route is authorized only when its bot, chat and sender all match.

For a shared bot/chat with more than one destination, prefix new messages with
`[project-slug]` or `[project-slug/harness]`. A reply to a bot message uses its
stored project/harness provenance and can omit the prefix. Unqualified
ambiguous messages are rejected. A dedicated chat that resolves to one
destination can omit the prefix.

## Standalone polling mode

The standalone service is the sole `getUpdates` poller for its configured bot
or bots. It uses short polls for each bot so a quiet bot cannot hold up other
bots or the outbound queue.

```sh
multi-harness-telegram \
  --config /path/to/telegram-shared.toml \
  --database "$MHB_HOME/bridge.sqlite3" \
  --telegram-state "$MHB_HOME/telegram.sqlite3" \
  --mode poll
```

The service binds PMA inbox/coordinator sessions per project, with distinct
session names. Its regular tick advances PMA inbox messages to coordinator,
then routes the coordinator work: `sender=telegram` is sent to the selected
harness, while a configured harness sender is posted to Telegram with
`[project/harness]` provenance. Do not run another poller for the same bot.

## Hermes-owned polling mode

Install `multi-harness-bridge` in the Hermes runtime, copy the example plugin
directory to `$HERMES_HOME/plugins/multi-harness-telegram/`, and enable it in
the profile.
The Hermes profile's own Telegram adapter remains the only `getUpdates` poller.
Set `MHB_TELEGRAM_CONFIG`, `MHB_TELEGRAM_STATE`, and optionally
`MHB_TELEGRAM_HERMES_BOT` in that profile's environment. Then run the same
service in queue-only mode; it sends through Bot API but never polls updates:

```sh
multi-harness-telegram \
  --config /path/to/telegram-shared.toml \
  --database "$MHB_HOME/bridge.sqlite3" \
  --telegram-state "$MHB_HOME/telegram.sqlite3" \
  --mode queue-only
```

The plugin is in
[`examples/telegram-hermes-plugin`](../examples/telegram-hermes-plugin). It
registers a `pre_gateway_dispatch` hook that validates the Hermes event against
the same allowlists and stores it in the public queue. The queue-only service
performs PMA claim, forwarding, delivery and acknowledgements.

## Delivery and recovery

Each inbound Bot API update has a stable dedupe key and content fingerprint.
The update offset advances only after accepted or explicitly rejected input;
queue or storage errors propagate so the same update can be retried. Native
Telegram replies map through persisted `(bot, chat, provider message ID)` rows
to their project, harness and source queue UUID.

Outbound provider sends persist an `uncertain` record before calling Telegram.
If the call may have succeeded but no receipt returns, the PMA coordinator
message stays leased and unacknowledged. Later ticks do not retry the provider
send automatically. Reconcile the Telegram conversation and local receipt
before resolving it. Confirmed sends advance the queue through its coordinator
stage. After a restart, expired coordinator leases are claimed independently
even when the PMA inbox is empty.

Resolve an uncertain send only after checking its exact project and queue UUID.
To record a confirmed provider receipt, provide the positive Telegram message
ID found in the conversation:

```sh
multi-harness-telegram \
  --database "$MHB_HOME/bridge.sqlite3" \
  --telegram-state "$MHB_HOME/telegram.sqlite3" \
  --reconcile-project project-a \
  --reconcile-queue-id QUEUE-UUID \
  --reconcile-outcome confirmed-sent \
  --provider-message-id 1234
```

If you verified that Telegram did not create the message, use
`--reconcile-outcome verified-unsent` and omit `--provider-message-id`. That
explicit decision releases the exact coordinator lease; the next tick may
retry the same key against its persisted bot, chat and thread, even if the
active route config has changed. A confirmed receipt also preserves the
historical allowlist and route provenance needed to correlate later replies.
The continuous service isolates bot/project failures and keeps running;
`--once` exits on the first operational failure for diagnosis.

The focused tests use a simulated Bot API and the real SQLite Store. They cover
multiple projects and harnesses, both directions of the PMA route, native
thread replies, dedupe, uncertainty, shared/dedicated configurations, and the
Hermes hook path.
