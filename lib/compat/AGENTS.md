# Compatibility adapter guidance

## Owners

- `openai.py` and `anthropic.py` translate requests and project task responses.
  Their HTTP owners are `routes/compat_*.py`; neither translator imports routes.
- `openai_projection.py` owns pure OpenAI finish values, direct-response usage
  and chunk structure for native, compatible and direct model responses.
  Callers validate terminal evidence and select content, clocks and accounting.
- `_common.py` owns shared request configuration and the terminal-verdict port.
- `_platform.py` owns OS compatibility; `__init__.py` retains its public exports.

## Boundaries and verification

Read `docs/contracts/HEADLESS_API.md` and `docs/modules/integrations_api.md`.
Keep narrator filtering, native event delivery, client tool handoff, auth,
settlement and cancellation in their existing owners. Wire helpers cannot
invent successful terminal evidence or start an execution/stream state machine.

Run `tests/compat/`, `tests/api/test_api_v1_chat_route.py`,
`tests/chat/test_chat_stream_direct.py` and `tests/api/test_compat_client_tools.py`.
The latter exercises SQLite, native daemon and native embedded storage; use a
certified engine/library pair through `TOFU_TEST_TOFUDB_BIN` for complete coverage.
