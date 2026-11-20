import plistlib
import shutil
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from multi_harness_bridge.config import Config
from multi_harness_bridge.services import (
    generate, render_launchd, render_systemd, render_task_xml, wake_argv,
)


def _ns(tag):
    return f"{{http://schemas.microsoft.com/windows/2004/02/mit/task}}{tag}"


def test_renderers_escape_machine_specific_paths(tmp_path):
    root = tmp_path / 'work space' / 'project "quoted" & $100%'
    state = tmp_path / 'state $ special%'
    executable = tmp_path / 'venv with space' / 'python "local"'
    project = "demo_project"

    plist = plistlib.loads(render_launchd(project, root, executable, state))
    assert plist["ProgramArguments"] == [str(executable), "-m", "multi_harness_bridge.wake", "--project", project]
    assert plist["WorkingDirectory"] == str(root)
    assert plist["EnvironmentVariables"]["MHB_HOME"] == str(state)

    systemd = render_systemd(project, root, executable, state)
    assert "WorkingDirectory=" + str(root).replace("%", "%%") in systemd
    assert '"--project" "demo_project"' in systemd
    assert '"MHB_HOME=' in systemd and "state $ special" in systemd and "%%" in systemd

    xml = ET.fromstring(render_task_xml(project, root, executable, state, user="Local User"))
    assert xml.find(f"./{_ns('Principals')}/{_ns('Principal')}/{_ns('UserId')}").text == "Local User"
    action = xml.find(f"./{_ns('Actions')}/{_ns('Exec')}")
    assert action.find(_ns("Command")).text == str(executable)
    assert action.find(_ns("WorkingDirectory")).text == str(root)
    args = action.find(_ns("Arguments")).text
    assert "MHB_HOME" in args and "state $ special%" in args and "--project" in args and project in args


def test_generate_is_idempotent_and_uses_one_file_per_project(tmp_path):
    project_a = tmp_path / "project a"
    project_b = tmp_path / "project b"
    project_a.mkdir()
    project_b.mkdir()
    state = Config(tmp_path / "state", {"alpha": (project_a,), "beta": (project_b,)})
    output = tmp_path / "generated"
    first = generate(platform_name="linux", output_dir=output, executable=tmp_path / "python", state=state)
    contents = {path.name: path.read_bytes() for path in first}
    second = generate(platform_name="linux", output_dir=output, executable=tmp_path / "python", state=state)
    assert first == second
    assert {path.name: path.read_bytes() for path in second} == contents
    assert sorted(path.name for path in first) == [
        "multi-harness-bridge-wake-alpha.service", "multi-harness-bridge-wake-beta.service"]
    assert "WorkingDirectory=" + str(project_a) in contents[first[0].name].decode()


def test_rejects_control_characters_and_empty_registry(tmp_path):
    with pytest.raises(ValueError, match="control character"):
        render_systemd("demo", "/tmp/bad\nroot", "/usr/bin/python", "/tmp/state")
    with pytest.raises(ValueError, match="Register at least one"):
        generate(platform_name="linux", output_dir=tmp_path, state=Config(tmp_path, {}))


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("plutil"), reason="requires macOS plutil")
def test_plist_passes_macos_linter(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    path = tmp_path / "service.plist"
    path.write_bytes(render_launchd("demo", root, "/usr/bin/python3", tmp_path / "state"))
    import subprocess
    result = subprocess.run(["plutil", "-lint", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout



def test_service_preserves_virtualenv_interpreter_path(tmp_path, monkeypatch):
    from multi_harness_bridge.services import _current_executable
    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    try:
        executable.symlink_to(sys.executable)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    monkeypatch.setattr(sys, "executable", str(executable))
    assert _current_executable() == str(executable)
    state = Config(tmp_path / "state", {"sample": (tmp_path,)})
    unit, = generate(platform_name="macos", output_dir=tmp_path / "units", state=state)
    assert plistlib.loads(unit.read_bytes())["ProgramArguments"][0] == str(executable)
