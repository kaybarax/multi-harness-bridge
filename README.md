# Multi Harness Bridge

Give Codex, Claude Code, Devin Local and Google Antigravity Desktop a shared way to coordinate on the same project. Each harness keeps its own **Coordinator chat** and **Bridge Inbox chat**. Messages move through a durable local SQLite queue, so teams can rotate chats, pause a harness, or return to an interrupted task without losing track of who received what.

This began with one developer dividing work among several AI development tools. A request sent in one app had to reach a specific project in another, and its reply had to find its way back after the developer had moved on. This repository packages that workflow for other machines.

## Try it locally

Requirements are Python 3.11 or later and a local filesystem. The queue uses Python's SQLite library. From the cloned repository:

```sh
# macOS or Linux
python3 scripts/setup.py
source .venv/bin/activate
mhb doctor
```

```powershell
# Windows PowerShell
py -3 scripts/setup.py
.\.venv\Scripts\mhb.exe doctor
```

Setup installs the package into an isolated environment, initializes the user's state directory, and runs twelve simulated round trips across two projects. No harness accounts, provider credentials, bot tokens or PostgreSQL installation are needed for that check. To install only the standard-library queue and CLI, add `--core-only`. To repeat the check, run `mhb self-test`.

The default state directory is `~/.multi-harness-bridge`. Set `MHB_HOME` before setup and in every service environment if you want to use another directory. Run all participating harnesses, wake services and the PMA on the same machine, under an account with access to the same state directory. Separate state directories create independent queues. Use one for disposable tests.

## Connect a project

1. **Register its workspace.** From the project's folder, run `mhb project add orchard .`. Supply more paths in the same command when one project spans several repositories. Repeat with a different project slug for another workspace.
2. **Connect the chat commands.** Setup provisions the three skills for the current user. In Codex type `$take-coordinator`; in Devin Desktop select **Local**, choose the **Session folder**, then type `/take-coordinator`; in Claude Code and Antigravity type `/take-coordinator`. Run `multi-harness-skills --help` and follow the [host setup guide](docs/skills.md) to enable the project’s native integration for each app. Use the local Desktop surface for a desktop-visible chat. An unrelated cloud session has a different identity.
3. **Assign the roles.** In one project chat enter `take coordinator "Orchard Coordinator"`. In another enter `take bridge "Orchard Bridge Inbox"`. The title is optional. The native chat identity and registered workspace determine the binding.
4. **Enable pickup.** Follow the [native wake guide](docs/native.md). Check the project with `mhb status orchard codex`. For automatic startup, generate a [user service definition](docs/services.md). Send a short test to the destination's inbox, inspect the coordinator's receipt, then send a correlated reply. The sender and recipient can belong to different harnesses.
5. **Connect Telegram if desired.** Choose a shared bot or dedicated bots, configure the authorized chat and users, and select who owns the Telegram connection. A threaded reply returns through the PMA to the originating harness and project. See [Telegram and Hermes](docs/telegram.md).

## What travels where

```text
Project A · Codex Coordinator
    → local bridge send → Project A · Devin Inbox
    → durable forwarding → Project A · Devin Coordinator
    → correlated reply → Project A · Codex Inbox → Codex Coordinator
```

The [architecture guide](docs/architecture.md) explains the design and failure boundaries. The queue scopes every operation to a project. A coordinator can delegate work using its native harness. An inbox forwards messages and preserves receipts. Each peer reply creates a new queue item linked to the original. A Telegram delivery completes when the external receipt is recorded. The human can answer that message later.

| Capability | How to use it |
|---|---|
| Inspect a route | `mhb status PROJECT HARNESS`, `mhb pending PROJECT`, `mhb trace PROJECT MESSAGE_ID` |
| Stop or resume | Ask the current coordinator or use `mhb stop PROJECT HARNESS` and `mhb resume PROJECT HARNESS` |
| Rotate chats | Open a new project chat and invoke `take coordinator` or `take bridge`. Outstanding work stays in the queue. |
| Reuse project skills | Invoke `propagate skills from Codex and Claude` in the destination. Reviewed adaptations, content hashes and local-edit conflicts are documented in [Skill propagation](docs/propagation.md). |
| Connect another client | Use a role-scoped MCP server on stdio or authenticated A2A v1 JSON-RPC on loopback. See [Mailbox protocols](docs/protocol.md). |
| Diagnose delayed delivery | Inspect the queue lease, native wake result, inbox forwarding, coordinator read and provider receipt in sequence. See [Operations](docs/operations.md). |

## Platform support

The original desktop workflow was developed and exercised on macOS. The Python queue, local operator CLI, project registry and protocol interfaces are platform-independent. Native session APIs, install locations and GUI automation vary by app, OS and version. The macOS Antigravity accessibility helper requires local compilation and user-granted Accessibility access. Linux and Windows users should verify their native adapters on their own installations. See [validation and platform coverage](docs/validation.md) for what has actually been exercised in this distribution.

## Development

Install `python -m pip install -e ".[test,mcp,a2a,claude]"` into a virtual environment and run `python -m pytest`. The test suite uses temporary state. [Contributing](CONTRIBUTING.md) describes how to test new adapters without touching a live account.

For native desktop acceptance, follow the [end-to-end checklist](docs/checklist.md), including cross-project isolation, stop/resume and Telegram replies.

MIT licensed. See [LICENSE](LICENSE).
