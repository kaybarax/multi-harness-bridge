from concurrent.futures import ThreadPoolExecutor

import pytest

from multi_harness_bridge import config, propagation


def skill(root, text):
    path = root / "release-review"
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text("---\nname: release-review\n---\n" + text)
    return path


def test_portable_skills_reach_shared_hosts_and_concurrent_runs_converge(tmp_path, monkeypatch):
    root = tmp_path / "project"
    source = skill(root / ".claude/skills", "Compare results to the acceptance criteria.")
    monkeypatch.setenv("MHB_HOME", str(tmp_path / "state"))
    config.register("orchard", [root])
    with ThreadPoolExecutor(2) as pool:
        reports = list(pool.map(lambda host: propagation.install("orchard", ["claude"], host), ("devin", "antigravity")))
    assert sorted(r["skills"][0]["status"] for r in reports) == ["already-current", "installed"]
    assert propagation.digest(source) == propagation.digest(root/".agents/skills"/source.name)


def test_reviewed_variant_is_pinned_to_source_and_preserves_user_edits(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    source = skill(root / ".claude/skills", "Use Claude's native review tool.")
    monkeypatch.setenv("MHB_HOME", str(tmp_path/"state"))
    config.register("orchard", [root])
    def propagate():
        return propagation.install("orchard", ["claude"], "antigravity")
    result = propagate()
    assert result["review"][0]["source_hash"] == propagation.digest(source)
    reviewed = root/".bridge-skills/variants/shared-agents"/source.name
    (reviewed/"skill").mkdir(parents=True)
    (reviewed/"skill/SKILL.md").write_text("Use the host's documented review workflow.")
    (reviewed/"source.sha256").write_text(propagation.digest(source))
    assert propagate()["skills"][0]["status"] == "installed"
    (root/".agents/skills"/source.name/"SKILL.md").write_text("local edits")
    assert propagate()["conflicts"][0]["status"] == "conflict-local-edits"
    (source/"SKILL.md").write_text("Claude changed its procedure.")
    assert propagate()["review"][0]["status"] == "blocked-review"


def test_failed_replacement_keeps_previous_tree(tmp_path, monkeypatch):
    root = tmp_path/"workspace"
    source = skill(root/".claude/skills", "old")
    monkeypatch.setenv("MHB_HOME", str(tmp_path/"state"))
    config.register("orchard", [root])
    propagation.install("orchard", ["claude"], "devin")
    dest = root/".agents/skills"/source.name
    old_hash = propagation.digest(dest)
    (source/"SKILL.md").write_text("new")
    move = propagation.os.replace
    def broken(a,b):
        if a.name == "ready":
            raise OSError("injected publish failure")
        return move(a,b)
    monkeypatch.setattr(propagation.os, "replace", broken)
    with pytest.raises(OSError, match="publish failure"):
        propagation.install("orchard", ["claude"], "devin")
    assert propagation.digest(dest) == old_hash
