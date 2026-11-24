# Sharing project skills

From a chat in the destination workspace, invoke `propagate skills from Codex and Claude`. The active registered workspace determines the destination; the order in the project registry does not. If several workspaces are registered and no active workspace can be established, propagation stops instead of guessing. The named harnesses determine which sources are considered. Users can share project instructions before the project has a coordinator.

In Antigravity Desktop, the project MCP server exposes `propagate_skills(sources)`. The Desktop hook verifies the native conversation against the registered project and accepts the call only when `sources` exactly matches the harnesses explicitly named in the current user request (for example, `@propagate-skills from Codex and Claude`). It uses the active native workspace when supplied by the host and reports ambiguity instead of guessing. This path works before the conversation is bound as a coordinator or inbox. The ordinary `multi-harness-skills` CLI remains for hosts that provide a trusted native identity to that process.

Skills live under `.agents/skills/<name>/SKILL.md` for Codex, Devin and Antigravity, and `.claude/skills/<name>/SKILL.md` for Claude Code. Three harnesses may already see the same directory. An in-place source remains user-owned. A source with the same name but different content across selected harnesses is reported as a conflict.

The installer searches registered workspace roots. Global harness skills are eligible when their canonical source is in the project or in a source root explicitly attributed to the project. If project skills live outside its checkout, create `.bridge-skills/sources.json` under the workspace and register **skill collection directories**, for example:

```json
{"codex": ["../shared-project-policies"], "claude": ["../shared-project-policies"]}
```

Each collection contains one subdirectory per skill. Review the registration before running the installer. Keep secrets outside all registered skill sources. Nested links, devices and special files need review before packaging. Byte-identical skills across selected sources are deduplicated.

## Reviewed adaptations

Portable instructions copy as written. If a bundle references another harness, the report lists the skill, source hash, and destination variant to review. A maintainer can translate those steps using verified tools for the destination. Keep the reviewed bundle at:

```text
.bridge-skills/variants/<variant>/<skill-name>/
  source.sha256
  skill/
    SKILL.md
    ...resources
```

Place the reported source hash in `source.sha256`. The variant is `claude` for the Claude directory and `shared-agents` for the directory read by Codex, Devin and Antigravity. For a shared variant, document each host's commands or conditions explicitly. Source changes invalidate the review. Avoid embedding project IDs, credentials and chat addresses in an adaptation.

Managed installations record their source and installed hashes in `.multi-harness-skills.json`. Repeated invocations with the same sources are idempotent. Concurrent installers serialize writes to the same physical directory. Local edits and conflicting untracked directories are reported. If installation is interrupted during file replacement, the old tree is retained under the destination's `.mhb-previous-<name>` until recovery. Compare that copy with the destination and manifest before removing it.

## One project entry

A project can opt into one native skill entry by committing
`.mhb/project-skills.json` in each workspace where the entry is available. A
declaration pins the entry and index files by SHA-256:

```json
{
  "version": 1,
  "entry": {"name": "project-workflows", "path": "agents/skills/project-workflows/SKILL.md", "sha256": "<64 lowercase hex characters>"},
  "index": {"path": "agents/skills/project-workflows/index.json", "sha256": "<64 lowercase hex characters>"}
}
```

The index uses version 1 and lists committed procedure files with their names,
workspace-relative paths and SHA-256 hashes. Additional routing fields are
preserved. Each declared path must stay inside its registered workspace and
must not traverse symlinks. Propagation verifies the index and procedure hashes
before installing the configured entry and its adjacent index in the current
harness's native skill directory. It does not edit project files or import
candidate procedures.

An index can declare a dependency such as
`{"path":"agents/skills/project-workflows/index.json","scope":"other-registered-workspace"}`.
The path must resolve to exactly one other registered workspace whose own
declaration pins that index. A missing or ambiguous dependency returns a
non-success result and installs no entry.

The regular `propagate skills from ...` request selects this mode automatically
when the active workspace contains the declaration. Without it, the existing
per-skill copy behavior remains available. Source skills from selected
harnesses are still inventoried: matching reviewed content is reported as
already indexed, differing same-name content is a conflict, and source-only
content is reported as `needs-project-review` for the project to adopt through
its normal review.

After editing an entry, index or procedure file, refresh its pins from the
registered workspace:

```sh
mhb skill-entry refresh PROJECT
```

This updates hashes in the local index and declaration. Review and commit those
project-owned file changes with the rest of the project change.
