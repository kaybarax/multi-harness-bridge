"""Run an existing Devin handoff MCP with a two-workspace manifest refresh."""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import sys

from . import config, legacy, reconcile


def serve_handoff(cfg, project, *, route_module=None):
    provider = legacy.load(cfg)
    if provider is None or provider.project != project:
        raise ValueError("The existing Devin handoff needs a configured legacy project")
    if route_module is None:
        sys.path.insert(0, str(provider.installation))
        route_module = importlib.import_module("devin_local_route_mcp")
        if Path(route_module.__file__).resolve() != provider.installation / "devin_local_route_mcp.py":
            raise ValueError("The Devin handoff module is outside the configured installation")

    def refresh_after_handoff(legacy_project, _native_store):
        if legacy_project.name != project or legacy_project.workspace.resolve() not in cfg.projects[project]:
            raise ValueError("The native Devin handoff selected a different project or workspace")
        reconcile.reconcile_devin(cfg, project, apply=True)
        return str(legacy_project.workspace / reconcile.DEVIN_MANIFEST)

    original = route_module._refresh_role_manifest
    route_module._refresh_role_manifest = refresh_after_handoff
    try:
        route_module.serve_role_handoff(project)
    finally:
        route_module._refresh_role_manifest = original


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("project")
    parser.add_argument("action", choices=("serve-handoff",))
    args = parser.parse_args(argv)
    os.environ["MHB_HOME"] = str(args.home.resolve())
    serve_handoff(config.load(args.home), args.project)


if __name__ == "__main__":
    main()
