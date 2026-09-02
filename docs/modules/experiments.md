# Module map — experiments

## Responsibility and entry points

`lib/experiments/` owns the versioned capability boundary for controlled
experiments. Its public entry points are `resolve_experiment_spec`,
`assign_experiment`, `apply_experiment`, `compile_experiment_application`,
`compile_metric_extractor`, and
`analyze_experiment`. It depends on standard-library primitives and the plugin
registry; it does not import routes, task managers, or a database backend.

The machine-readable durable definition is
[`contracts/experiments_v1.schema.json`](../../contracts/experiments_v1.schema.json).
`lib/cost_experiments.py` is the product adapter for the maintained context-cost
experiment. It owns compatibility fields and diagnostic rollups, but delegates
strategy resolution, assignment, metric extraction, and the final decision to
the generic capability layer.

```text
authored definition
  -> resolve installed providers + freeze versions/digests
  -> immutable spec + specDigest
  -> owner-scoped assignment
  -> guarded strategy application + exposure state
  -> owner-filtered outcome projection
  -> assignment-unit analyzer
  -> decision (promotion defaults to deny)
```

## Boundaries

| Owner | Owns | Must not own |
|---|---|---|
| `contracts.py` | structural validation, canonical JSON, immutable spec digest | product request fields or statistics |
| `registry.py` | atomic, version-aware plugin mount/unmount, entry-point discovery, callback-free catalog | experiment activation or fallback policy |
| `service.py` | owner-aware bucketing, provider identity checks, strategy application, compiled metric plans, analyzer dispatch | HTTP, storage, or product-specific metrics |
| a plugin | strategies, conflict policy, metric extractors, analyzer | user identity, persistence, or route behavior |
| `cost_experiments.py` | context-cost adapter, outcome diagnostics, legacy settings/report shape | provider discovery or generic assignment algorithms |
| storage semantic operation | owner and experiment filtering before `LIMIT`, compact outcome projection | statistical decisions |
| `routes/config.py` | HTTP parsing, off-loop storage call, response envelope | SQL, bucketing, or inference |

This split is deliberate: adding a strategy does not require changing the
assignment kernel, and changing storage does not require reimplementing the
analysis rules.

## Plugin contract

An install-time bundle is an `ExperimentPlugin` containing any combination of
typed `StrategyProvider`, `MetricProvider`, and `AnalyzerProvider` objects. The
Python packaging entry-point group is `tofu.experiments`; an entry may return
one bundle or a list of bundles.

Registration is all-or-nothing and returns an idempotent disposer. Multiple
versions of one plugin can coexist so historical resolved specs remain
executable; an authored reference must select `pluginVersion` whenever more
than one matching version is installed. Optional
entry-point discovery is fail-soft so an unrelated broken extension cannot
prevent startup. A provider referenced by an active specification is strict:
resolution fails if it is absent, and execution verifies both its declared
version and implementation digest. A request-time strategy exception preserves
the original request but records `application_failed`; it is not silently
counted as an exposure. Reports reject mixed fingerprints and unverified
exposures.

Prompt-profile arms also require model-visible adoption evidence on every task:
the requested, resolved, and effective profiles must match the fixed arm, the
status must be applied, and positive size/token plus SHA-256 evidence must be
present. `tofu-benchmark/v2` rejects lean, named-ablation, and combined task
records without this proof; a lower aggregate token count cannot select the arm.
The formal `tofu-kimi` candidate freezes the requested prompt contract and then
reconciles the resolved model-visible proof for every runtime context round;
manifest intent alone is not exposure evidence.

Orchestration arms use the same fail-closed rule with a distinct evidence
contract. A provider/native/local backend recorded at request assembly is only
`projectionEvidence`. `tofu-benchmark/v2` requires every `orchestration_v2` or
`combined_v2` task to retain an explainable shape, reasons, expected savings,
and a status consistent with actual runtime evidence. The full release gate
also requires at least one real program trajectory and one real agent
trajectory across the frozen 1,845 records; a merely exposed gateway cannot be
reported as an adopted or winning mechanism.

Product adapters must also isolate reusable work by effective request policy.
If a product cache key does not encode the resolved arm, an explicitly applied
arm bypasses both cache reads and writes; in-flight dedup includes model and the
canonical request-config fingerprint. A control/candidate pair must never join
one worker or overwrite a shared canonical artifact. Paper report/deepen use
this request-local rule, while ordinary requests retain their shared cache.

Both high-volume boundaries compile provider lookups once: the request hot path
uses a registry-generation-aware application plan, and each report scan uses a
metric extraction plan. A plugin mount/unmount increments the registry generation
and invalidates the request plan before its next use.

Provider metadata, never callbacks, is discoverable at
`GET /api/v1/experiments/capabilities`.

## Immutable experiment identity

One `experimentId` identifies exactly one resolved specification. The digest
covers assignment unit and algorithm, enrollment, arm allocation, resolved
strategy configs, plugin versions and implementation digests, metrics, analyzer,
and the fixed-horizon analysis plan, including the maximum assignment-unit
cohort. Operational lifecycle is not part of the spec: a new ID is `draft`,
enabling moves it to `running` and records a server-owned `started_at_ms`; disabling
a running ID irreversibly moves it to `sealed` with `sealed_at_ms`. A sealed ID
cannot be restarted. Any spec change also requires a new ID.

Assignment hashes `(owner_id, assignment_unit, unit_id)` into a retained
`subjectDigest`, then uses the experiment ID and named lanes for enrollment and
arm buckets. Raw owner identity is never persisted in the exposure. Owner is an
explicit argument, preserving the multi-user evolution seam.

## Decision contract

The built-in context-cost analyzer uses one value per conversation, not raw
turns. Cost is the sum for a fully priced assignment unit; semantic quality is
the unit-level oracle rate; latency is the unit-level mean feeding a P90
guardrail. The deterministic clustered bootstrap reports candidate-minus-control
intervals. For each conversation, its first terminal exposed task is the frozen
metric observation; later turns remain descriptive and cannot change inference.
The storage scan starts at the server-recorded experiment start, not
the UI's lookback selector. Only the precommitted first
`maximumAssignmentUnits`, ordered by verified exposure time and subject digest,
enter inference. Later observations
remain descriptive, so delaying shutdown cannot change the analyzed cohort.
Assignment-only task checkpoints act as the exposure denominator: if an earlier
cohort task has not produced a terminal outcome (or terminal outcome construction
failed), `pending_exposures` blocks the decision instead of silently selecting a
later survivor.
Promotion requires all of the following:

- the fixed cohort is full, enrollment is sealed, minimum assignment units
  exist in both arms, the server-owned start/seal times are known, and a usable
  sample-ratio-mismatch
  diagnostic;
- complete real-price coverage (missing price is never zero or complete-case
  evidence);
- the quality interval lower bound at or above the frozen non-inferiority
  margin;
- the cost interval upper bound below zero;
- P90 latency within the frozen regression limit;
- an untruncated source with no malformed rows, cross-arm units, mixed spec
  digests, unversioned outcomes, unverified exposures, or metric failures.

`comparison.pointEstimateOptimizedCheaper` is the fixed cohort's descriptive
sign only; `allObservedCostPerConversationDeltaPct` is a separate diagnostic for
later observations and never feeds a decision.
`promotionEligible` (and the compatibility field `optimizedIsCheaper`) is the
evidence-backed decision. Consumers must never promote from a point estimate.

## Extension procedure

1. Implement and test a plugin bundle. Callbacks are pure; strategy application
   returns a detached request object.
2. Publish it through `tofu.experiments`, keep old provider versions mounted
   while their specs must remain analyzable, and confirm all versions appear in
   the capability catalog.
3. Resolve an authored definition before activation and persist the complete
   resolved spec, not only provider names.
4. Choose an explicit assignment unit and pass owner identity at the adapter
   boundary.
5. Persist exposure status and spec digest with every outcome. Aggregate at the
   assignment unit, precommit the inference horizon, compile application and
   metric extraction plans at their respective boundaries, then dispatch the
   analyzer pinned in the spec.
6. Add failure-injection tests for missing provider, digest drift, callback
   failure, incomplete metrics, truncation, and rollback/disposal.

The current maintained settings panel activates one built-in context-cost
adapter. New domains add a thin activation/outcome adapter; they do not fork the
registry, assignment service, or decision vocabulary.

## Tests

- `tests/test_experiment_framework.py`: registry rollback/version coexistence,
  immutable specs, owner-scoped assignment, drift/failure behavior, schema, and
  compiled metric/analyzer dispatch.
- `tests/test_cost_experiments.py`: product adapter, persistence outcome, report,
  and HTTP capability/report surfaces.
- `tests/test_storage_sidecar_contract.py::test_task_results_cost_experiment_scan_projects_only_outcomes`:
  compact owner/experiment projection and post-filter cap semantics.
- `tests/test_context_efficiency_audit.py::test_benchmark_jsonl_budget_public_price_and_acceptance`:
  benchmark non-inferiority release gate.
