import hashlib
import json
from pathlib import Path

import pytest

from multi_harness_bridge import cli, config, project_entry
from multi_harness_bridge.config import Config
from multi_harness_bridge.propagation import HARNESS_DIRS, bootstrap
from multi_harness_bridge.store import Store


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def declare(root: Path, name="workflow-entry", *, procedures=None, dependencies=None):
    folder = root / "agents/skills" / name
    entry = write(folder / "SKILL.md", "---\nname: workflow-entry\n---\n\nRead index.json and follow the selected procedure.\n")
    procedures = procedures or []
    index = write(folder / "index.json", json.dumps({
        "version": 1,
        "procedures": procedures,
        **({"dependencies": dependencies} if dependencies is not None else {}),
    }, sort_keys=True, indent=2) + "\n")
    write(root / ".mhb/project-skills.json", json.dumps({
        "version": 1,
        "entry": {"name": name, "path": entry.relative_to(root).as_posix(), "sha256": sha(entry)},
        "index": {"path": index.relative_to(root).as_posix(), "sha256": sha(index)},
    }, sort_keys=True, indent=2) + "\n")
    return entry, index


def procedure(root: Path, name="review-process", text="Review the project changes."):
    path = write(root / "agents/skills/workflow-entry" / f"{name}.md", text + "\n")
    return {"name": name, "path": path.relative_to(root).as_posix(), "sha256": sha(path)}


def test_provisions_configured_entry_only_and_rerun_preserves_edits(tmp_path):
    docs = tmp_path / "docs"
    dev = tmp_path / "dev"
    docs.mkdir()
    dev.mkdir()
    declared = procedure(dev)
    declare(dev, procedures=[declared])
    cfg = Config(tmp_path / "state", {"sample": (docs, dev)})
    source = write(dev / ".claude/skills/new-check/SKILL.md", "---\nname: new-check\n---\nFresh local procedure.\n")

    first = project_entry.install("sample", ["claude"], "codex", workspace=dev, cfg=cfg)
    second = project_entry.install("sample", ["claude"], "codex", workspace=dev, cfg=cfg)

    destination = dev / ".agents/skills/workflow-entry"
    assert first["destination"] == str(destination)
    assert first["candidates"][0]["status"] == "needs-project-review"
    assert first["status"] == "complete-with-review"
    assert second["skills"][0]["status"] == "already-current"
    assert sorted(path.name for path in destination.iterdir()) == ["SKILL.md", "index.json"]
    assert not (docs / ".agents/skills/workflow-entry").exists()
    (destination / "SKILL.md").write_text("local edit\n")
    third = project_entry.install("sample", ["claude"], "codex", workspace=dev, cfg=cfg)
    assert third["conflicts"][0]["status"] == "conflict-local-edits"
    assert (destination / "SKILL.md").read_text() == "local edit\n"
    assert source.is_file()


def test_active_workspace_selects_registered_root_independent_of_order(tmp_path):
    docs = tmp_path / "docs"
    dev = tmp_path / "dev"
    docs.mkdir()
    dev.mkdir()
    declare(docs)
    declare(dev)
    cfg = Config(tmp_path / "state", {"sample": (dev, docs)})

    report = project_entry.install("sample", ["claude"], "devin", workspace=docs, cfg=cfg)

    assert report["active_workspace"] == str(docs)
    assert (docs / ".agents/skills/workflow-entry/SKILL.md").is_file()
    assert not (dev / ".agents/skills/workflow-entry").exists()


def test_candidate_inventory_does_not_cross_registered_projects(tmp_path):
    root = tmp_path / "project"
    unrelated = tmp_path / "unrelated"
    root.mkdir()
    unrelated.mkdir()
    declare(root)
    write(unrelated / ".claude/skills/private/SKILL.md", "---\nname: private\n---\nOther project.\n")
    cfg = Config(tmp_path / "state", {"sample": (root,), "other": (unrelated,)})

    report = project_entry.install("sample", ["claude"], "codex", workspace=root, cfg=cfg)

    assert report["candidates"] == []
    assert not (unrelated / ".agents/skills/workflow-entry").exists()


def test_exact_native_symlink_is_already_current_but_other_symlinks_conflict(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    entry, _ = declare(root)
    target = root / ".agents/skills/workflow-entry"
    target.parent.mkdir(parents=True)
    target.symlink_to(entry.parent, target_is_directory=True)
    cfg = Config(tmp_path / "state", {"sample": (root,)})

    current = project_entry.install("sample", ["claude"], "codex", workspace=root, cfg=cfg)
    assert current["skills"][0]["status"] == "already-current"
    target.unlink()
    unrelated = root / "unrelated-entry"
    unrelated.mkdir()
    (unrelated / "SKILL.md").write_text("unrelated\n")
    target.symlink_to(unrelated, target_is_directory=True)
    conflict = project_entry.install("sample", ["claude"], "codex", workspace=root, cfg=cfg)

    assert conflict["skills"][0]["status"] == "conflict-symlink"
    assert target.resolve() == unrelated


@pytest.mark.parametrize("host", ["codex", "claude", "devin", "antigravity"])
def test_configured_entry_uses_each_native_picker_adapter(tmp_path, host):
    root = tmp_path / "project"
    root.mkdir()
    declare(root)
    cfg = Config(tmp_path / "state", {"sample": (root,)})

    report = project_entry.install("sample", ["codex"], host, workspace=root, cfg=cfg)

    destination = root / HARNESS_DIRS[host] / "workflow-entry"
    assert report["skills"][0]["status"] == "installed"
    assert (destination / "SKILL.md").is_file()
    assert (destination / "index.json").is_file()


def test_candidate_in_registered_noncanonical_source_is_reviewed(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    declare(root)
    source = write(root / "procedures/source-collection/new-check/SKILL.md",
                   "---\nname: new-check\n---\nNew procedure for review.\n")
    write(root / ".bridge-skills/sources.json", json.dumps({"codex": ["procedures/source-collection"]}))
    cfg = Config(tmp_path / "state", {"sample": (root,)})

    report = project_entry.install("sample", ["codex"], "devin", workspace=root, cfg=cfg)

    assert report["candidates"][0]["skill"] == "new-check"
    assert report["candidates"][0]["status"] == "needs-project-review"
    assert report["candidates"][0]["source_harnesses"] == ["codex"]
    assert source.is_file()


def test_cross_workspace_dependency_requires_one_registered_provider(tmp_path):
    docs = tmp_path / "docs"
    dev = tmp_path / "dev"
    docs.mkdir()
    dev.mkdir()
    _, dev_index = declare(dev, procedures=[procedure(dev)])
    declare(docs, dependencies=[{"path": dev_index.relative_to(dev).as_posix(),
                                 "scope": "other-registered-workspace"}])
    cfg = Config(tmp_path / "state", {"sample": (docs, dev)})

    ready = project_entry.install("sample", ["codex"], "claude", workspace=docs, cfg=cfg)
    missing_docs = tmp_path / "docs-only"
    missing_docs.mkdir()
    declare(missing_docs, dependencies=[{"path": dev_index.relative_to(dev).as_posix(),
                                        "scope": "other-registered-workspace"}])
    missing_cfg = Config(tmp_path / "state-missing", {"sample": (missing_docs,)})
    missing = project_entry.install("sample", ["codex"], "claude", workspace=missing_docs, cfg=missing_cfg)

    assert ready["status"] == "complete"
    assert ready["procedures"][0]["workspace"] == str(dev)
    assert (docs / ".claude/skills/workflow-entry/index.json").is_file()
    assert missing["status"] == "missing-dependency"
    assert not (missing_docs / ".claude/skills/workflow-entry").exists()


def test_stale_index_is_non_success_and_does_not_install(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    entry, index = declare(root, procedures=[procedure(root)])
    declaration_path = root / ".mhb/project-skills.json"
    declaration = json.loads(declaration_path.read_text())
    write(index, index.read_text() + " ")
    declaration_path.write_text(json.dumps(declaration))
    cfg = Config(tmp_path / "state", {"sample": (root,)})

    with pytest.raises(ValueError, match="index revision is stale"):
        project_entry.install("sample", ["codex"], "claude", workspace=root, cfg=cfg)
    assert not (root / ".claude/skills/workflow-entry").exists()
    assert entry.is_file()


def test_configured_name_must_match_native_skill_frontmatter(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    entry, _ = declare(root, name="workflow-entry")
    entry.write_text("---\nname: another-entry\n---\nRead index.json.\n")
    declaration_path = root / ".mhb/project-skills.json"
    declaration = json.loads(declaration_path.read_text())
    declaration["entry"]["sha256"] = sha(entry)
    declaration_path.write_text(json.dumps(declaration))
    cfg = Config(tmp_path / "state", {"sample": (root,)})

    with pytest.raises(ValueError, match="frontmatter name"):
        project_entry.install("sample", ["codex"], "claude", workspace=root, cfg=cfg)
    assert not (root / ".claude/skills/workflow-entry").exists()


def test_refresh_repins_entry_index_and_procedure_after_reviewed_edits(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    declared = procedure(root)
    entry, index = declare(root, procedures=[declared])
    procedure_path = root / declared["path"]
    procedure_path.write_text("Review the complete change set.\n")
    entry.write_text("---\nname: workflow-entry\n---\nUse the updated index.\n")
    cfg = Config(tmp_path / "state", {"sample": (root,)})

    refreshed = project_entry.refresh("sample", workspace=root, cfg=cfg)
    report = project_entry.install("sample", ["codex"], "codex", workspace=root, cfg=cfg)

    assert refreshed["status"] == "hashes-refreshed"
    assert report["skills"][0]["status"] == "installed"
    updated_index = json.loads(index.read_text())
    assert updated_index["procedures"][0]["sha256"] == sha(procedure_path)


def test_mhb_cli_exposes_hash_refresh_without_initializing_mailbox(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    declare(root, procedures=[procedure(root)])
    cfg = Config(tmp_path / "state", {"sample": (root,)})
    monkeypatch.setattr(cli.settings, "load", lambda: cfg)
    args = cli.build_parser().parse_args(["skill-entry", "refresh", "sample", "--workspace", str(root)])

    result = cli.run(args)

    assert result["status"] == "hashes-refreshed"


def test_conflicting_source_versions_and_portability_are_reported(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    declare(root)
    write(root / ".agents/skills/conflict/SKILL.md", "---\nname: conflict\n---\none\n")
    write(root / ".claude/skills/conflict/SKILL.md", "---\nname: conflict\n---\ntwo\n")
    write(root / ".claude/skills/host-specific/SKILL.md",
          "---\nname: host-specific\n---\nUse Claude's native editor.\n")
    cfg = Config(tmp_path / "state", {"sample": (root,)})

    report = project_entry.install("sample", ["codex", "claude"], "codex", workspace=root, cfg=cfg)

    assert report["conflicts"][0]["status"] == "conflict-sources"
    candidate = next(item for item in report["candidates"] if item["skill"] == "host-specific")
    assert candidate["status"] == "needs-project-review"
    assert candidate["portability_review"]


def test_native_builtins_are_not_adoption_candidates_and_bundles_need_review(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    cfg = Config(tmp_path / "state", {"sample": (root,)})
    bootstrap("sample", "codex", workspace=root, cfg=cfg)
    modified = root / ".agents/skills/take-coordinator"
    (modified / "SKILL.md").write_text((modified / "SKILL.md").read_text() + "Local adjustment.\n")
    bundle = root / ".claude/skills/review-bundle"
    skill_file = write(bundle / "SKILL.md", "---\nname: review-bundle\n---\nRead the indexed review steps.\n")
    write(bundle / "examples/sample.txt", "Companion material.\n")
    procedure_item = {"name": "review-bundle", "path": skill_file.relative_to(root).as_posix(),
                      "sha256": sha(skill_file)}
    declare(root, procedures=[procedure_item])
    cfg.root.mkdir()
    store = Store(cfg.database)
    store.bind("sample", "codex", "coordinator", "coordinator-session")
    store.bind("sample", "codex", "inbox", "inbox-session")

    report = project_entry.install("sample", ["codex", "claude"], "antigravity", workspace=root, cfg=cfg)

    names = {candidate["skill"] for candidate in report["candidates"]}
    assert "bridge-mailbox" not in names
    assert next(item for item in report["candidates"] if item["skill"] == "take-coordinator")["status"] == "needs-project-review"
    checked = next(item for item in report["candidates"] if item["skill"] == "review-bundle")
    assert checked["status"] == "needs-project-review"
    assert checked["unverified_companion_files"] == ["examples/sample.txt"]
    assert store.roles("sample", "codex") == {"coordinator": "coordinator-session", "inbox": "inbox-session"}
