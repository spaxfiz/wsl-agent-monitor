# Changelog

All notable changes to this project will be documented in this file.

The format is inspired by Keep a Changelog, with a practical bias toward
release notes humans can still read while half-awake.

## [Unreleased]

- Ongoing improvements will land here before the next tagged release.

## [0.2.0] - 2026-03-16

### Added

- Host-side monitoring for local Windows `Claude` and `Codex` sessions
- Top-level `Host` / `WSL` source switching buttons in the floating monitor
- Compact usage statistics below each agent log view

### Changed

- Release packaging now publishes a `.zip` archive containing `WSLAgentMonitor.exe`
  to reduce download-time antivirus deletion risk

## [0.1.0] - 2026-03-15

### Added

- Initial public release of the floating Windows monitor
- Live WSL session monitoring for `Claude` and `Codex`
- Multi-session tracking with compact session metadata display
- Tray support and right-edge docking UI
- Single-file Windows packaging via `PyInstaller`
- Basic automated tests for UI toggles, entrypoint import, and probe failures

[0.1.0]: https://github.com/spaxfiz/wsl-agent-monitor/releases/tag/v0.1.0
[0.2.0]: https://github.com/spaxfiz/wsl-agent-monitor/releases/tag/v0.2.0
