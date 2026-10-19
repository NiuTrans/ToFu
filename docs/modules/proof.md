# Module map — proof

## Responsibility and entry points

`lib/proof/` owns proof-carrying deliverables: read-only assembly of a
content-addressed evidence bundle for one owned task, and verification of
submitted bundles. It produces no new facts and owns no write path; every
adapter reads an existing authority — the terminal task snapshot
(`lib/tasks_pkg/durable_chat_replay.py`), the per-attempt Turn Trace
(`lib/tasks_pkg/turn_trace.py`), Request Inspector request rounds
(`lib/tasks_pkg/request_inspector.py`) — or settlement file-receipt
metadata. A bundle proves an execution trail exists, is consistent, and is
untampered; deliverable correctness stays with its own reviewers and tests.

Wire authority: [`contracts/deliverable_proof_v1.schema.json`](../../contracts/deliverable_proof_v1.schema.json).
Design record: the proof-deliverables design proposal (retired; recoverable from git history).

Entry points: `assemble_task_proof`, `verify_proof_bundle`,
`lib.proof.evidence.server_evidence_resolver`. HTTP:
`GET /api/v1/tasks/{id}/proof` and `POST /api/v1/proof/verify` in
`routes/api_v1/proof.py` (scope `tasks`; the v4 projection is automatic).

## Boundaries

| Owner | Owns | Must not own |
|---|---|---|
| `contract.py` | wire-authority load, limits, fail-closed validation | assembly or verdict policy |
| `digests.py` | one canonical form (orjson sorted keys) + `sha256:` addressing | evidence semantics |
| `evidence.py` | one read-only adapter per authority; content-free round projection; the server resolver | new facts, write paths, second authorities |
| `assemble.py` | subject/verdict fold, coverage degradation, bundle sealing | storage dialect, HTTP |
| `verify.py` | pure verdict core (`tampered` > `unknown_evidence` > `partial` > `valid`) | evidence reads (injected as `resolver`) |
| `routes/api_v1/proof.py` | auth boundary, body cap, typed envelope | domain policy |

- Owner identity is explicit everywhere; foreign and missing tasks stay
  indistinguishable (`None` → 404), and the server evidence re-check only
  runs for the bundle's own owner (foreign bundles verify structurally).
- Missing evidence degrades `coverage` to `partial` with closed reasons —
  never fabricated, never zero-filled (Turn Trace honest-empty precedent).
- Bundles are content-free by default (`contentIncluded: false`): payload
  lanes fold into digests and counts.
- `lib.tasks_pkg` readers are imported lazily inside functions; tests patch
  the source modules directly.

## Tests

- `tests/proof/test_deliverable_proof_contract.py`: golden instances, typed
  negatives, declared resource limits.
- `tests/proof/test_proof_bundle.py`: assembly/verification semantics —
  determinism, tamper (stale address and re-sealed forgery), partial
  reasons, `unknown_evidence`, owner isolation, content freedom.
- `tests/api/test_api_v1_proof.py`: auth/scope boundary, explicit-owner fencing,
  export→verify round-trip, malformed bodies, the 1 MiB verify cap.
- Neighboring gates: `tests/api/test_api_endpoint_index.py`,
  `tests/api/test_api_v4_product.py`, `tests/api/test_api_contract_drift.py`.
