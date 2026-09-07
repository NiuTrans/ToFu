# Contract guidance

## Scope and authority

This directory contains machine-readable wire and schema authorities. Read
`docs/README.md` and the owning authority document before changing a contract.
Generated clients and projections are consumers, not alternative sources of
truth.

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
`pytest -q tests/test_identity_contract.py`, and `make docs-check`. Finish with
the neighboring API/frontend contract gate for changed public wire behavior.
