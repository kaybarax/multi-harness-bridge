# Project skills and native role access

A verified Take Bridge resumes pickup for this project and harness once both roles are assigned. Either role can be assigned first. Take Coordinator alone preserves an explicit stop.

Setup provisions three user-wide skills. Open a fresh chat in your registered project and use the composer for your app.

| App | Coordinator | Bridge Inbox | Propagation |
| --- | --- | --- | --- |
| Codex | `$take-coordinator` | `$take-bridge` | `$propagate-skills` |
| Devin Desktop Local (select a project folder first) | `/take-coordinator` | `/take-bridge` | `/propagate-skills` |
| Antigravity Desktop | `/take-coordinator` | `/take-bridge` | `/propagate-skills` |
| Claude Code | `/take-coordinator` | `/take-bridge` | `/propagate-skills` |

In Devin Desktop, select **Local**, choose the project in **Session folder**, then type `/take` in the composer to see both role skills. Type `/propagate` to find the third. An unscoped Local composer can show an empty picker. The standalone Devin CLI lists the same slash commands. Selecting a role skill and sending the request performs the handoff. Append a quoted title to request a sidebar rename.

In Antigravity Desktop, the project MCP server exposes `propagate_skills(sources)` for native project-scoped propagation. Name the source harnesses explicitly in the current prompt; the Desktop hook checks that the tool's source list matches that request.

Setup preserves existing skill entries, including customized files and links. Review any same-name entry reported as preserved. Use `--no-user-skills` for an isolated installation. To provision just the skills from an installed environment, run `python -m multi_harness_bridge.user_skills`. Keep that environment's command directory on the harness PATH.

To enable native role operations for a registered project, run `multi-harness-skills --install-native` from the invoking harness. For project-local skill copies and native setup together, run:

```sh
multi-harness-skills --bootstrap
```

This installs `take-coordinator`, `take-bridge`, and `propagate-skills` in the
invoking harness's project-local skills directory and applies supported host
setup. Run `multi-harness-skills --install-native` to repeat host setup.
Claude receives a project-local `UserPromptSubmit` hook in
`.claude/settings.local.json`; existing settings are preserved. Devin registers
`mhb_roles_<project>` in `.devin/mcp_config.local.json`, preserving existing
servers. Antigravity installs project MCP grants and its signed native helper.
Codex needs no project configuration edits for native session verification.
Install the package with the `mcp` extra before loading the Devin server, then
reload the project MCP configuration in Devin Desktop.

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
`send`, `stop`, and `resume`. Devin and Antigravity use MCP tools
`bridge_status`, `pickup_notification`, `acknowledge`, `send_message`, and
`set_pickup`. Each derives the current bound session and role. Inbox finishing
relays work to its Coordinator; only the Coordinator sends outbound messages
or changes pickup state. Replies use `reply_to`, and retries must reuse a stable
explicit idempotency `key`.

Native paths can be configured with `CODEX_HOME`, `CLAUDE_CONFIG_DIR`,
`MHB_DEVIN_SESSIONS_DB`, and `MHB_ANTIGRAVITY_SUMMARIES_DB`. Claude hook context
is short-lived under the configured bridge home. Antigravity MCP tools accept
no caller session field.

Optional sidebar titles use native APIs and are verified by readback: Codex
app-server, Claude session SDK, Devin ACP session rename when advertised, and
the Antigravity accessibility helper. The role result reports title status
independently from role binding.
