# Desktop shell and packaging guidance

## Scope

`desktop/` owns local launchers, the thin Tk host, installer templates, and
platform packaging. Remote device protocol code lives under `lib/desktop*` and
is governed by `docs/modules/remote_execution.md`.

## Editing rules

- Keep the shell thin: application behavior stays in the web/runtime owners.
  Do not fork server lifecycle or settings logic into the desktop UI.
- Preserve platform-specific install, update, launch, and uninstall semantics.
  Paths and subprocess arguments must be explicit, quoted, and validated.
- Never embed credentials, signing material, machine-specific paths, or private
  endpoints in source or installers.
- Match installer artifact names, kinds, versions, checksums, and workflow
  publication rules. Partial installs and failed launches must be visible and
  recoverable.
- Keep optional UI/toolkit dependencies lazy so headless/server imports remain
  unaffected.

## Verification

Run the focused `test_desktop_*`, `test_installer_*`, and release-asset tests.
For packaging changes, build the affected platform artifact in its supported
environment and smoke-test install, launch, upgrade, and cleanup.
