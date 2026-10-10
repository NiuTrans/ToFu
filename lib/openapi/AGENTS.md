# OpenAPI package guidance

## Scope and authority

`lib/openapi/` assembles the registered HTTP surface from production route
assembly and per-handler `@api_meta` declarations. Those declarations own
product domain metadata; `contracts/api_v4.yaml` owns the release envelope.
Native protocols supply their generated components and operations, including
Conversation Sync. `docs/contracts/API_CONTRACT.md` owns boundary behavior.
The generated `contracts/api_endpoint_index_v1.json` inventories HTTP and
WebSocket registration, including factory routes. Its `/v1` is the index
schema version, independent of the mounted API major.

## Boundaries

- Facade: `__init__.py`; metadata: `_meta.py`; path conversion: `_paths.py`;
  reusable components/media: `_schema.py`; response projection: `_wire.py`;
  assembly: `_spec.py`; documentation UI: `_docs.py`.
- File/text success handlers declare `raw_success_response`; runtime and spec
  share this flag. Mixed JSON/SSE handlers keep product projection.
- Metadata stays per handler (`fn._api_meta`), with no global registry.
- The owning blueprint declares generic release or native response handling;
  never infer native exemption from a path prefix.
- Undecorated routes stay visible. Missing metadata is explicit in the index;
  documentation must not invent authentication guarantees for it.
- Index assembly opens no storage, starts no workers and discovers no plugins.
  Optional plugin routes remain owned by their packages.

## Verification

`pytest -q tests/openapi tests/api/test_api_endpoint_index.py
 tests/api/test_api_contract_drift.py`, then `make contracts-check`.
