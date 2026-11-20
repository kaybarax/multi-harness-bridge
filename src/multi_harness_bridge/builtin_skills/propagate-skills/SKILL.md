---
name: propagate-skills
description: Copy explicitly selected project-scoped skills to the current harness while preserving conflicts and local edits.
---

# Propagate Skills

Run from the project workspace and name source harnesses explicitly. The destination is the current invoking harness, established by a trusted native adapter. Never use a caller-supplied harness or environment variable as identity evidence. Resolve project from the current workspace; an optional hint may only disambiguate projects that match.

For example, when the user names Codex and Claude, run `multi-harness-skills --from codex claude` in the registered workspace. Report which skills were installed, already available, or held for review.

The installer considers project-local skill directories in the registered project roots and global skill directories only when a skill's canonical path is inside those roots. Same-name equal-content skills are deduplicated. Same-name differing content is reported as a source conflict and skipped. Existing untracked files, symlinks, and local edits are preserved and reported. A manifest records provenance and installed hashes so unchanged managed copies can be safely updated. Review conflicts and review items in the JSON result.

The installer blocks skills with detected references to other harnesses. A host-specific reference requires a reviewed adaptation for the destination. Portable instructions can be installed directly. Do not delete a destination or manifest to force an update.
