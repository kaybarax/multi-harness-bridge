# Wake-turn policy

A wake, a listener hint or a digest notice starts a coordinator turn that nobody typed. The bridge decides, in one place, what such a turn may do through the bridge, and enforces it on every host. The wording of the wake and of the skills explains the policy; the bridge does not rely on it.

## Turn kinds

| Kind | Meaning |
| --- | --- |
| `user` | The person started the turn: typed it, or set a goal that the host continues. |
| `wake` | A bridge notification or listener hint started the turn. |
| `digest` | A bridge digest notice started the turn. |
| `operator` | No host session at all: a terminal or a service. |
| `unknown` | A host session whose turn kind cannot be proven. |

Each host plugs in a turn source, a function that classifies the current turn from native evidence only. Register one with `wake_policy.register_turn_source(harness, source)`.

| Host | Evidence |
| --- | --- |
| Codex | The latest turn's trigger and its first user message in the thread transcript. A bridge text is a wake. A `composer` or `goal` turn is the user's. Anything else is unknown. |
| Claude Code | The `UserPromptSubmit` hook context and the time of the listener's last hint. The turn is the user's only when the person typed after the last hint. |
| Devin Local | The user message that precedes the tool call in the native session. |
| Antigravity Desktop | The hook event's last user input. |

## What a turn may do

| Action | `user`, `operator` | `wake` with `act` | `wake` with `acknowledge` | `digest` | `unknown` |
| --- | --- | --- | --- | --- | --- |
| Read, claim, finish | yes | yes | yes | read only | yes |
| Reply | yes | yes | to the woken message only | no | yes (counted) |
| Send to bound peers | yes | yes | no | no | yes (counted) |
| Role take or handover | yes | no | no | no | no |
| Bridge settings (stop, resume, options, digests on/off, install) | yes | no | no | no | no |
| Skill propagation | yes | no | no | no | no |

A peer message is never the user's approval. The wake text says so, and anything that needs the user's go still waits for the user.

The checks cover every bridge entry point: `multi-harness-skills`, the `mhb` operator CLI when it is called from inside a host session, the Devin and Antigravity MCP tools, and the credential transport. A credential caller (MCP over stdio, or A2A) without a host session is `unknown`.

Threat model: the policy stops an agent that follows peer text through the bridge's interfaces. It does not stop a hostile program running as the same OS user, which could edit the bridge's files directly. Host sandboxes and permissions own that boundary.

Limits worth knowing:

- **Host markers.** A process counts as a host session by its host's environment markers (for example `CODEX_THREAD_ID` or `CLAUDE_CODE_SESSION_ID`). A shell whose host does not export a marker, or a command that removes them, runs as an operator. Credential (MCP or A2A) callers never count as operators: without a host session they are `unknown`.
- **Unknown turns.** In both modes an `unknown` turn may read and message, and every such message counts toward the loop cap. It can never change roles, settings or skills.
- **Re-hints.** When a Claude Code listener hints again during a turn the person typed, the rest of that turn counts as a wake. The bridge then refuses setting changes; send the request again in a new turn.
- **Claude Code prompts.** Every prompt submitted to the chat counts as the user's, including messages from other local sessions of the same app.

## Loop guard

- **Ack-only replies.** A reply that only confirms receipt can be marked ack-only (`--ack-only`, or `ack_only=true` in MCP tools). It is stored as done at once, wakes nobody and still appears in traces, the history feed and digests. Ack-only is for replies of at most 280 characters.
- **Person routes.** Messages to or from a person's notification route (Telegram, an assistant relay) are never capped.
- **Per-pair cap.** Messages sent from turns that are not the user's count toward a cap per pair of harnesses, in both directions: `loop_cap_messages` (default 10) within `loop_cap_minutes` (default 30). The stricter of the two harnesses' settings applies. At the cap, the bridge refuses further such messages and records a `loop-cap` event. The watcher then alerts the bridge chat once per pair and window, and escalates like other alerts. Turns the user starts are never capped.

A message whose origin a caller does not state counts toward the cap, so a new integration cannot skip it by leaving the origin out.

## Devin Local

Under `act`, the Devin wake prompt asks the chat to pick up the message, finish it (with an optional reply), and then act. The bridge's ACP client:

- allows the bridge's messaging tools only after the pickup;
- refuses role, setting and skill tools;
- treats Devin's own tools according to `wake_host_tools`: `deny` (default, because nobody can answer a permission prompt in a bridge turn) or `allow`.

The turn runs in its own thread. The dispatcher returns as soon as the pickup is confirmed. A turn lease in the store keeps a second wake out of the same session until the turn ends, or until `wake_turn_minutes` (default 15) passes and the turn is cancelled. A lease whose holder stopped beating expires after 90 seconds.

## Settings

Per project and harness in `options.json` (`mhb option set PROJECT HARNESS KEY VALUE`):

| Key | Values | Default |
| --- | --- | --- |
| `wake_actions` | `act`, `acknowledge` | `act` for Codex, Claude Code and Devin Local; Antigravity is always `acknowledge` |
| `loop_cap_messages` | 1–1000 | 10 |
| `loop_cap_minutes` | 1–1440 | 30 |
| `wake_host_tools` (Devin) | `deny`, `allow` | `deny` |
| `wake_turn_minutes` (Devin) | 1–120 | 15 |

To keep the earlier acknowledge-only wakes, set `wake_actions acknowledge` for each harness before you upgrade. Claude Code chats were never limited before, so `acknowledge` is new for Claude Code.
