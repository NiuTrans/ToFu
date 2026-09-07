# Shared editor configuration guidance

## Scope

This directory contains intentionally committed VS Code/code-server workspace
defaults. Read `README.md`; personal launch, task, and workspace files remain
untracked.

## Editing rules

- Keep settings editor-only. Product runtime, test, formatter, and build policy
  belongs in repository-owned code/configuration, not a VS Code preference.
- Preserve aggressive watcher and search exclusions for runtime data, logs,
  uploads, FUSE mounts, dependencies, build output, evaluation workdirs, and
  other high-churn trees. New large ignored roots should be considered here.
- Do not exclude authored source, tests, contracts, or generated-source owners
  merely to hide performance problems or diagnostics.
- Recommendations are minimal, public, cross-platform, and useful in desktop
  VS Code, code-server, and Codespaces. Do not require a personal/proprietary
  extension for normal development.
- Never commit machine paths, credentials, remote host identifiers, personal
  launch arguments, or user-specific UI preferences.
- Keep settings valid for VS Code's JSON-with-comments parser and keys explicit;
  use comments only where the owning format supports them.

## Verification

Validate changed configuration with a JSONC-aware editor/parser and compare
watcher/search exclusions with `.gitignore`. Open the workspace in the affected
editor when changing an extension identifier or exclusion glob.
