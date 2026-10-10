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
   `data/local_serve/` after the explicit disk-budget preflight (default 20 GiB).
4. `_process` owns spawn, bounded rotating logs, readiness, OOM retry, stop, and
   restart.
5. `_store` owns the capped durable instance ledger at
   `data/config/local_serve.json`; environments and logs are reconstructible.
6. `_register` publishes the endpoint through the shared owner-scoped v2 local
   provider service. Discovered wire IDs are `pending_identity` Offerings until
   independently matched to an official model identity; dispatcher routing,
   health checks, and Settings use that same authority from then on.

The plan table follows current engine contracts: vLLM 0.28 is V1-only and does
not generate removed in-tree bitsandbytes flags; SGLang 0.5 owns its memory and
chunked-prefill tuning and does not generate removed/unmaintained flags;
Ollama's KV lever is `OLLAMA_KV_CACHE_TYPE=q8_0`; llama.cpp uses `--ui-config`.

Managed servers bind loopback ports 18100–18199 only, never well-known engine
ports, so they cannot shadow operator-run engines or trip autodiscovery.
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
