---
name: propagate-skills
description: Provision project skills or the configured project entry while preserving conflicts and local edits.
---

# Propagate Skills

Run from the project workspace and name source harnesses explicitly. The destination is the current invoking harness, established by a trusted native adapter. Never use a caller-supplied harness or environment variable as identity evidence. Resolve project from the current workspace; an optional hint may only disambiguate projects that match.

For example, when the user names Codex and Claude, run `multi-harness-skills --from codex claude` in the active registered workspace. In Antigravity Desktop, invoke the project's `propagate_skills` MCP tool with the exact named sources; the native PreToolUse hook verifies the current project conversation and requires the tool's source list to match the current user's explicit request. The active workspace, not registration order, determines the destination.

If the active workspace contains `.mhb/project-skills.json`, the same request provisions its configured, revision-matched entry and adjacent index. The entry points to project-owned procedures listed in that index. Verify the declared paths and report missing or stale dependencies; do not install a broken entry. Source-only skills are reported as `needs-project-review` and are not imported. If the active workspace has no declaration, the existing per-skill copy behavior applies. Report which skills were installed, already available, or held for review.

The installer considers project-local skill directories in the registered project roots and global skill directories only when a skill's canonical path is inside those roots. Same-name equal-content skills are deduplicated. Same-name differing content is reported as a source conflict and skipped. Existing untracked files, symlinks, and local edits are preserved and reported. A manifest records provenance and installed hashes so unchanged managed copies can be safely updated. Review conflicts and review items in the JSON result.

The installer blocks skills with detected references to other harnesses. A host-specific reference requires a reviewed adaptation for the destination. Portable instructions can be installed directly. Run `mhb skill-entry refresh PROJECT` after editing a committed entry, index, or procedure so its pinned hashes match the new revision. Do not delete a destination or manifest to force an update.
