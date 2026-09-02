# Developing Tofu

This repository is developed by language models. Optimize every change for
unambiguous ownership, bounded discovery, machine-checkable contracts, and
small verification loops.

## Start here

1. Read [AGENTS.md](AGENTS.md).
2. Use [docs/README.md](docs/README.md) to find the domain owner and contract.
3. Inspect one bounded batch with `rg`/`rg --files`; do not dump the repository.
4. Run the smallest test that describes the behavior before editing.

Contracts and domain maps are authoritative. README files explain entry points;
they never redefine protocol fields or implementation rules.

## Architecture rules

- One concept has one owner. Do not add fallback state machines, transports,
  repositories, configuration merge policies, or error taxonomies.
- Authentication and authorization are decided once at middleware boundaries,
  default deny. User identity is an explicit service/repository parameter.
- Routes parse HTTP and call application services. They contain no SQL and no
  durable business transaction logic.
- Application services depend on repository protocols and semantic storage
  operations. Only the storage sidecar contains backend-specific SQL.
- Request handlers are stateless. Session state belongs to a declared store
  with a lifecycle and disposal path.
- Frontend domain behavior is TypeScript under `frontend/src/`. Retained
  runtime sections only inject ambient UI dependencies and must shrink.
- Failure behavior is part of the contract: preserve typed errors, rollback,
  idempotency, cancellation, retries, and user-visible recovery.
- Compatibility code is not a default. When an old surface is retired, remove
  its route, client, fallback, tests, and documentation in the same change.

Every new or substantially changed module starts with a short responsibility
header naming its inputs, outputs, owner boundary, and dependencies.

## Generated files

Never hand-edit generated artifacts. Edit their source and run the owner:

| Artifact | Source | Command |
|---|---|---|
| `frontend/src/runtime/app-runtime.js` | `frontend/src/runtime/sections/` | `npm run generate:runtime` |
| `static/styles.css`, `static/settings.css` | `frontend/src/styles/` | `npm run generate:styles` |
| Conversation clients/validators | `contracts/conversation_sync_v3.yaml` | `npm run generate:conversation-sync` |
| Runtime action catalog | action source modules | `npm run generate:actions` |
| Tool inventory | tool registry | `python3 scripts/gen_tool_inventory.py` |

Checks use the corresponding `check:*` command and fail on stale output.

## Change workflow

1. State the invariant and the current owner.
2. Reproduce the failure at the smallest semantic boundary.
3. Fix the owner, not each symptom site.
4. Delete the superseded path and migrate callers in the same increment.
5. Update the machine-readable contract first, then regenerate consumers.
6. Update the authority document and delete any document made historical.
7. Verify with the test ladder below.

Do not encode incident IDs, dates, screenshots, or one-off production stories
as the architecture. Tests should name the durable outcome and failure
semantics. Git history is the incident archive.

## Test ladder

```bash
# smallest relevant tests
python3 -m pytest -q tests/test_<owner>.py

# current generated frontend graph
npm run check:frontend

# domain gates
make test-unit
make test-api
make test-frontend

# full release gate
make test-all
```

Use `make test-affected` for a bounded iteration hint; it does not replace the
domain or release gate. Do not rerun an unchanged suite. Test policy and marker
semantics live in [docs/TESTING_STRATEGY.md](docs/TESTING_STRATEGY.md).

## Documentation lifecycle

Every `docs/**/*.md` file must be present in `docs/catalog.json`. Run:

```bash
make docs-check
```

Documentation describes the running system. Completed plans, audit snapshots,
incident reports, migration diaries, and duplicated tutorials are deleted after
their still-valid invariants move into an authority document. Git history is
the archive.

## Before handoff

- Relevant focused tests pass.
- Generated artifacts and contracts are current.
- No second authority or hidden compatibility branch was introduced.
- Error, cancellation, idempotency, ownership, and rollback behavior remain
  explicit and tested.
- Documentation links pass and stale documents were deleted.
- The final report distinguishes verified results from any unrun broad gate.
