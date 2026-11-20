# Local acceptance checklist

Use two disposable project workspaces and distinct native role chats. The queue-only self-test runs without any provider. The following steps validate a particular operator's desktop deployment with actual apps.

| Route | Send | Verify in the destination | Verify at the origin |
|---|---|---|---|
| Harness to harness | From each project's coordinator, send a unique no-work request to each available peer. | Inbox wakes and forwards once. Correct coordinator reads and returns one reply with the original bridge ID. | Original project's inbox and coordinator see the matching reply. Sibling project's trace and chat remain empty for that ID. |
| Harness to Telegram | From each coordinator, send the PMA a message identifying the test run. | Exactly one Telegram post identifies the project and sending harness. | Reply using Telegram's native Reply action. Confirm its return through the same PMA, project inbox and coordinator. |
| Shared bot isolation | Interleave both projects in one chat. | A threaded response uses the saved provider receipt; an ambiguous new message requires an explicit destination. | Verify there is no delivery to the other project or a default harness. |
| Dedicated bots | Route different projects through different bot registrations. | Only the configured bot and authorized chat receive each message. | A reply is matched to the bot, chat and external message ID used for that send. |

For three active harnesses and two projects this gives twelve directed peer round trips and six Telegram round trips. Add a fourth harness when its native account is available. Isolate unavailable harnesses so that an offline endpoint cannot stall the others.

**Rotation.** Start the test with the coordinator chat visible, bind a fresh inbox, and repeat a message. Then rotate the coordinator while keeping the inbox. Verify both sidebar titles separately. The old session must be unable to claim or send on behalf of its replacement. An in-flight lease should delay handoff until it finishes or expires. Drain the work queue before retiring an old native process.

**Stop and resume.** Tell the coordinator to stop pickup. Enqueue from another harness, rotate a chat, and leave the recipient stopped for more than one polling interval. The original message should remain pending. Resume explicitly and observe one successful delivery. Repeat with the other project still running.

**Timing and evidence.** Record message ID, sender, enqueue, first claim, forwarding, coordinator read, provider acceptance and correlated reply separately. Distinguish a durable queue receipt, visible native transcript and human acknowledgement. Use a fake Bot API and fixture transcripts for regression tests; use a real no-work canary only when the native apps and accounts are available.
