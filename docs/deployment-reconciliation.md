# Deploy free-text routing alongside a legacy bridge

Use a dedicated Multi Harness Bridge state directory for the project. Existing legacy
notifications, assignments, Telegram routing, credentials, and queue services stay on
the legacy installation. The Multi Harness Bridge worker processes only the dedicated
mailbox for new free-text routes. An outbound request, the Inbox forwarding stages,
and replies all use that one mailbox.

## Capture and review

1. Record both workspace roots, which binary and configuration each native chat uses,
   the current coordinator and Inbox per harness, intentional stops, outstanding
   mailbox entries, and active or uncertain leases. Record a concurrent handoff
   against the new role owner before comparing results.
2. Stage the already-tested candidate in a private environment with an explicit
   executable path. Register both workspace roots in a dedicated `MHB_HOME`. The
   same path must reach the desktop tool registrations, wake dispatcher, and
   command-line integration.
3. Add `legacy.json` in that state directory with version `1`, the registered
   `project` slug, and its absolute legacy `installation` directory. The directory
   must contain the existing `state` subdirectory. This tells the candidate to
   consult the installed bridge live for the project's role and pickup state.
4. Run the reconciliation command without `--apply`. The preview lists only the
   affected files, recognized project server names, and content hashes. Back up
   those files and the affected process configuration into a private directory.
   Use each database's online SQLite backup API for consistent point-in-time
   copies while services are running. Pause mutation and refresh the baseline
   before any cutover requiring a coordinated snapshot across multiple stores.

```sh
"$MHB_CANDIDATE_PYTHON" -m multi_harness_bridge.reconcile PROJECT --home "$MHB_PROJECT_STATE"
```

When replacing a generated portable registration that names an earlier executable,
pass `--previous-executable` with that exact absolute path. Other fields must match
the project registration. An unrecognized collision is an error.

## Apply and refresh

1. Apply the preview with `--apply`, then repeat it. The second execution should
   show an empty change list. This updates recognized legacy Devin role arguments
   using the live binding and installs the candidate's project server in each
   registered workspace. The candidate server has no chat ID in its arguments
   and derives the current caller from the native tool invocation. The existing
   Devin handoff server keeps its native authentication and tool schema. After
   it records a new role owner, its compatibility launcher refreshes the
   recognized legacy registrations in both roots. Refresh historically loaded
   legacy MCP processes in affected chats to pick up updated static arguments.
   Identify those processes by their static role and session arguments. The
   portable `serve-roles` process serves its owning native stdio client and exits
   when that client closes stdin. Refresh the affected server through the native
   client. Other legacy route tools retain their role validation and schemas.
2. Install or refresh the project-specific Antigravity registration and hook
   using the candidate's native installer. Reload that exact registered server in
   the native app. Refresh the Devin server in each affected chat so it discovers
   the new schema. For Codex and Claude, verify the installed mailbox skill
   invokes the candidate executable with the project's state directory in its
   environment. The Claude Code installer pins the same state directory in
   both workspace-scoped prompt hooks. Its native identity proof uses Claude
   Code transcript metadata. Retain the established legacy role handoff and
   lifecycle commands. If a custom Devin propagation skill is already installed,
   its maintained copy needs to call `propagate_skills` on the project's
   `mhb_roles_<project>` MCP server with the sources from the direct user turn.
   Setup preserves the existing skill directory and any links to it. For a
   stock copy, compare its regular `SKILL.md` file with the previously bundled
   version before replacing that exact file from the candidate. Keep a backup
   and leave any custom copy or link in place for its maintainer to update.
3. Run the candidate wake dispatcher scoped to this one project. It handles its own
   mailbox and rechecks the current role, pickup state, and notification before
   submitting the wake. Claude uses the subscribed native chat. For Devin, verify
   the project from the actual native session working directory so either root is
   supported. With the legacy binding provider configured, the portable worker
   shares `state/devin-local-project-wake.lock` and `state/devin-local-relay.wake.lock`
   with the two legacy pollers. It acquires them in that order, without waiting,
   after the portable SQLite reservation commits. It holds them through native
   command cleanup. A busy lock leaves the notification pending without spending
   an attempt. The legacy pollers continue to serve their own queues.
4. Read status in the existing chats. Compare the role pairs, pending work,
   and stop state with the refreshed baseline. Use one new idempotency key per
   test exchange, and compare observed pickup and replies with the durable trace.
   Rotate a role through its native handoff, then verify the successor and the
   retired session in both registered workspace roots. New free-text tools resolve
   the active binding without a manifest edit.

This integration reads the existing bridge's authoritative roles and controls. Use
its native take and stop/resume workflows. An active legacy handoff recovery record
or conflicting mailbox and native role pair blocks candidate authorization until the
existing bridge resolves it. Reconciliation does not rewrite native transcripts,
clear queue leases, replay deliveries, or purge caches.

A legacy direct control command can still contend for the same Devin session.
Run manual legacy stop or resume outside an active native wake when possible.
If the native host reports the session is locked, retry after that turn exits.

## Rollback

Stop the project-scoped candidate dispatcher. Preserve the candidate queue and its
notification ledger so deliveries and uncertain attempts can be reviewed or resumed.
Restore the affected project entries in the native configurations from the private
backup, after checking for concurrent edits and retaining other projects' changes.
Refresh only the affected servers. Leave later deliveries in the original live
mailbox intact. A database backup is recovery evidence and does not replace newer
live records during a configuration rollback.
