# Contract guidance

## Scope and authority

This directory contains machine-readable wire and schema authorities. Read
`docs/README.md` and the owning authority document before changing a contract.
Generated clients and projections are consumers, not alternative sources of
truth.

Authority by API surface:

- **v4 / conversation-sync / events / domain schemas**: the YAML/JSON files
  here are canonical; generated clients and fixtures are outputs.
- **product domain metadata**: handler `@api_meta` declarations remain the
  authority for projected product routes (`lib/openapi/AGENTS.md`). Their
  source directory name does not select the release major.
  `api_endpoint_index_v1.json` derives the complete first-party HTTP/WebSocket
  mount from production registration; its version identifies the index schema.
- **environment knobs**: `config_knobs_v1.json` is the complete inventory of
  env configuration reads. Adding or renaming an env read requires updating
  it via `python3 scripts/check_config_knobs.py --rescan` in the same change;
  new knobs must enter as `documented` (owner + description), never `legacy`.

## Editing rules

- Change the canonical schema first, then run its committed generator. Never
  patch a generated consumer to conceal contract drift.
- Preserve compatibility deliberately. A breaking removal or rename requires
  explicit migration, versioning, and failure behavior rather than permissive
  parsing scattered across consumers.
- Keep identity, ownership, bounds, nullability, defaults, and error shapes
  explicit and machine-checkable.
- Update every server, frontend, SDK, documentation, and fixture consumer in
  the same change. Delete superseded generated output.
- Add the contract and its guard to `docs/catalog.json` when introducing a new
  JSON/YAML authority.

## Verification

Run the focused generator in check mode and its cataloged guard test. Common
checks are `npm run check:conversation-sync`, `npm run check:api-v4`,
and `pytest -q tests/identity/test_identity_contract.py`. Finish with
the neighboring API/frontend contract gate for changed public wire behavior.

`native_aggregate_cost_v1` describes original Goal credits without settlement
authority. Generate standalone schemas via gen_native_checkpoint_control; verify
aggregate fee tests plus native Goal claim/consumption and passive replay.
