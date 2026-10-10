# Isolated JavaScript runtime

`execute_javascript_program` runs one async function body in a fresh WASM
instance. `bootstrap.js` is retained source; `assets/` holds the pinned upstream
reactor and notices. Shared admission/evidence policy lives in
`lib/tools/program_contract.py`. The gateway still owns task identity, catalog,
schema validation, permissions, approvals and child settlement.

Keep bridge helpers and receipt identities private to the host. Copies of
receipts cannot acquire original Python identity or canonical provenance.
Host callbacks record terminal signals on their own instance; never raise
through Wasmtime's process-global callback exception slot. The lazy shared execution clock joins its ticker on last Store release;
Store deadlines exclude host bridge waits and remain independent. Every path closes
its Store; no pending child work, continuation or instance survives a run.

Read `docs/contracts/TOOL_SEARCH_EXECUTION_GATEWAY.md` and
`docs/RESOURCE_CONTROL.md`. Verify `tests/tools/test_javascript_program.py`,
`test_javascript_resources.py`, then shared gateway/evidence tests. Packaging
must retain bootstrap, verified WASM checksum and all upstream notices.
