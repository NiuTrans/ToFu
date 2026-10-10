# Desktop wire policy

This package is the stdlib-only contract boundary shared by the server bridge
and frozen device agent. The schema in `contracts/desktop_bridge_v3.schema.json`
owns limits, version and replay lifetime. It imports neither composition and
contains no command queue, credentials, GUI or execution state.

Read `docs/modules/remote_execution.md`; verify with
`tests/desktop/test_desktop_bridge_v3.py`. Changes to wire policy update the
schema and both consumers together. Both PyInstaller specs include the schema.
