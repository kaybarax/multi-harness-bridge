"""Provision bundled skills in user catalogs, preserving every existing entry."""
import os
from pathlib import Path

from .propagation import BUILTIN_ROOT


def install(home, env):
    codex_home = Path(env.get("CODEX_HOME") or home / ".codex")
    claude_home = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    config_home = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    catalogs = {
        "codex": home / ".agents/skills",
        "claude": claude_home / "skills",
        "devin": config_home / "devin/skills",
        "antigravity": home / ".gemini/config/skills",
    }
    for harness, catalog in catalogs.items():
        for bundle in sorted(BUILTIN_ROOT.iterdir()):
            skill = bundle / "SKILL.md"
            if not skill.is_file():
                continue
            target = catalog / bundle.name
            legacy = codex_home / "skills" / bundle.name
            if harness == "codex" and os.path.lexists(legacy):
                target = legacy
            if os.path.lexists(target):
                yield harness, target, "preserved"
                continue
            data = skill.read_bytes()
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.mkdir()
            except FileExistsError:
                yield harness, target, "preserved"
                continue
            with (target / "SKILL.md").open("xb") as out:
                out.write(data)
            yield harness, target, "installed"


def main():
    for harness, target, status in install(Path.home(), os.environ):
        print(f"{harness}: {status} {target}")


if __name__ == "__main__":
    main()
