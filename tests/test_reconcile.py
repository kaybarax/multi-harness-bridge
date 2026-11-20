import json

import pytest

from multi_harness_bridge import config, devin_tools, reconcile


def setup_roots(tmp_path):
    roots = tuple(tmp_path / name for name in ("docs", "dev", "unrelated"))
    for root in roots:
        (root / ".devin").mkdir(parents=True)
    state = tmp_path / "state"
    cfg = config.register("maple", roots[:2], state)
    return config.register("other", roots[2:], cfg.root), tuple(
        root / reconcile.DEVIN_MANIFEST for root in roots
    )


def write(path, servers):
    path.write_text(json.dumps({"mcpServers": servers, "nativeSetting": True}))


def read(path):
    data = json.loads(path.read_text())
    assert data["nativeSetting"]
    return data["mcpServers"]


def test_reconcile_uses_role_evidence_and_preserves_unrelated_entries(tmp_path):
    cfg, (docs, dev, other) = setup_roots(tmp_path)
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy_python = str(legacy_root / ".venv/bin/python")
    relay = str(legacy_root / "devin_local_relay.py")
    route = str(legacy_root / "devin_local_route_mcp.py")
    old_python, candidate = tmp_path / "old/python", tmp_path / "candidate/python"
    portable = devin_tools.server_name("maple")
    unowned = {"command": "/other/server", "args": []}
    write(docs, {
        "maple-local-inbox": unowned,
        "maple-local-coordinator": {"command": legacy_python, "args": [relay, "serve-coordinator", "old-coordinator"]},
        "devin-maple-route-inbox": {"command": legacy_python, "args": [route, "inbox", "maple", "old-inbox"]},
        "another-project": unowned,
    })
    write(dev, {
        portable: devin_tools.installation_entry(cfg, "maple", executable=old_python),
        "devin-maple-route-coordinator": {
            "command": legacy_python, "args": [route, "coordinator", "maple", "old-coordinator"],
        },
    })
    write(other, {portable: unowned})
    before = tuple(path.read_bytes() for path in (docs, dev, other))
    options = {
        "executable": candidate, "predecessor": old_python, "legacy_root": legacy_root,
        "roles": {"coordinator": "acp/devin-cli/new-coordinator", "inbox": "acp/devin-cli/new-inbox"},
    }
    changes = reconcile.plan_devin(cfg, "maple", **options)
    assert tuple(path.read_bytes() for path in (docs, dev, other)) == before
    assert {change.path for change in changes} == {docs, dev}
    assert all(change.before_digest for change in changes)
    reconcile.apply_devin(changes)
    assert read(docs)["maple-local-inbox"] == read(docs)["another-project"] == unowned
    assert read(docs)["maple-local-coordinator"]["args"][-1] == options["roles"]["coordinator"]
    assert read(docs)["devin-maple-route-inbox"]["args"][-1] == options["roles"]["inbox"]
    assert read(dev)["devin-maple-route-coordinator"]["args"][-1] == options["roles"]["coordinator"]
    assert read(docs)[portable] == read(dev)[portable] == devin_tools.installation_entry(
        cfg, "maple", executable=candidate,
    )
    assert other.read_bytes() == before[-1]
    assert reconcile.plan_devin(cfg, "maple", **options) == ()


def test_reconcile_fails_closed_on_conflict_or_stale_preview(tmp_path):
    cfg, (docs, dev, _) = setup_roots(tmp_path)
    changes = reconcile.plan_devin(cfg, "maple")
    write(dev, {devin_tools.server_name("maple"): {"command": "/foreign/server"}})
    with pytest.raises(ValueError, match="after preview"):
        reconcile.apply_devin(changes)
    assert not docs.exists()
    with pytest.raises(ValueError, match="Unrecognized"):
        reconcile.plan_devin(cfg, "maple")
    assert not docs.exists()


def test_reconcile_rejects_manifest_symlink(tmp_path):
    cfg, (docs, dev, _) = setup_roots(tmp_path)
    write(dev, {})
    docs.symlink_to(dev)
    with pytest.raises(ValueError, match="symlinked"):
        reconcile.plan_devin(cfg, "maple")
    assert read(dev) == {}


def test_reconcile_normalizes_a_generated_state_alias_and_rejects_another_tenant(tmp_path):
    cfg, (docs, dev, _) = setup_roots(tmp_path)
    alias = tmp_path / "private state link"
    alias.symlink_to(cfg.root, target_is_directory=True)
    name = devin_tools.server_name("maple")
    installed = devin_tools.installation_entry(cfg, "maple")
    alias_entry = {**installed, "args": [*installed["args"][:3], str(alias), *installed["args"][4:]]}
    write(docs, {name: alias_entry})
    write(dev, {name: installed})
    changes = reconcile.plan_devin(cfg, "maple")
    assert [change.path for change in changes] == [docs]
    reconcile.apply_devin(changes)
    assert read(docs)[name] == installed
    assert not reconcile.plan_devin(cfg, "maple")

    unrelated_root = tmp_path / "another state"
    unrelated_root.mkdir()
    alias.unlink()
    alias.symlink_to(unrelated_root, target_is_directory=True)
    write(docs, {name: alias_entry})
    with pytest.raises(ValueError, match="Unrecognized"):
        reconcile.plan_devin(cfg, "maple")
    assert read(docs)[name] == alias_entry
