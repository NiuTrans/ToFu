# LLM benchmark and managed-local profiles

This guide owns specialized runtime profiles referenced by the
[LLM I/O domain map](modules/llm_io.md). They reuse the canonical dispatch and
settlement owners rather than creating alternate provider semantics.

## Sub-agent model inheritance and AA evidence

`lib/model_profiles` preserves a non-empty parent model for all delegation
roles and tiers. Delegation does not authorize a cheaper model substitution;
normal dispatch still enforces the owner, provider pin, and route eligibility.
Explicit per-agent overrides remain caller-owned.

Without a parent, configured profiles retain tier/role gates and rank fresh,
exact canonical-model AA evidence before price: Coding then Intelligence for
coding/review roles, Agentic then Intelligence for browser work, Intelligence
otherwise. Missing evidence is unknown, not zero. Original indices are compared
lexicographically; they are not averaged or mapped to invented tier thresholds.
Within unmeasured candidates, declared quality precedes price. These declared
tiers are provisional, not benchmark measurements.

The AA projection reads the existing public dataset cache without credentials,
network calls, or refresh workers. Expired evidence and base/variant prefix
matches cannot influence selection. Price is a tie-breaker, not the capability
objective. Capacity, retry, and authorization remain with canonical dispatch;
this policy does not add an independent health or cooldown system.

## Paired Kimi benchmark paths

`evaluations/codex_kimi_proxy/` is a benchmark-only, one-request/one-Kimi-call
Responses adapter, not a provider. It pins the Codex binary, keeps compaction
client-local, normalizes tools, rejects unknown native types, and records raw
wall, total proxy CPU, and pure translation CPU separately; compact requests
invalidate a trial. The formal launcher strips Kimi secrets from Harbor,
restricts the guest to a same-UID private relay, re-verifies the binary, binds
provider/binary identity, and re-projects raw JSONL/metrics instead of trusting
non-empty output. Immutable task claims and identical release locks govern
resume/export; outer failures retain usage, artifacts, wall time, and terminals.

The paired `tofu-kimi` profile instead uses public production `AgentRuntime`.
Secrets stay host-side; the guest exposes only run/submit tools while Tofu owns
dispatch, context, compaction, settlement, and orchestration. Native events,
sanitized evidence, raw/visible tool audit, and ATIF-v1.7 must reconcile without
prompt/runtime/schema drift, call/usage mismatch, fallback, secret persistence,
missing compaction evidence, or an unverified final claim. Candidate wall time
is never proxy-adjusted, and failed outer attempts remain in the same ledger.

## Managed local deployment

The classic local-model flow is operator-driven: the user starts vLLM, SGLang,
Ollama, or llama.cpp and configures its v2 Connection in Settings, or lets
`lib/llm_dispatch/autodiscover_local.py` sweep well-known loopback ports.
`lib/local_serve/` adds the managed path: the user supplies a HuggingFace model
directory or `.gguf` file and the chat agent drives this fixed pipeline through
the `local_serve_*` tool family:

1. `_probe.inspect_model_path` / `probe_hardware` reads model and resource facts.
2. `_plan.plan_launch` is the sole engine/flag authority and owns the bounded OOM
   degradation ladder; the agent narrates this plan and never invents flags.
3. `_env.ensure_engine` creates isolated per-engine uv environments under
   `data/local_serve/` on demand. Disk estimates are diagnostic; actual installation
   and filesystem errors are reported without a directory quota.
4. `_process` owns spawn, bounded rotating logs, readiness, OOM retry, stop, and
   restart.
5. `_store` owns the durable instance ledger at
   `data/config/local_serve.json`; environments and logs are reconstructible.
6. `_register` publishes the endpoint through the shared owner-scoped v2 local
   provider service. Discovered wire IDs are `pending_identity` Offerings until
   independently matched to an official model identity; dispatcher routing,
   health checks, and Settings use that same authority from then on.

The plan table follows current engine contracts: vLLM 0.28 is V1-only and does
not generate removed in-tree bitsandbytes flags; SGLang 0.5 owns its memory and
chunked-prefill tuning and does not generate removed/unmaintained flags;
Ollama's KV lever is `OLLAMA_KV_CACHE_TYPE=q8_0`; llama.cpp uses `--ui-config`.

Managed servers select available loopback ports and avoid conventional engine
endpoints. Existing listeners and active/planned ledger endpoints are excluded.
Installing packages and starting servers cross the approval boundary in
`lib/tasks_pkg/tool_dispatch/_approval.py`; every subprocess wait is bounded,
and OOM recovery may reduce memory pressure but never a safety property.
Managed host processes are deliberately personal-mode only. Distributed mode
fails closed until the host has an owner-aware resource scheduler and ledger;
this does not weaken the owner boundary of the v2 routing records.

Two sibling modules serve the choose-and-repair experience around the pipeline:
`_catalog` is the curated open-model shortlist (1.7B–32B, purpose tags, HF BF16
+ GGUF variants) whose `evaluate_hardware` ranks feasible models against one
hardware snapshot and whose `cloud_alternative` recommends DeepSeek V4 Flash
(pulling live pricing from `lib/pricing`) when the machine can't comfortably
run a capable model; `_diagnose` classifies a failed deployment's
`last_error` + bounded log tail into known failure kinds (OOM, CUDA driver,
architecture, disk, port, network, timeout, interruption) with concrete next
steps and a retryable verdict, embedded in `deployment_status` so the chat
tool and the Settings page relay identical guidance. The read-only surface
for all three facets is `routes/api_v1/local_serve.py` (hardware /
recommendations / deployments); every write stays with the approval-gated
tool family.

The Settings 本地部署 page renders that surface directly: a live hardware
strip, purpose-filtered recommendation cards whose deploy button hands a
prefilled `local_serve_*` instruction to a fresh chat, the cloud escape hatch
deep-linking the DeepSeek provider template wizard, and the managed-instance
ledger where failed rows hand off to the agent's diagnosis-repair flow. The
“模型路径托管” tile still collects path and engine preference for the same
chat handoff; nothing writes routing authority client-side. llama.cpp remains
a first-class endpoint preset on port 8080, pinned against backend
`WELL_KNOWN_ENGINES` by parity tests.

Local engine installation — Problem: a 20 GiB directory quota, estimated-size
plus 20% free-space preflight and 4 GiB download ceiling rejected usable installs.
Decision: install on demand, stream downloads in 1 MiB chunks, and report actual
network, filesystem and installer errors. Disk observations remain diagnostic;
failed probes do not deny installation and no recursive directory scan occurs.
The retired `TOFU_LOCAL_SERVE_BUDGET_GB` value is ignored. Alternatives: larger
quotas retain artificial refusal; reading the entire archive increases memory.
Tests transfer 4097 MiB through a counting sink with one reused 1 MiB chunk and
verify real read/write failures plus invalid-engine rejection.

GGUF inspection — Problem: fixed metadata counts, key/string lengths and array
lengths rejected valid files, while unchecked seeks could accept truncated
values. Decision: validate declared lengths against the opened file's physical
remaining bytes, seek over unused strings and fixed-width arrays, and traverse
nested arrays iteratively. Only metadata used by the planner is decoded; no
metadata count, byte or nesting-depth quota controls admission. Alternatives:
larger quotas still reject valid files; recursive traversal risks stack failure;
decoding tokenizer strings retains unnecessary data. Type/version validation
and structured read/truncation errors remain. The [upstream GGUF specification](https://github.com/ggml-org/ggml/blob/master/docs/gguf.md)
defines array counts as element counts and permits nested arrays. Tests cover
10001 extra entries, long keys/strings, 1000001-element scalar/string arrays,
2048 nested levels and declarations beyond EOF.
Measurement: inspecting a sparse file containing 256 MiB of unused string data
took 0.140 ms with 5,239 bytes traced peak allocation;
the string payload is sought over, not read or decoded. This measures metadata
inspection, not model loading or tensor validation.

Ollama launch parallelism — Problem: every host and model was forced to one
request and one loaded model. Decision: for GGUF Llama/Qwen2/Qwen3 dense
attention, inspect layer/head dimensions and derive q8_0 KV allocation per
context. Parallel allocation uses the selected GPU's free VRAM (or available
RAM for CPU inference), subtracting the existing 1.2x
weights/workspace estimate. No fixed upper count is imposed. Missing or
unsupported layout evidence starts lean; insufficient estimated space does
not reject launch. The model-count override is removed. Plan evidence exposes
its inputs and estimate; the OOM ladder halves parallel allocation before
reducing context, never increasing a shorter context. Alternatives: deleting
NUM_PARALLEL retains upstream's current default of one; a large constant
preallocates KV without resource evidence. This sizes startup allocation, not
a task admission gate or proof of GPU throughput. Actual Ollama placement and
its upstream queue/architecture constraints remain; they require further
integration work to deliver the full demand-execution objective.
Sources: [GGUF attention dimensions](https://github.com/ggml-org/ggml/blob/master/docs/gguf.md),
[Ollama concurrency](https://docs.ollama.com/faq#how-does-ollama-handle-concurrent-requests).
The hardware snapshot captures an explicit `OLLAMA_NUM_PARALLEL` operator
override once; the planner preserves its literal value and Ollama validates it.
The OOM ladder is built from that requested count. q8_0 block accounting follows
the [GGML block definition](https://github.com/ggml-org/ggml/blob/master/src/ggml-common.h)
(32 quantized elements plus a two-byte scale).
Planning measurement: synthetic 5 GiB dense Qwen3 (32 layers, 8 KV heads,
128-wide K/V, 8192 context) with 12/24/48/96 GiB free VRAM yields
11/33/79/169 parallel allocations. Four plans took 0.458 ms,
with 6,079 bytes traced peak allocation. No engine/model was launched; these
are allocation estimates, not measured inference throughput or memory use.
CPU batch concurrency has no separate CPU-count ceiling: requests share the
engine's compute threads, while per-context KV allocation consumes real RAM.
Tests vary CPU counts with equal RAM and verify equal parallel allocation.

Managed local endpoints — Problem: the fixed 18100–18199 port band rejected
a 101st deployment even when the host had free listening ports. Decision:
prefer an OS-selected loopback endpoint, then scan unprivileged TCP ports if
its ephemeral choice overlaps planned records or its ephemeral range is full.
There is no managed port-count ceiling. Active/planned ledger rows are
excluded; stopped/failed history does not reserve ports. Restarting terminal
history allocates a fresh endpoint and atomically updates its port, launch
arguments/environment and base URL before spawning. Conventional engine
endpoints remain excluded; loopback binding and owner checks remain. Real
socket/FD/permission failures retain their cause. Alternatives: widening a
fixed band retains refusal; ignoring planned rows permits avoidable collision;
reserving terminal history converts durable retention into resource exhaustion.
The probe socket closes before the external engine binds it; a concurrent
external bind race still uses the existing startup failure/diagnosis path.
Tests cover 100 planned rows plus another endpoint, physical bound sockets,
kernel/ledger collisions, descriptor failure and restart for all four engines.
Measurement: selecting a real loopback port with 4,097 synthetic planned rows
took 0.458 ms with 164,104 bytes traced peak allocation (row creation
excluded). The probe socket closes on return; no listener, worker or durable
reservation is added. This measures endpoint selection, not model deployment.

Local physical loading — Problem: weight-size multipliers denied GPU/CPU
launches, and tight/extreme tiers injected request limits of 8/2 plus fixed
SGLang token pools. Decision: compatible requests reach actual engine loading.
vLLM uses automatic context sizing; SGLang retains automatic memory fraction,
prefill and token-pool sizing. Neither initial arguments nor OOM recovery adds
a Tofu request-count/token-pool ceiling. An estimated GPU overflow can offload
weights to CPU using observed available RAM, in whole GiB bounded by that
headroom; missing RAM evidence adds no offload hint and still permits launch.
Known zero free VRAM stays zero. CUDA graph removal and quantized KV recovery
remain, with actual failure/diagnosis preserved. CPU llama.cpp uses every
observed allowed CPU instead of subtracting a core; missing CPU evidence uses
one thread. Recommendation ranking may prefer estimated fits but cannot deny
a direct deployment. Alternatives: raising fixed counts retains local ceilings;
pretending extra VRAM exists loses physical failures; rejecting estimated
overflow prevents engine memory mapping/offloading from being tried.
Sources: [vLLM 0.28 engine arguments](https://docs.vllm.ai/en/v0.28.0/configuration/engine_args/),
[SGLang server arguments](https://docs.sglang.io/docs/advanced_features/server_arguments),
[SGLang offloader GiB conversion](https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/utils/offloader.py).
These changes do not remove upstream schedulers or prove a particular model
fits; physical engine performance and multi-GPU placement remain to verify.
Planning measurement: synthetic 24 GiB weights / 4 GiB free VRAM with
0/3/64 GiB available RAM yields 0/3/26 GiB offload hints for each HF engine,
and all six plans are accepted. Planning took 0.444 ms with 5,091 bytes
traced peak allocation. This measures configuration work; no real engine or
model was launched and physical CPU/GPU memory use remains unmeasured.
