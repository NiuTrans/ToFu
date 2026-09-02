# Long-agent release matrix

This package compiles the formal Tofu-versus-Codex `tofu-benchmark/v2`
manifest. It performs no downloads and no model calls. A successful preflight
requires all of the following immutable inputs:

- the verified 500-task SWE-bench definition cache produced by
  `python -m evaluations.swebench prepare-rootless`, whose complete name-to-
  digest catalog must equal the repository lock for dataset revision
  `sha256:b934b0cc...`;
- the repository's pinned 89-task Terminal-Bench 2.1 digest lock, expanded by
  the compiler to exactly five trials per task;
- five external, mode-0700 frozen task-pack directories for integrated tools,
  continuity, source-pack research, long writing, and fault recovery. These
  contain 200/200/200/200/100 real task payloads with hidden oracles.

The expected custom layout is:

```text
CUSTOM_ROOT/
  frozen-integrated-tools/pack.json
  frozen-continuity/pack.json
  frozen-source-packs/pack.json
  frozen-writing/pack.json
  frozen-fault-recovery/pack.json
```

Every `pack.json` is `tofu-frozen-task-pack/v1`, lists sorted task IDs,
relative paths and SHA-256 values, fixes `worldVersion` and the shared frozen
backend digest, and declares coverage tags. Every task payload is
`tofu-frozen-task/v1` and includes its instructions, hidden exact oracle,
deterministic simulator definition, permissions, family-specific shape, and
tags. Paths may not escape the pack or traverse a symlink. Payloads and hidden
oracles are not copied into the public benchmark manifest.

Preflight is read-only and fails if even one task is missing or changed:

```bash
python -m evaluations.long_agent_release preflight \
  --release-id kimi-codex-release-2026-08 \
  --swebench-definitions-root "$TOFU_SWEBENCH_DEFS" \
  --custom-packs-root "$TOFU_LONG_AGENT_PACKS"
```

Formal manifest creation additionally consumes an explicit JSON config; it
does not read Tofu or Codex user settings. The config freezes the harness and
agent hashes, provider face plus non-secret slot ID, thinking, tool permissions, prompt/schema hashes,
sandbox, timeout, infrastructure retry policy, experiment arm, pair/role, and
the maximum bytes per artifact, task, and run. Existing output is never
overwritten unless its canonical bytes are identical:

```bash
python -m evaluations.long_agent_release manifest \
  --config /private/release-config.json \
  --swebench-definitions-root "$TOFU_SWEBENCH_DEFS" \
  --custom-packs-root "$TOFU_LONG_AGENT_PACKS" \
  --output /private/artifacts/manifest.json
```

Create one mode-0700 evidence store for each manifest. Runners first store the
pre-dispatch claim, then store the raw JSONL trajectory (and, for Codex, proxy
metrics), put the content-addressed descriptors into `task.artifacts`, and
commit the complete v2 task record. `record-task` binds and closes the active
claim as oracle-ready:

```bash
python -m evaluations.long_agent_release run-init \
  --manifest /private/candidate-manifest.json \
  --run-root /private/candidate-run
python -m evaluations.long_agent_release attempt-start \
  --run-root /private/candidate-run --task-id TASK_ID \
  --execution-id RUNNER_EXECUTION_ID --runner-kind tofu-runner
python -m evaluations.long_agent_release store-artifact \
  --run-root /private/candidate-run --task-id TASK_ID \
  --kind raw_trajectory --source /private/raw.jsonl
python -m evaluations.long_agent_release record-task \
  --run-root /private/candidate-run --record /private/task-record.json
python -m evaluations.long_agent_release run-finalize \
  --run-root /private/candidate-run
```

The store rejects cross-arm records, unresolved oracles/infrastructure errors,
unpriced usage, mutable artifacts, malformed raw JSONL, excess retries, and all
three byte-limit overruns. It recalculates model/compaction/paid-tool cost from
the frozen price card. Codex records additionally require one proxy-metrics
artifact whose call count and CPU fields match the task; candidate latency may
not subtract Codex proxy overhead. An infrastructure failure is closed with
`attempt-fail`; post-dispatch failures require `taskStartedAtUnixMs`, usage,
paid-tool cost, and retained trajectory descriptors. Candidate failures also
require exactly one `failed_attempt_runtime_evidence` artifact, and its main
plus compaction usage must equal `modelUsages`; timeout/cancellation paths in
the formal Tofu adapter persist this sanitized snapshot before propagating the
failure. `attempt-retries` returns
the exact rows the eventual task record must include. A provably pre-dispatch
batch can use `attempt-fail-execution`. Final JSONL includes every attempt event
in manifest order.
Pair readiness is a separate fail-closed check:

```bash
python -m evaluations.long_agent_release pair-status \
  --baseline-root /private/codex-run \
  --candidate-root /private/candidate-run --require-complete
```

An audited formal SWE/TB Codex slice can be projected directly; the exporter
binds the Codex/Harbor/QEMU hashes, provider slot, timeout, task refs and
verifier lifecycle before writing any task record. Its Harbor launch must have
used `--release-run-root /private/codex-run` before paid dispatch:

```bash
python -m evaluations.long_agent_release export-codex-harbor \
  --harbor-run-dir /private/harbor-slice \
  --run-root /private/codex-run
```

The production-Tofu SWE/TB path is symmetric. Its formal Harbor launch uses
`--agent tofu-kimi`, a frozen non-secret runtime JSON, the exact candidate arm,
and `--release-run-root /private/candidate-run`. The host-only AgentRuntime
retains native events, sanitized runtime evidence, raw/visible tool audit, and
ATIF. Export replays all four views, charges model retries, compactions and
paid tools, and leaves candidate corrected wall equal to raw wall:

```bash
python -m evaluations.long_agent_release export-tofu-harbor \
  --harbor-run-dir /private/harbor-candidate-slice \
  --run-root /private/candidate-run
```

Prompt-contract, tool-schema, runtime-config, provider/slot, thinking, arm,
agent and source-revision drift all fail before a task record is committed.

After both stores are complete and finalized, generate the immutable paired
report. A pilot report is always diagnostic; only the exact 1,845-task table
can reach the conjunctive release decision:

```bash
python -m evaluations.long_agent_release pair-report \
  --baseline-root /private/codex-run \
  --candidate-root /private/candidate-run \
  --output /private/reports/pair-report.json
```

The compiler and recorder prove identity, attempt retention, and completeness
only. Full release stores cannot finalize without one pre-dispatch claim and
oracle-ready terminal per task; the paired report derives infrastructure rate
from those starts/failures, never only from surviving task rows. The paired
SWE/TB launch/export paths do not supply the still-missing 900 custom tasks or
their simulator launch adapters, do not claim that a paid trial ran, and do not
infer that Tofu leads Codex. That claim
still requires both complete stores and a report whose full-matrix quality,
family, judge, safety, cost, P90, infrastructure, and actual-orchestration gates
all pass.
