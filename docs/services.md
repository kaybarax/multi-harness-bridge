# Per-user native wake services

`multi-harness-service generate` writes one native wake monitor definition for
each project in the local registry. It uses the Python interpreter that runs
the installed command, the project's first registered workspace root, and the
current bridge state directory. This lets the service resolve the project
while it polls for mailbox work. Generation is repeatable and only writes
files; it does not register, enable, start, or stop any OS service.

The files go to the current user's standard service directory:

| Platform | Generated file |
| --- | --- |
| macOS | `~/Library/LaunchAgents/org.multi-harness-bridge.wake.<project>.plist` |
| Linux | `~/.config/systemd/user/multi-harness-bridge-wake-<project>.service` |
| Windows | `%LOCALAPPDATA%\MultiHarnessBridge\Tasks\MultiHarnessBridge-Wake-<project>.xml` |

Run `multi-harness-service generate` after installing the package and
registering projects with `mhb project add`. To direct generated files to a
temporary or managed directory, add `--output-dir PATH`. `--platform macos`,
`--platform linux`, or `--platform windows` selects a renderer for inspection;
it does not change the host OS. Existing generated files with the same names
are replaced. No bot token or other secret is written to a service file.

Use the commands for the current OS to install or refresh the generated
definition, enable it, disable it, or reload a running service. Replace
`<project>` with the registered lowercase project identifier.

## macOS

The generated plist is already in LaunchAgents. Enable and load it for the
logged-in user. LaunchAgents use the launchd environment. If Codex or Devin
Local is installed outside its default `PATH`, configure the service's `PATH`
or supply an absolute executable through the [native adapter settings](native.md).
The plist's `MHB_HOME` should match the state directory used by the desktop
clients.

```sh
launchctl enable "gui/$(id -u)/org.multi-harness-bridge.wake.<project>"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/org.multi-harness-bridge.wake.<project>.plist"
```

Disable and unload it:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/org.multi-harness-bridge.wake.<project>.plist"
launchctl disable "gui/$(id -u)/org.multi-harness-bridge.wake.<project>"
```

Reload by unloading and bootstrapping again:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/org.multi-harness-bridge.wake.<project>.plist"
launchctl enable "gui/$(id -u)/org.multi-harness-bridge.wake.<project>"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/org.multi-harness-bridge.wake.<project>.plist"
```

## Linux

Load the generated unit definition and enable it at user login:

```sh
systemctl --user daemon-reload
systemctl --user enable --now multi-harness-bridge-wake-<project>.service
```

Disable and stop it:

```sh
systemctl --user disable --now multi-harness-bridge-wake-<project>.service
```

Reload the unit and restart the process:

```sh
systemctl --user daemon-reload
systemctl --user restart multi-harness-bridge-wake-<project>.service
```

## Windows

In PowerShell, import or replace the generated task definition:

```powershell
$taskName = "MultiHarnessBridge-Wake-<project>"
$taskFile = "$env:LOCALAPPDATA\MultiHarnessBridge\Tasks\$taskName.xml"
Register-ScheduledTask -TaskName $taskName -Xml (Get-Content -Raw $taskFile) -Force
```

Enable and start it:

```powershell
Enable-ScheduledTask -TaskName "MultiHarnessBridge-Wake-<project>"
Start-ScheduledTask -TaskName "MultiHarnessBridge-Wake-<project>"
```

Disable and stop it:

```powershell
Disable-ScheduledTask -TaskName "MultiHarnessBridge-Wake-<project>"
Stop-ScheduledTask -TaskName "MultiHarnessBridge-Wake-<project>"
```

Reload by stopping it, replacing the task definition, and starting it again:

```powershell
$taskName = "MultiHarnessBridge-Wake-<project>"
$taskFile = "$env:LOCALAPPDATA\MultiHarnessBridge\Tasks\$taskName.xml"
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Xml (Get-Content -Raw $taskFile) -Force
Start-ScheduledTask -TaskName $taskName
```

The monitor polls the registered project's current bound native roles. Claude
Code uses its in-session mailbox monitor by default, so the service does not
start a detached Claude turn. To check state, inspect the OS service manager
and use `multi-harness-wake --project <project> --once` from a registered
workspace. Queue enable/stop controls remain separate from the OS service
process.

## Shared watcher (coordinator pull mode)

`multi-harness-service --watch` writes `org.multi-harness-bridge.watch.<project>.plist` on macOS. It runs `multi-harness-watch --project <project>` at login, restarts it after a crash, and logs to `<MHB_HOME>/watch/<project>.log`. A second start exits 0 under the watcher's lock. Keep the wake service only while some harness in the project stays on `push`. See [Coordinator pickup](pickup.md).
