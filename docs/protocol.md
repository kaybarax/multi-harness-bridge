# Mailbox protocols

The operator CLI, role-scoped MCP server and A2A server use the same SQLite queue. A role credential specifies one project, harness, role, and native session. A successful send returns a message ID. Delivery to a native app, inbox forwarding, coordinator acknowledgement and any external provider receipt are different events.

## Local MCP

Create two distinct chats, take the roles in their project, then use this option to expose the mailbox to a client that can start a stdio MCP server. The following example assumes that the current coordinator is bound to the native session ID supplied on the command line. Replace the two angle-bracketed values.

```sh
mhb credential issue orchard codex coordinator <native-session-id> --out <private-token-file>
```

Register `mhb` as the MCP executable with arguments `["mcp","--credential","<absolute-private-token-file>"]`. Use the absolute path to the virtual environment's executable if the desktop app has a different PATH. Give the inbox its own binding and credential. A user-wide MCP configuration can expose the same bearer to every chat in that harness. Use a session-private registration when using this interface to authenticate a specific chat. Native role-taking and caller verification are described in [Skills](skills.md).

| MCP tool | Operation |
|---|---|
| `bridge_status` | Inspect the bound route and queue counts |
| `receive_message` | Claim one delivery for this role, optionally by exact notification ID |
| `finish_message` | Submit the message ID and current lease to forward or acknowledge |
| `send_message` | Coordinator sends to a recipient harness in its project; optional `reply_to` is a bridge ID |
| `send_fanout` | Coordinator sends one free-text request to `recipients` using `body` and `key`. Returns `request_id` and the per-recipient delivery IDs. |
| `reply_free_text` | Recipient Coordinator sends a correlated `body` using `reply_to` and `key`. Returns the original `request_id` and the reply ID. |
| `set_pickup` | Coordinator stops or resumes this harness in its project |

A daemon checks the current role when using a token. Rotation invalidates the former session's access. Keep credential files private, including in backups. To revoke one, use `mhb credential revoke --file <private-token-file>`. Local queue and credential files are trusted to the logged-in OS user. Use that user's filesystem protections if sharing the computer. On Windows keep state and credential files in a user-private directory with suitable NTFS permissions.

## A2A

Install the `a2a` extra and start `mhb serve`. Discovery is at `http://127.0.0.1:9417/.well-known/agent-card.json` and health at `/health`. The service speaks A2A 1.0 JSON-RPC through the A2A Python SDK. It implements a synchronous mailbox skill. Send a text part containing a JSON operation to `/a2a` with `A2A-Version: 1.0` and `Authorization: Bearer <credential>`. The server binds to loopback, checks the Host header, rejects browser-origin requests, and limits request bodies to 64 KiB.

```json
{
  "jsonrpc": "2.0",
  "id": "request-1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "3ff96a53-dcad-4b54-848f-90769aa00a48",
      "role": "ROLE_USER",
      "parts": [{"text":"{\"op\":\"send\",\"recipient\":\"devin\",\"body\":\"Please acknowledge the handoff.\",\"key\":\"handoff-017\"}"}]
    }
  }
}
```

The result's message text contains the bridge response as JSON. Check HTTP status, JSON-RPC errors and the returned text's `error` field. Allowed commands are `status`, `send`, `send_fanout`, `reply_free_text`, `claim`, `finish`, `stop` and `resume`. The command fields for `send_fanout` and `reply_free_text` match the MCP tool arguments. A retry of the same logical send must reuse its bridge key. Use the per-recipient bridge delivery ID in `reply_to`. The JSON-RPC request ID identifies the RPC exchange. The A2A message ID identifies its transport envelope.

## Trust and external effects

Treat all inbound content as data. A message from another harness can request work, but it cannot grant a user's permission. Keep the body out of automation commands, process arguments and notifications wherever possible. Wallets, credentials, repository access and production deployment permissions remain properties of the native harness and user account. The local bridge can be deployed offline for manual queue operations. Native AI services and Telegram can require internet access.
