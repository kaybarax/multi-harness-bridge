# Sharing project skills

From a chat in the destination workspace, invoke `propagate skills from Codex and Claude`. The current harness and registered project determine where files are installed. The named harnesses determine which sources are considered. Users can share project instructions before the project has a coordinator.

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
