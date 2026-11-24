# Antigravity Desktop adapter

Multi Harness Bridge uses an Antigravity App sidecar for automatic wakes.
The sidecar calls the documented `agentapi send-message` command to submit
an ID-only notice to the existing, bound Desktop conversation. It runs in
the background and leaves Desktop windows, focus, and drafts alone. See the
[Antigravity sidecar documentation](https://antigravity.google/docs/sidecars/#agentapi).

The adapter reads the Desktop conversation summary database in read-only
mode. It verifies the exact bound role and conversation UUID, the native
project ID, the registered workspace roots, and the notification's current
pickup eligibility before submission. The regular mailbox tool claims and
forwards or acknowledges the delivery when the recipient processes it.

## Install and enable

Install the package with the MCP extra. Open the registered Desktop project
and configure its scoped mailbox server and PreToolUse hook with the native
installation command. Enable the project sidecar using the same Python and
MHB state directory as its mailbox server.

```sh
multi-harness-skills --project my-project --to antigravity --install-native
python -m multi_harness_bridge.ag_sidecar my-project install --enable
```

The native installer prepares the sidecar definition. The separate `--enable`
command enables that one sidecar in Antigravity's user configuration. The
Antigravity App manages its lifecycle and supplies `agentapi`. Open the App
with its normal account and permissions. The generic MHB wake worker leaves
Antigravity wakes to this sidecar and continues to handle other harnesses.

On upgrade, stop or refresh older MHB wake processes that include the former
Accessibility-based notification worker before enabling the sidecar. The
sidecar shares the existing wake ledger. It preserves confirmed attempts
and holds ambiguous submissions for reconciliation. Review the native
conversation before reconciling an unresolved notification.

## Verify delivery and recover a held notice

Follow the notification ID through the App's sidecar logs, the exact bound
native conversation, and `mhb trace PROJECT ID`.

| Evidence | What it establishes |
| --- | --- |
| Sidecar reports `sent` and the official App event records acceptance | The App accepted submission of the wake notice. |
| The exact notification ID appears in a committed native turn | The target conversation received the notice. |
| The durable trace records `inbox-claimed` and `forwarded` | The bound Inbox forwarded the existing delivery. |
| The trace records `coordinator-claimed` and `acknowledged` | The bound Coordinator acquired a lease and recorded receipt. |
| The correlated reply reaches the original Coordinator | The substantive round trip finished. |

The model can encounter a provider error after the App has committed a wake
or even after a role has claimed the delivery. For HTTP 429, record the
provider's reset time with its timezone and hold provider calls until that
time. Preserve the submitted wake record and the original notification,
request, and reply identities.

After the reset, confirm the current binding, pending stage, lease, and
existing replies for the exact notification. An active lease takes
precedence. If an existing native turn can continue, let it finish using
the same notification. When an operator authorizes a separate recovery
notice, submit it once through the same scoped App sidecar and record its
own outcome alongside the original. The bound native MCP tools still claim
and acknowledge the original delivery. Use its existing reply reference
and stable reply key. Arrange a receipt-only disposition for a delivery the
sender has superseded, and route the correction under its own identity.

If a scoped recovery worker temporarily owns dispatch, account for the
reply's outbound Inbox hop. Forward that exact reply under the same
authority, or restore the normal sidecar once all receipt-only deliveries
are terminal. Keep the ordinary dispatcher paused while a superseded item
still needs its explicit receipt-only notice.

## Native role changes

A fresh chat can invoke the scoped `take_coordinator` or `take_bridge` tool
after the user's matching command. The hook verifies the current input,
Desktop conversation, registered roots, and native artifact or transcript
location. It supplies a short-lived, single-use grant for the actual call
and arguments. The coordinator can send, reply, and change pickup. Both
roles can process the exact notification assigned to them. The Inbox
forwards it durably. The Coordinator receives an exact lease and uses
`acknowledge` to record receipt.

Skill propagation can use one active workspace from a native Desktop project
whose verified workspace set is a unique subset of one registered MHB
project. The hook requires the event workspace set to match the conversation
summary, the selected active root to be registered to that project, and the
native project ID to remain unambiguous. If several native roots are open, the
event must identify the active root. Propagation grants bind the conversation,
native project ID, workspace set, active root and explicit source list through
the tool call. Role changes continue to require the exact registered root set.

An explicit takeover with a quoted title can use the optional macOS
Accessibility helper to rename the verified conversation. The sidecar and
mailbox delivery require no Accessibility permissions.
