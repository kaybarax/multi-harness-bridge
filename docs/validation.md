# Validation and platform coverage

The original workflow ran on macOS. This repository distributes a generalized implementation with a separate test suite. A successful local self-test establishes queue and reply routing. A desktop acceptance run also needs a signed-in account, installed host application and two visible role chats per participating project.

| Platform or interface | Evidence | What to verify on your installation |
|---|---|---|
| macOS | A clean checkout was built into a wheel and installed in a fresh environment. All 71 tests passed. The tests exercise SQLite, role rotation, wake bookkeeping, native transcript fixtures, MCP and A2A. The Antigravity helper was compiled locally. | Confirm that the installed host version accepts a wake, presents the intended conversation, and preserves your existing composer draft. |
| Linux | The installed wheel passed 69 tests on Linux ARM64 with Python 3.12. The two macOS-specific tests were skipped. The offline queue self-test also completed twelve routes. A generated unit passed systemd’s native verifier. | Install the service in your login session and verify each available host's native command. |
| Windows | The package supports Python 3.11+ and generates a Task Scheduler definition. Tests inspect its XML and quoting. | Windows installation and an interactive desktop run require validation on a Windows machine. |
| Telegram and Hermes | Automated tests use a simulated Bot API with the real queue, including provider uncertainty and correlated replies. | Verify bot permissions, chat and user allowlists, and a threaded return message with your own account. |
| Claude Code | Automated checks cover its local integration. | Run a live inbox-to-coordinator test when a Claude session is available. |

## Live macOS acceptance

A fresh installation based on `8229fe2`, with the multi-instance Antigravity helper change, used two disposable projects on macOS. Each project had separate inbox and coordinator sessions in Codex, Devin Local and Antigravity Desktop. For each project, every coordinator sent a request to each of the other two harnesses. All twelve requests and twelve correlated replies reached the acknowledged state. Each reply matched the original project, sender and recipient.

All six coordinators also sent through the PMA to one dedicated test bot shared by both projects. Each received one threaded Telegram Web reply in its originating project and coordinator. Codex and Devin sessions were explicitly resumed, and Antigravity Desktop chats were prompted through the UI for these round trips. This matrix exercises native mailbox processing independently of unattended wake delivery.

The earlier Accessibility-based macOS Antigravity dispatcher was also exercised separately with a bound chat visible for the tested project and Accessibility granted to the installed helper. In both projects it woke the native inbox and coordinator, with one recorded forward and acknowledgement per message. Stopping pickup from one coordinator kept its message unclaimed while the other project completed delivery. Resuming pickup delivered the queued message. One coordinator reclaimed its lease before acknowledging. The two wake IDs appeared only in their own project’s native chats.

The current Antigravity App sidecar passed a separate macOS background test. A bound Devin coordinator sent a harmless request to Antigravity. The bound Antigravity inbox and coordinator received native system messages and returned one pre-authorized, correlated reply to the original Devin chat. Codex remained in front throughout the exchange. The installed sidecar build passed 134 automated tests. See the [fresh-install steps](../README.md#set-up-the-antigravity-background-sidecar).

Run `mhb self-test` for twelve directed, simulated round trips across two projects. Use the [live acceptance checklist](checklist.md) to exercise desktop sessions and Telegram. A provider receipt, a successful queue claim and a reply visible in a native chat are distinct observations.
