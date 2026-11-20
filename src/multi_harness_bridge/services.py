"""Generate per-user native wake service definitions for the current install."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import platform
import plistlib
import sys
import xml.etree.ElementTree as ET

from . import config


def _clean(value: str, what: str) -> str:
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{what} contains an unsupported control character")
    return value


def wake_argv(executable: str | Path, project: str) -> list[str]:
    """Use the interpreter running this installed package, even from a venv."""
    config.identifier(project)
    return [_clean(executable, "Executable"), "-m", "multi_harness_bridge.wake", "--project", project]


def _exec(argv: list[str]) -> str:
    """Quote one systemd ExecStart argv without invoking a shell."""
    def quote(arg: str) -> str:
        arg = _clean(arg, "systemd argument")
        arg = arg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%")
        return f'"{arg}"'
    return " ".join(quote(arg) for arg in argv)


def render_systemd(project: str, root: str | Path, executable: str | Path, state_home: str | Path) -> str:
    root = _clean(Path(root).resolve(), "Project root")
    state_home = _clean(Path(state_home).resolve(), "State directory")
    argv = wake_argv(executable, project)
    return ("[Unit]\n"
            f"Description=Multi Harness Bridge native wake monitor ({project})\n"
            "After=default.target\n\n"
            "[Service]\nType=simple\n"
            f"WorkingDirectory={_exec([root])}\n"
            f"Environment={_exec(['MHB_HOME=' + state_home])}\n"
            f"ExecStart={_exec(argv)}\nRestart=on-failure\nRestartSec=5\n\n"
            "[Install]\nWantedBy=default.target\n")


def render_launchd(project: str, root: str | Path, executable: str | Path,
                   state_home: str | Path) -> bytes:
    config.identifier(project)
    root = _clean(Path(root).resolve(), "Project root")
    state_home = _clean(Path(state_home).resolve(), "State directory")
    label = f"org.multi-harness-bridge.wake.{project}"
    payload = {
        "Label": label,
        "ProgramArguments": wake_argv(executable, project),
        "WorkingDirectory": root,
        "EnvironmentVariables": {"MHB_HOME": state_home},
        "RunAtLoad": True,
        "KeepAlive": True,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def render_task_xml(project: str, root: str | Path, executable: str | Path,
                    state_home: str | Path, user: str | None = None) -> bytes:
    config.identifier(project)
    root = _clean(Path(root).resolve(), "Project root")
    state_home = _clean(Path(state_home).resolve(), "State directory")
    command = _clean(executable, "Executable")
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)

    def node(parent, name, text=None, attrs=None):
        element = ET.SubElement(parent, f"{{{ns}}}{name}", attrs or {})
        if text is not None:
            element.text = str(text)
        return element

    task = ET.Element(f"{{{ns}}}Task", {"version": "1.4"})
    reg = node(task, "RegistrationInfo")
    node(reg, "Description", f"Multi Harness Bridge native wake monitor ({project})")
    triggers = node(task, "Triggers")
    logon = node(triggers, "LogonTrigger")
    node(logon, "Enabled", "true")
    principal = node(task, "Principals")
    current = node(principal, "Principal", attrs={"id": "Author"})
    user = user or (f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
                    if os.environ.get("USERDOMAIN") and os.environ.get("USERNAME")
                    else getpass.getuser())
    node(current, "UserId", _clean(user, "Windows user"))
    node(current, "LogonType", "InteractiveToken")
    node(current, "RunLevel", "LeastPrivilege")
    settings = node(task, "Settings")
    node(settings, "MultipleInstancesPolicy", "IgnoreNew")
    node(settings, "DisallowStartIfOnBatteries", "false")
    node(settings, "StopIfGoingOnBatteries", "false")
    node(settings, "StartWhenAvailable", "true")
    node(settings, "ExecutionTimeLimit", "PT0S")
    node(settings, "Enabled", "true")
    actions = node(task, "Actions", attrs={"Context": "Author"})
    exec_action = node(actions, "Exec")
    node(exec_action, "Command", command)
    node(exec_action, "WorkingDirectory", root)
    # Task Scheduler stores one Windows command line here. Use a Python -c
    # launcher because Task Scheduler actions have no environment-variable field.
    import subprocess
    code = ("import os,runpy,sys; os.environ['MHB_HOME']=" + repr(state_home) +
            "; sys.argv=['multi-harness-wake',*sys.argv[1:]]; "
            "runpy.run_module('multi_harness_bridge.wake',run_name='__main__')")
    node(exec_action, "Arguments", subprocess.list2cmdline(["-c", code, "--project", project]))
    return ET.tostring(task, encoding="utf-8", xml_declaration=True)


def _current_executable() -> str:
    """Resolve the current install's Python, independent of service PATH."""
    return str(Path(sys.executable).resolve())


def _platform_name(value: str | None) -> str:
    value = (value or platform.system()).lower()
    if value in {"darwin", "mac", "macos"}:
        return "macos"
    if value in {"linux", "windows"}:
        return value
    raise ValueError(f"Unsupported service platform: {value}")


def default_output_dir(name: str) -> Path:
    home = Path.home()
    if name == "macos":
        return home / "Library" / "LaunchAgents"
    if name == "linux":
        return home / ".config" / "systemd" / "user"
    return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "MultiHarnessBridge" / "Tasks"


def generate(*, platform_name: str | None = None, output_dir: str | Path | None = None,
             executable: str | Path | None = None, state=None) -> list[Path]:
    """Write deterministic per-project unit files without enabling them."""
    selected = _platform_name(platform_name)
    state = state or config.load()
    if not state.projects:
        raise ValueError("Register at least one project before generating services")
    output = Path(output_dir).expanduser() if output_dir is not None else default_output_dir(selected)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    python = str(Path(executable or _current_executable()).expanduser().resolve())
    created = []
    for project, roots in sorted(state.projects.items()):
        if not roots:
            continue
        root = roots[0]
        if selected == "macos":
            path = output / f"org.multi-harness-bridge.wake.{project}.plist"
            data = render_launchd(project, root, python, state.root)
        elif selected == "linux":
            path = output / f"multi-harness-bridge-wake-{project}.service"
            data = render_systemd(project, root, python, state.root).encode("utf-8")
        else:
            path = output / f"MultiHarnessBridge-Wake-{project}.xml"
            data = render_task_xml(project, root, python, state.root)
        path.write_bytes(data)
        created.append(path)
    return created


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("generate", nargs="?", choices=("generate",), default="generate")
    parser.add_argument("--platform", dest="platform_name", help="override detected OS for packaging and inspection")
    parser.add_argument("--output-dir", type=Path, help="write generated files here instead of the OS user service directory")
    args = parser.parse_args(argv)
    try:
        result = generate(platform_name=args.platform_name, output_dir=args.output_dir)
        print(json.dumps({"generated": [str(path) for path in result]}, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        parser.exit(1, f"multi-harness-service: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
