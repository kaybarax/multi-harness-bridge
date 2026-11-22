import json

import pytest

from multi_harness_bridge import config
from multi_harness_bridge.config import load, register


def test_nested_and_mixed_workspaces(tmp_path):
    home = tmp_path / "settings"
    one = tmp_path / "one"
    two = one / "other"
    two.mkdir(parents=True)
    register("one", [one], home)
    config = register("two", [two], home)
    assert load(home).resolve(two) == "two"
    assert config.resolve([one, two], "one") == "one"
    with pytest.raises(ValueError):
        config.resolve([one, two])
    with pytest.raises(ValueError):
        config.resolve(two, "one")
    with pytest.raises(ValueError):
        register("one", [two], home)


def test_options_missing_and_malformed_mean_off(tmp_path):
    assert config.load_options(tmp_path) == {}
    assert config.project_option(tmp_path, "sample", "codex", "coordinator_wake") is None
    (tmp_path / "options.json").write_text("{not json")
    assert config.load_options(tmp_path) == {}
    assert config.project_option(tmp_path, "sample", "codex", "coordinator_wake") is None
    for bad in ('{"version":2,"projects":{}}', '{"version":1,"projects":[]}', '"text"', '[]'):
        (tmp_path / "options.json").write_text(bad)
        assert config.load_options(tmp_path) == {}
    (tmp_path / "options.json").write_text(json.dumps({
        "version": 1, "projects": {"sample": {"codex": {"coordinator_wake": "agent-relay"}},
                                  "odd": "not-a-dict"}}))
    assert config.project_option(tmp_path, "sample", "codex", "coordinator_wake") == "agent-relay"
    assert config.project_option(tmp_path, "sample", "claude", "coordinator_wake") is None
    assert config.project_option(tmp_path, "odd", "codex", "coordinator_wake") is None
    assert config.project_option(tmp_path, "absent", "codex", "coordinator_wake") is None


def test_set_project_option_merges_and_prunes(tmp_path):
    config.set_project_option(tmp_path, "alpha", "codex", "coordinator_wake", "agent-relay")
    config.set_project_option(tmp_path, "alpha", "claude", "coordinator_wake", "queue")
    config.set_project_option(tmp_path, "beta", "codex", "other", "x")
    assert json.loads((tmp_path / "options.json").read_text()) == {
        "version": 1,
        "projects": {
            "alpha": {"claude": {"coordinator_wake": "queue"},
                      "codex": {"coordinator_wake": "agent-relay"}},
            "beta": {"codex": {"other": "x"}}}}
    # Rewriting the same value is a no-op merge, not a duplicate.
    config.set_project_option(tmp_path, "alpha", "codex", "coordinator_wake", "agent-relay")
    assert config.project_option(tmp_path, "alpha", "codex", "coordinator_wake") == "agent-relay"
    # Unsetting prunes empty harness and project parents.
    config.set_project_option(tmp_path, "alpha", "claude", "coordinator_wake", None)
    config.set_project_option(tmp_path, "beta", "codex", "other", None)
    assert json.loads((tmp_path / "options.json").read_text()) == {
        "version": 1,
        "projects": {"alpha": {"codex": {"coordinator_wake": "agent-relay"}}}}
    config.set_project_option(tmp_path, "alpha", "codex", "coordinator_wake", None)
    assert config.load_options(tmp_path) == {}


def test_set_project_option_rejects_bad_names(tmp_path):
    with pytest.raises(ValueError):
        config.set_project_option(tmp_path, "Bad", "codex", "key", "agent-relay")
    with pytest.raises(ValueError):
        config.set_project_option(tmp_path, "alpha", "codex", "Bad Key", "agent-relay")
    with pytest.raises(ValueError):
        config.set_project_option(tmp_path, "alpha", "codex", "key", "Not Valid!")
    assert not (tmp_path / "options.json").exists()


def test_provision_codex_workspace_merges_without_clobbering(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / ".codex").mkdir(parents=True)
    target = workspace / ".codex" / "config.toml"
    original = ('# operator comment\nmodel = "gpt-5"\n'
                'approval_policy = "never" # keep me\n\n[profiles.fast]\nmodel = "x"\n')
    target.write_text(original)
    first = config.provision_codex_workspace(workspace)
    assert first["status"] == "installed" and first["config"] == str(target)
    text = target.read_text()
    assert 'approval_policy = "on-request" # keep me' in text
    assert 'approvals_reviewer = "auto_review"' in text
    assert 'model = "gpt-5"' in text and "[profiles.fast]" in text and "# operator comment" in text
    # Top-level keys must land before the first table header to stay top-level.
    assert text.index("approvals_reviewer") < text.index("[profiles.fast]")
    second = config.provision_codex_workspace(workspace)
    assert second["status"] == "already-current"
    assert target.read_text() == text


def test_provision_codex_workspace_creates_missing_config(tmp_path):
    workspace = tmp_path / "fresh"
    workspace.mkdir()
    result = config.provision_codex_workspace(workspace)
    assert result["status"] == "installed"
    target = workspace / ".codex" / "config.toml"
    assert target.read_text() == 'approval_policy = "on-request"\napprovals_reviewer = "auto_review"\n'
    import tomllib
    assert tomllib.loads(target.read_text())["approvals_reviewer"] == "auto_review"


def test_provision_codex_workspace_refuses_invalid_toml(tmp_path):
    workspace = tmp_path / "broken"
    (workspace / ".codex").mkdir(parents=True)
    target = workspace / ".codex" / "config.toml"
    target.write_text("invalid = = toml\n")
    with pytest.raises(ValueError, match="not valid TOML"):
        config.provision_codex_workspace(workspace)
    assert target.read_text() == "invalid = = toml\n"
