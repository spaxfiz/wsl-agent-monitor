# WSL Agent Monitor

Did my agents dead?

Probably not. But when `claude` and `codex` go suspiciously quiet inside WSL,
this floating Windows monitor gives you a live, low-drama answer by reading the
session files they are already writing.

`WSL Agent Monitor` is a small desktop utility for Windows that:

- watches already-running `Claude` and `Codex` sessions inside WSL
- streams recent activity into a floating Tkinter window
- supports multiple live sessions per agent
- keeps session metadata compact so the output area stays readable
- hides to the tray and can dock to the screen edge
- does not write extra log files or launch new agent processes

It is intentionally simple: a live process-viewer for agent sessions, not a
workflow engine, terminal multiplexer, or accidental observability platform.

## Why This Exists

If you already have long-running agent sessions in WSL, the usual debugging
questions are annoyingly practical:

- Is the session still alive?
- Which session is producing output right now?
- Is Codex idle, stale, or just thinking very hard?
- Did Claude disappear, or did it simply move to another file?

This app tries to answer those questions with one always-available floating
window and without adding new moving parts.

## Features

- Live monitoring for `Claude` and `Codex`
- Multi-session tracking per agent
- Session list with `session id + name`
- Output lines tagged with short session id
- Single-button `Watch` / `Pause` per agent
- Single-button `Watch all` / `Pause all`
- Tray integration for quick reopen
- Right-edge dock with hover-to-expand behavior
- Rolling in-memory output buffer for responsiveness

## How It Works

The monitor runs on Windows but probes session state inside WSL using
`wsl.exe`.

- `Claude` sessions are discovered from `~/.claude/sessions/*.json` plus their
  matching project `jsonl` logs
- `Codex` sessions are discovered from
  `~/.codex/sessions/**/rollout-*.jsonl`
- Codex sessions remain in current activity while their rollout files receive
  events within a 10-minute activity window
- New sessions are detected dynamically, and inactive sessions can drop out of
  current activity without restarting the app

The UI only displays recent events. It does not create its own persistent log.

## Project Layout

```text
app.py                 Thin entrypoint
wsl_agent_monitor/     UI, probe scripts, constants, models
tests/                 Lightweight unit tests
launch_monitor.bat     Run helper for local use
build_exe.bat          One-file Windows build script
```

## Requirements

- Windows with Python available
- WSL installed and working
- `claude` and/or `codex` sessions already running inside WSL
- For tray support and packaging: dependencies from `requirements-build.txt`

## Local Setup

Create the local virtual environment in the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
```

## Run

Recommended:

```powershell
launch_monitor.bat
```

Directly with Python:

```powershell
python app.py
```

Or from the local virtual environment:

```powershell
.\.venv\Scripts\python.exe app.py
```

## Usage

1. Optionally enter a WSL distro name. Leave it empty to use the default.
2. Use the top toggle button to start or pause both agent watchers.
3. Use each agent card's toggle button to control `Claude` and `Codex`
   independently.
4. Check `Current activity` for the newest summarized event.
5. Expand `Sessions` when you need full `session id + name` details.
6. Expand `Watched files` only when you want the underlying file list.
7. Minimize to tray with `_`, then reopen from the notification area.
8. Enable `Edge dock` if you want the window to hug the right edge and slide
   out on hover.

## Tests

Run the lightweight test suite with:

```powershell
python -m unittest discover -s tests -v
```

The tests cover the thin entrypoint, key UI toggle behavior, and probe error
handling.

## Build

Package the app into a single Windows binary:

```powershell
build_exe.bat
```

Expected output:

```text
dist\WSLAgentMonitor.exe
```

## Limitations

- The monitor depends on the on-disk session formats currently used by Claude
  and Codex inside WSL
- If those tools change their local storage layout, the probe logic will need
  an update
- Codex monitoring intentionally uses rollout activity instead of assuming a
  separate state store is present
- Tray support depends on optional local dependencies such as `pystray` and
  `Pillow`

## License

MIT. See `LICENSE`.
