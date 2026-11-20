from multi_harness_bridge.user_skills import install


def test_fresh_user_and_rerun(tmp_path):
    first = list(install(tmp_path, {}))
    assert len(first) == 12
    for _, target, status in first:
        assert status == "installed"
        assert f"name: {target.name}" in (target / "SKILL.md").read_text()
    assert all(status == "preserved" for _, _, status in install(tmp_path, {}))


def test_preserve_collisions_and_legacy_entries(tmp_path):
    legacy = tmp_path / ".codex/skills/take-coordinator"
    legacy.mkdir(parents=True)
    existing = legacy / "SKILL.md"
    existing.write_text("custom coordinator")
    dangling = tmp_path / ".claude/skills/take-bridge"
    dangling.parent.mkdir(parents=True)
    dangling.symlink_to(tmp_path / "offline-skills")
    list(install(tmp_path, {}))
    assert existing.read_text() == "custom coordinator"
    assert not (tmp_path / ".agents/skills/take-coordinator").exists()
    assert dangling.is_symlink()
    assert not dangling.exists()
