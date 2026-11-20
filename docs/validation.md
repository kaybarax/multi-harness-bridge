# Validation and platform coverage

The original workflow ran on macOS. This repository distributes a generalized implementation with a separate test suite. A successful local self-test establishes queue and reply routing. A desktop acceptance run also needs a signed-in account, installed host application and two visible role chats per participating project.

| Platform or interface | Evidence | What to verify on your installation |
|---|---|---|
| macOS | Python package tests exercise SQLite, role rotation, wake bookkeeping, native transcript fixtures, MCP and A2A. The Antigravity helper was compiled locally. | Confirm that the installed host version accepts a wake, presents the intended conversation, and preserves your existing composer draft. |
| Linux | The queue, CLI and peer-route self-test were exercised in a Linux container. The package can render a user systemd service. | Install the service in your login session and verify each available host's native command. |
| Windows | The package supports Python 3.11+ and generates a Task Scheduler definition. Tests inspect its XML and quoting. | Windows installation and an interactive desktop run require validation on a Windows machine. |
| Telegram and Hermes | Automated tests use a simulated Bot API with the real queue, including provider uncertainty and correlated replies. | Verify bot permissions, chat and user allowlists, and a threaded return message with your own account. |
| Claude Code | Automated checks cover its local integration. | Run a live inbox-to-coordinator test when a Claude session is available. |

Run `mhb self-test` for twelve directed, simulated round trips across two projects. Use the [live acceptance checklist](checklist.md) to exercise desktop sessions and Telegram. A provider receipt, a successful queue claim and a reply visible in a native chat are distinct observations.
