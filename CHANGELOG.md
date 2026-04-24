# Changelog

All notable changes to tofu-open are documented in this file.

## [0.9.3] - 2026-04-22

### Fixed
- **MCP launcher pre-flight check.** When an MCP server is configured with a
  `command` that is not on PATH (e.g. `uvx` without uv installed, `npx` without
  Node), we now emit a clear, actionable install hint instead of a cryptic
  `FileNotFoundError`. Covers uvx / npx / pipx / node / python3.

### Improved
- **Overleaf MCP auto-install resilience.** The catalog entry and migration
  rules now pin `overleaf-mcp-plus[compile]>=0.1.3`, the slimmer release that
  drops the unused playwright dependency (~100 MB faster first-run install).
- **Auto-migration upgraded.** Stale server entries from prior versions are
  rewritten on load even when only the args list differs — user-supplied env
  vars and credentials are always preserved.

## [0.9.2] - 2026-04-20

### Fixed
- Fixed Overleaf MCP server failing to launch with `FileNotFoundError: 'overleaf-mcp'`
  on machines where the package was not pre-installed. The curated registry entry
  now uses `uvx --from overleaf-mcp-plus[compile]` so the server is auto-fetched
  from PyPI on first launch, matching the behavior of the other MCP cards.

## [0.9.1] - 2026-04-20

### Improved
- Further optimized support for Claude Opus 4.7.

### Added
- Added support for the Overleaf MCP server in the curated registry
  (edit/read/compile/history on Overleaf LaTeX projects).

### Fixed
- Fixed incorrect retry behavior of the model when invoked by tools.

## [0.9.0]

- Previous release.
