# Optional project orchestrator for Multi Harness Bridge

**Approved implementation plan.** A project can run indefinitely with independent harness coordinators and no shared board. Users who prefer a single coordinator to own a project board can explicitly designate one with `take-coordinator -orc`. The designation identifies whom to consult about the board. Each project defines the board, its location and the work its owner may authorize.

## Baseline before this change

At revision `6391811`, MHB resolves coordinator and inbox bindings by **project and harness**. The coordinator chat is that harness’s home for coordinating its workers and handling bridge messages routed through its inbox. Other harnesses in the project retain their own coordinators. A plain takeover carries no project-wide rank. That revision has no project board-owner designation or supported `-orc` option. The public CLI offers `--take coordinator` and `--take bridge`; native adapters also expose role operations and verify the calling chat. Worker management remains native to each harness. [Role storage](../../src/multi_harness_bridge/store.py) · [CLI](../../src/multi_harness_bridge/skills.py) · [Native role invocation](../skills.md) · [Operations](../operations.md)

## Proposed behavior

| User action | Result |
| --- | --- |
| Set up or upgrade a project | Start with no designated orchestrator. Skip any board setup. All existing role and mailbox operations remain available. |
| Invoke plain `take-coordinator` | Bind the current verified chat as coordinator for this harness and project. The command may be used by every harness independently. |
| Invoke `take-coordinator -orc` | Bind the verified chat as its harness coordinator and designate that exact chat as the project’s board owner. A subsequent explicit invocation in another harness transfers ownership. Repeating the same request in the same chat is idempotent. |
| Replace the board owner’s chat using a plain takeover | Complete the harness takeover. Report the previous designation as stale, with no effective board owner. The replacement can opt in explicitly. Other harnesses’ takeovers leave a valid owner alone. |
| Inspect project status | Show the coordinator for each harness and a separate optional board-owner field. Distinguish never designated, active, and stale. A stale or vacant designation never blocks normal work. |
| Stop using a board owner | Provide an explicit user operation to clear the designation and retain every harness binding. Review its command spelling before implementation. |

The ordinary inbox paths, worker ownership, approvals and access controls keep their existing meaning in both modes. The board owner receives messages addressed to its own harness through the usual route. Selecting an owner neither redirects all project traffic nor grants the chat authority to execute another harness’s work.

## Implementation plan after approval

1. **Specify invocation and verification.** Support the exact standalone `-orc` token in the coordinator invocation across Codex, Claude, Devin and Antigravity. Preserve each host’s existing invocation syntax and optional quoted title. A title containing “-orc”, discussion of the flag, an inbox message or a model-supplied tool argument cannot opt in. Bind proof of the user’s explicit option to the same project, session and handoff request. Reject the flag on inbox takeover.
2. **Keep the project role separate from harness bindings.** Add one optional designation per project, identifying the harness and verified coordinator session. Read through the existing binding provider when determining whether the designation is effective. Put transfer and status logic in the shared core. Keep native proof and syntax in the adapters. Include no project-specific names, board paths or task policies.
3. **Make handoff recoverable.** Serialize competing transfers and record the previous and new designation. For bindings and ownership stored together, commit them atomically. For an external binding provider, use a recoverable handoff and verify the binding before exposing the new owner. Test interrupted transfers and retries. Status must never report two effective owners or a chat that no longer holds its harness binding.
4. **Make the result discoverable.** Return the effective designation to the invoker and expose it in project status and handoff summaries. Use the ordinary bridge path for a concise ownership-change notice to participating harnesses. A delayed notice can be reconciled from status. Preserve compatibility for clients that only read their existing harness role fields.
5. **Document both workflows side by side.** Show one example with multiple independent coordinators and a second with an explicitly designated board owner. Keep ordinary takeover as the default in quick-start instructions. Document transfer, rotation, clear and recovery.

## Acceptance checks

- **Opt-out is a complete operating mode.** Start a new project and upgrade an existing one with several coordinators. Without ever supplying the flag, rotate chats, coordinate workers and exchange messages. Verify zero board owners, no ownership-change notices and no new setup prompt or permission requirement.
- **Explicit opt-in works across hosts.** Exercise plain and flagged invocations in each supported native harness. Confirm a real flagged invocation sets the owner, a plain invocation cannot elevate a chat, and a quoted title remains a title. A client unable to verify the flag must decline the designation.
- **Ownership stays scoped.** Transfer the board between two harnesses, rotate the owning chat without the flag, explicitly reassign it, then clear it. Inspect status from a different harness after each operation. Verify a second project is unaffected throughout.
- **Failures preserve a truthful result.** Exercise concurrent transfers, interrupted native handoffs, retries, failed verification and a missed peer notice. Confirm one effective owner at most, a usable mailbox throughout and sufficient history to explain the final state.

Publishing a release is a separate step from this implementation and its verification.
