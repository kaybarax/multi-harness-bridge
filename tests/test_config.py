import pytest

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
