# Antigravity Desktop adapter

The `antigravity` adapter targets the already-bound conversation in Google
Antigravity Desktop on macOS. It reads `~/.gemini/antigravity/conversation_summaries.db`
in read-only mode and requires the conversation's native project ID and full
workspace-root set to match exactly one project in the MHB registry. Every
registered project with a native Antigravity ID is checked for ID collisions;
bound conversation UUIDs must also be canonical and unique across projects.

The public adapter operations are:

```python
from multi_harness_bridge import ag_wake

ag_wake.verify(project, role, session)
ag_wake.wake(project, role, session, notification_id)
ag_wake.rename(project, role, session, title)
```

`verify` checks the SQLite role binding and native Antigravity summary. `wake`
then uses the macOS Accessibility helper to select the exact UUID sidebar link
and submit a fixed notice containing the notification ID. It never includes the
mailbox body or changes queue state. The recipient remains responsible for
using its installed MHB mailbox tools to inspect and process one pending item.
`rename` changes the title in Antigravity's native interface only after checking
the exact bound role and visible conversation.

## Install on macOS

Install the package with its packaged Swift source, then compile and sign the
helper with:

```sh
python -m multi_harness_bridge.ag_wake install
```

The helper is written to the private MHB configuration directory under
`native/antigravity-ax`. The installer compiles it with `swiftc` and applies a
stable ad-hoc code signature. To authorize its native UI access, open **System
Settings → Privacy & Security → Accessibility**, add or enable
`antigravity-ax` (under the MHB configuration directory), and approve the
system prompt. If macOS does not list the helper after installation, use the
add button to select that executable. Reinstalling after a helper update may
require granting access again.

For an initial check, leave Antigravity running with one window on either
bound conversation in the registered project and run:

```sh
multi-harness-wake --project my-project --harness antigravity --once
```

The helper refuses to send if there is not exactly one Antigravity app and
matching project window, the requested UUID sidebar link is missing or
ambiguous, the app is busy, a dialog is open, the composer has a draft, or
recent keyboard or mouse input indicates active use. It preserves the current
composer draft and avoids switching conversations while the app is generating.
An ambiguous send is treated as uncertain and is not automatically retried.

## Other operating systems

This native driver uses AppKit, ApplicationServices, launchctl, and macOS
Accessibility permissions. It does not automate Antigravity on Windows or
Linux. Those systems need their own native adapter implementing the MHB
`verify` and `wake` contract; they must verify the exact registered workspace
and bound conversation before submitting a prompt. The package does not fall
back to coordinate clicking or a guessed Antigravity command-line interface.

## Connect a native inbox and coordinator

From the registered workspace, run `python -m multi_harness_bridge.ag_inbox PROJECT install` with the MCP extra installed. This registers a project-specific stdio server and a matching native PreToolUse hook in Antigravity's configuration. The installer merges its entries and preserves unrelated servers. Reload that server in Desktop so the active processes have the current code.

A fresh chat can invoke the project server's `take_coordinator` or `take_bridge` after the user's matching command. The hook verifies the current user input, Desktop conversation ID, native summary, workspace roots and artifact/transcript location. It supplies a short-lived, single-use grant associated with the actual call and arguments. The coordinator may send messages and change pickup. Both roles can call `pickup_notification` for the supplied wake ID. For an inbox this forwards the message durably. For a coordinator it returns the message and lease; the coordinator calls `acknowledge` after receiving it. Using a quoted title applies that title to the native window and reports any separate rename failure. The hook and the MCP server take no session IDs from the user.
