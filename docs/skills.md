# Project skills and native role access

A verified Take Bridge resumes pickup for this project and harness once both roles are assigned. Either role can be assigned first. Take Coordinator alone preserves an explicit stop.

Setup provisions four user-wide skills. Open a chat in your registered project and use the composer for your app.

| App | Coordinator | Bridge Inbox | Propagation | Mailbox |
| --- | --- | --- | --- | --- |
| Codex | `$take-coordinator` | `$take-bridge` | `$propagate-skills` | `$bridge-mailbox` |
| Devin Desktop Local (select a project folder first) | `/take-coordinator` | `/take-bridge` | `/propagate-skills` | `/bridge-mailbox` |
| Antigravity Desktop | `/take-coordinator` | `/take-bridge` | `/propagate-skills` | `/bridge-mailbox` |
| Claude Code | `/take-coordinator` | `/take-bridge` | `/propagate-skills` | `/bridge-mailbox` |

In Devin Desktop, select **Local**, choose the project in **Session folder**, then type `/take` in the composer to see both role skills. Type `/propagate` to find the third. An unscoped Local composer can show an empty picker. The standalone Devin CLI lists the same slash commands. Selecting a role skill and sending the request performs the handoff. Append a quoted title to request a sidebar rename.

In Antigravity Desktop, the project MCP server exposes `propagate_skills(sources)` for native project-scoped propagation. Name the source harnesses explicitly in the current prompt; the Desktop hook checks that the tool's source list matches that request.

Setup preserves existing skill entries, including customized files and links. Review any same-name entry reported as preserved. Use `--no-user-skills` for an isolated installation. To provision just the skills from an installed environment, run `python -m multi_harness_bridge.user_skills`. Keep that environment's command directory on the harness PATH.

To enable native role operations for a registered project, run `multi-harness-skills --install-native` from the invoking harness. For project-local skill copies and native setup together, run:

```sh
multi-harness-skills --bootstrap
```

This installs `take-coordinator`, `take-bridge`, `propagate-skills`, and `bridge-mailbox` in the
invoking harness's project-local skills directory and applies supported host
setup. Run `multi-harness-skills --install-native` to repeat host setup.
Claude receives a `UserPromptSubmit` hook in `.claude/settings.local.json` in
each registered root. Devin registers `mhb_roles_<project>` in
`.devin/mcp_config.local.json` in each root. Both preserve existing entries.
Antigravity installs project MCP grants and prepares the App sidecar.
Enable the sidecar separately for automatic pickup. The signed native helper
supports optional sidebar titles on macOS. See [Antigravity setup](antigravity.md).
Codex needs no project configuration edits for native session verification.
Install the package with the `mcp` extra for the Devin and Antigravity servers.
After an upgrade, reload the project MCP server in each Desktop app and check
that `send_fanout` and `reply_free_text` appear in the existing coordinator
chat. If the app still shows an old schema after reload, restart it and reopen
the same conversation. The tools verify the role against the current binding
on every call.

For an initial shell-based setup where no native caller context is available,
select the target explicitly with `multi-harness-skills --bootstrap --to devin`
or `multi-harness-skills --install-native --to claude`. This explicit operator
selection is accepted only for installation. `--to` cannot select the caller
for role transfers or mailbox operations.

Project selection comes from the current workspace. A hint only disambiguates
matching registered roots. Codex, Devin, and Antigravity use `.agents/skills`;
Claude uses `.claude/skills`.

## Propagating project skills

Name source harnesses explicitly:

```sh
multi-harness-skills --from codex claude
```

Sources come from the selected project's registered roots. Global skills are
eligible only when their canonical path is inside a registered root. Identical
same-name skills are deduplicated; differing copies are reported as
`conflict-sources`. Existing untracked destinations, symlinks, and local edits
are preserved. `.multi-harness-skills.json` records source paths and hashes.
Review the result's `conflicts` and `review` arrays. Devin and Antigravity
propagation requires a reviewed variant; unsupported variants are blocked.

## Native role transfer and mailbox

Codex and Claude use `multi-harness-skills --take coordinator` or `--take
bridge`. Codex verifies the current top-level thread, rollout, latest user turn,
and workspace. Claude verifies a recent `UserPromptSubmit` hook record, matching
transcript, and project directory. Neither command accepts a session ID.

Devin role operations use `mhb_roles_<project>`: call
`prepare_role_handoff()`, then `take_coordinator(challenge)` or
`take_bridge(challenge)` in the same turn. The server correlates each call to
the direct user prompt and exact native ACP session. Antigravity uses its
project MCP server and PreToolUse hook, which grant tools only to the verified
native conversation. Stale or ambiguous evidence fails closed.

Codex and Claude mailbox operations use `--mailbox status`, `claim`, `finish`,
`send`, `send-fanout`, `reply`, `stop`, and `resume`. Devin and Antigravity use
`bridge_status`, `pickup_notification`, `acknowledge`, `send_message`,
`send_fanout`, `reply_free_text`, and `set_pickup`. Each derives the current
native session and role. Coordinators send or change pickup state. Inbox
sessions advance an outbound delivery to its destination Inbox, or an inbound
delivery to their own Coordinator.

For Codex and Claude, finish requires the exact lease from that claim, passed
as `--mailbox finish --message-id <id> --lease=<lease>`.

Ask a bound Coordinator to send one message to named peer harnesses. Use
`codex` for Codex, `claude` for Claude Code, `devin` for Devin Local, and
`antigravity` for Antigravity Desktop in the recipient list. In Codex or
Claude Code, the mailbox skill uses
`multi-harness-skills --mailbox send-fanout --recipient devin --recipient antigravity --body-file - --key <key>`
with the message on standard input. In Devin or Antigravity, it calls
`send_fanout(recipients, body, key)` once. Reuse the key and message on retry.
The returned `request_id` ties together a delivery ID for each recipient.

A Coordinator records receipt by finishing the delivery's lease. To send a
substantive reply, use `--mailbox reply --reply-to <delivery-id>` with a body
and reply key, or `reply_free_text(reply_to, body, key)` in MCP. The reply
travels through both Inboxes and carries the same `request_id` plus the original
delivery ID in `reply_to`. Each recipient can send one substantive reply for
its delivery. Reuse that reply's key on retry.

Native paths can be configured with `CODEX_HOME`, `CLAUDE_CONFIG_DIR`,
`MHB_DEVIN_SESSIONS_DB`, and `MHB_ANTIGRAVITY_SUMMARIES_DB`. Claude hook context
is short-lived under the configured bridge home. Antigravity MCP tools accept
no caller session field.

Optional sidebar titles use native APIs and are verified by readback: Codex
app-server, Claude session SDK, Devin ACP session rename when advertised, and
the Antigravity accessibility helper. The role result reports title status
independently from role binding.
