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
2. **Connect the chat commands.** Setup provisions the bridge skills for the current user. Open a chat in the registered project. In **Devin Desktop**, first select **Local** as the environment and point Devin to the **project folder**. Then type the command for your app.

   | App | Take coordinator | Take bridge | Propagate skills |
   | --- | --- | --- | --- |
   | Codex | `$take-coordinator` | `$take-bridge` | `$propagate-skills` |
   | Devin Desktop (Local) | `/take-coordinator` | `/take-bridge` | `/propagate-skills` |
   | Google Antigravity | `/take-coordinator` | `/take-bridge` | `/propagate-skills` |
   | Claude Code | `/take-coordinator` | `/take-bridge` | `/propagate-skills` |

   Run `multi-harness-skills --help` and follow the [host setup guide](docs/skills.md) to enable the project’s native integration for each app. Use the local Desktop surface for a desktop-visible chat. An unrelated cloud session has a different identity.

3. **Assign the roles.** In one project chat enter `take coordinator "Orchard Coordinator"`. In another enter `take bridge "Orchard Bridge Inbox"`. A verified Take Bridge resumes pickup for this project and harness once both roles are assigned. Either role can be assigned first. Take Coordinator alone preserves an explicit stop. The title is optional. The native chat identity and registered workspace determine the binding.
4. **Enable pickup.** Codex, Claude Code and Devin Local default to [coordinator pull mode](docs/pickup.md): install the shared watcher with `multi-harness-service --watch`, and `take coordinator` starts or verifies it. For Antigravity Desktop, follow [Set up the Antigravity background sidecar](#set-up-the-antigravity-background-sidecar) below. For other apps, follow the [native wake guide](docs/native.md). Check the project with `mhb status orchard codex`. For automatic startup, generate a [user service definition](docs/services.md). Use the `bridge-mailbox` skill to send one free-text request from a coordinator to one or more peer harnesses in the project. Inspect each coordinator's receipt and send a reply using its delivery ID.
5. **Connect Telegram if desired.** Choose a shared bot or dedicated bots, configure the authorized chat and users, and select who owns the Telegram connection. A threaded reply returns through the PMA to the originating harness and project. See [Telegram and Hermes](docs/telegram.md).

Each harness can coordinate its workers independently. If your project uses a shared board and you want one chat to own it, invoke `take-coordinator -orc` in that chat. You can leave the board owner unset for the lifetime of the project. [Optional project orchestrator](docs/skills.md#optional-project-orchestrator)

### Set up the Antigravity background sidecar

On macOS, run the default setup under **Try it locally** and activate its virtual environment. The Antigravity adapter needs the MCP dependencies. Use the same environment and `MHB_HOME` for all commands below. Keep the virtual environment at its installed path because the Desktop app uses its Python executable to start the sidecar.

1. From your project folder, register the workspace with `mhb project add orchard .` as in step 1 above. Replace `orchard` with your project slug throughout. Open that same folder in a signed-in Antigravity Desktop project and start a conversation there so the app records its project identity. For a multi-folder project, open the same set of folders you registered.
2. From the registered project folder, install the mailbox MCP server, its tool hook, and the sidecar definition. Then explicitly enable that project's sidecar:

   ```sh
   multi-harness-skills --project orchard --to antigravity --install-native
   python -m multi_harness_bridge.ag_sidecar orchard install --enable
   ```

   The second command should report `"id": "mhb-wake-orchard"` and `"enabled": true`. Antigravity Desktop starts the sidecar and supplies its `agentapi` command. Keep the app running; it can stay in the background. Mailbox delivery works without macOS Accessibility permission.
3. Reload the project MCP server in Antigravity Desktop. If the new server is missing from the list, quit and reopen the app. In two separate chats in that project, invoke `/take-coordinator` and `/take-bridge`, one per chat. Run `mhb status orchard antigravity` and check that the two bindings are distinct and pickup is enabled.
4. For a live check, enable another harness in the same project. Ask the Antigravity coordinator to acknowledge one harmless test with a reply. Bring the sending app to the front and send the test from its coordinator to `antigravity`. Leave Antigravity in the background. Verify that the sending coordinator receives the correlated reply, then check the Antigravity inbox and coordinator histories. A successful sidecar submission alone confirms only that the app accepted the notice.

For installation conflicts, held notifications, or model quota errors, see the [Antigravity adapter guide](docs/antigravity.md).

## What travels where

```text
Project A · Codex Coordinator
    → Project A · Codex Inbox → Project A · Devin Inbox
    → durable forwarding → Project A · Devin Coordinator
    → correlated reply → Project A · Devin Inbox → Project A · Codex Inbox → Codex Coordinator
```

The [architecture guide](docs/architecture.md) explains the design and failure boundaries. The queue scopes every operation to a project. A coordinator can delegate work using its native harness. An inbox forwards messages and preserves receipts. Each peer reply creates a new queue item linked to the original. A Telegram delivery completes when the external receipt is recorded. The human can answer that message later.

| Capability | How to use it |
|---|---|
| Inspect a route | `mhb status PROJECT HARNESS`, `mhb pending PROJECT`, `mhb trace PROJECT MESSAGE_ID` |
| Stop or resume | Ask the current coordinator or use `mhb stop PROJECT HARNESS` and `mhb resume PROJECT HARNESS` |
| Rotate chats | Open a new project chat and invoke `take coordinator` or `take bridge`. Outstanding work stays in the queue. |
| Reuse project skills | Invoke `propagate skills from Codex and Claude` in the active workspace. A committed `.mhb/project-skills.json` opts that workspace into one reviewed project entry; otherwise propagation keeps its per-skill behavior. See [Skill propagation](docs/propagation.md). |
| Connect another client | Use a role-scoped MCP server on stdio or authenticated A2A v1 JSON-RPC on loopback. See [Mailbox protocols](docs/protocol.md). |
| Diagnose delayed delivery | Inspect the queue lease, native wake result, inbox forwarding, coordinator read and provider receipt in sequence. See [Operations](docs/operations.md). |
| Let coordinators collect their own mail | The default for Codex, Claude Code and Devin Local (`coordinator_pickup: pull`). A shared watcher forwards mail without opening or selecting chats; the bridge chat becomes a history board with digests. See [Coordinator pickup](docs/pickup.md). |
| Let woken coordinators act | A turn the bridge starts may read, reply, message the other bound harnesses and carry on with assigned work (`wake_actions: act`, the default for Codex, Claude Code and Devin Local). Role changes, bridge settings and skill propagation are refused in such turns on every host, receipts can be ack-only, and a per-pair loop cap raises an alert. See [Wake-turn policy](docs/wake-policy.md). |
| Wake a busy Codex coordinator | Enable the agent relay with `mhb option set PROJECT codex coordinator_wake agent-relay`; the inbox agent injects the wake into the coordinator's active turn. See [Native wake](docs/native.md). |

## Platform support

The original desktop workflow was developed and exercised on macOS. The Python queue, local operator CLI, project registry and protocol interfaces are platform-independent. Native session APIs, install locations and GUI automation vary by app, OS and version. Antigravity Desktop uses its native sidecar for background delivery. On macOS, the optional helper for renaming a chat requires local compilation and Accessibility access. Linux and Windows users should verify their native adapters on their own installations. See [validation and platform coverage](docs/validation.md) for what has actually been exercised in this distribution.

## Development

Install `python -m pip install -e ".[test,mcp,a2a,claude]"` into a virtual environment and run `python -m pytest`. The test suite uses temporary state. [Contributing](CONTRIBUTING.md) describes how to test new adapters without touching a live account.

For native desktop acceptance, follow the [end-to-end checklist](docs/checklist.md), including cross-project isolation, stop/resume and Telegram replies.

MIT licensed. See [LICENSE](LICENSE).
