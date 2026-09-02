# Context-efficiency benchmark

This directory contains the reproducible Tofu versus Codex experiment described
in `docs/LLM_COST_OPTIMIZATION.md`. It uses the official frozen
SWE-bench Multilingual images and grader, append-only JSONL evidence, fixed
infrastructure-only retries, and a model-specific price card frozen into each
run manifest.

For subscription providers, `actualCostUsd` is zero incremental API spend and
`publicApiShadowCostUsd` is the comparable public-API conversion. For ordinary
API providers, `actualCostUsd` retains the provider estimate while the shadow
field reprices raw usage with the frozen canonical card. This keeps paired
arms comparable without pretending subscription quota is per-task API spend.

## Reproduce

```bash
python -m benchmarks.context_efficiency.cli manifest \
  --output benchmarks/context_efficiency_manifest.json

python -m benchmarks.context_efficiency.cli preflight \
  --manifest benchmarks/context_efficiency_manifest.json \
  --stage calibration --gold-oracle

python -m benchmarks.context_efficiency.cli run \
  --manifest benchmarks/context_efficiency_manifest.json \
  --stage calibration --arms tofu-control,codex-mechanism --workers 1

python -m benchmarks.context_efficiency.cli run \
  --manifest benchmarks/context_efficiency_manifest.json \
  --stage ablation \
  --arms tofu-control,tofu-explicit,tofu-routed,tofu-evidence,tofu-ptc,tofu-ptc-additive,tofu-ptc-serial-gateway,tofu-prompt-lean,tofu-effort-medium,tofu-effort-low,tofu-multi-agent,tofu-ws64,tofu-ws96,tofu-ws128

python -m benchmarks.context_efficiency.analyze \
  --results-dir benchmarks/context_efficiency_results --stage ablation \
  --candidate-out benchmarks/context_efficiency_candidate.json \
  --report-out benchmarks/context_efficiency_results/ablation/report.json

python -m benchmarks.context_efficiency.cli run \
  --manifest benchmarks/context_efficiency_manifest.json --stage pilot \
  --arms tofu-control,tofu-candidate,codex-mechanism \
  --candidate-config benchmarks/context_efficiency_candidate.json
```

Every run is resumable. A source fingerprint is part of the run ID, so changing
the runner, mechanism implementation, or candidate config creates a new JSONL
run instead of silently appending incompatible evidence.

The 100-task confirmation is started only after the pilot freezes one candidate:

```bash
python -m benchmarks.context_efficiency.cli run \
  --manifest benchmarks/context_efficiency_manifest.json --stage confirmation \
  --arms tofu-candidate,codex-mechanism \
  --candidate-config benchmarks/context_efficiency_candidate.json
```

The PTC arm is not eligible merely because quality/cost/latency pass. At least
one `source=openai_ptc` hosted program must be observed (local ToolScript runs
do not count), every observed PTC run must complete, and the recorded runs must
contain zero rejected calls, output truncations, or budget violations. Before
a paid benchmark, validate the provider's live protocol with:

```bash
python scripts/ptc_live_smoke.py --dry-run
OPENAI_API_KEY=... python scripts/ptc_live_smoke.py --model gpt-5.6

# Baseline / Pro mode / Multi-agent request and live protocol checks
python scripts/gpt56_live_smoke.py --scenario all --dry-run
OPENAI_API_KEY=... python scripts/gpt56_live_smoke.py --scenario all
```

The smoke uses only deterministic in-memory reads. It verifies stateless replay,
`caller` preservation, program output, the final message, and the same call /
continuation ceilings enforced by the application.

## Live Kimi calibration (2026-09-01)

The live run pinned the provider, model, source tree and permitted endpoint:

```bash
export CONTEXT_BENCH_MODEL_ID=kimi-k3
export CONTEXT_BENCH_PROVIDER_ID=example-corp
export CONTEXT_BENCH_ALLOW_HOSTS=your-llm-gateway.example.com
export CONTEXT_BENCH_TOFU_SOURCE="$PWD/output/serial_gateway_eval/source_snapshot_20260901a"
```

On three valid SWE-bench Multilingual tasks, `tofu-control` (`xhigh`, Kimi
`max`) and `tofu-effort-medium` (`medium`, Kimi `high`) both resolved 3/3. The
frozen-price total fell from $1.2394 to $0.8803 (-29.0%) and model rounds from
69 to 62 (-10.1%). Carbon and nlohmann/json improved; bat was the retained
counterexample (+0.5% cost and +34.4% model-phase latency). Aggregate latency
was therefore +4.9%, so the result supports the existing medium product
default but does not establish a latency win or authorize overriding an
explicit user-selected `xhigh`/`max`.

Lucene 12022 is excluded from quality and aggregate comparisons: both patches
applied, but the grader attempted to download `gradle-wrapper.jar` from the
network-isolated container and failed with `UnknownHostException` before any
test was graded. The evaluator now recognizes only that strong bootstrap
signature as infrastructure; ordinary test network errors remain model/test
outcomes. Append-only historical records are intentionally not rewritten.

The serial-gateway arm is also retained as opt-in evidence rather than shipped:
its real Carbon trial passed in both arms but increased normalized cost 40.6%
and model-phase latency 41.8%. Promotion still requires the normal pilot and
confirmation gates.
