# LLM request cost and context policy

This note records the request-path cost audit and the defaults introduced on 2026-08-08. It is about repeated agent/tool rounds, not ordinary one-shot chat.

The 2026-08-14 GPT-5.6 P0/P1 rollout follows OpenAI's current builder guide: official providers use Responses, the Sol/Terra/Luna family and exact current limits/prices; Pro is a reasoning mode rather than a model slug; lean prompts, PTC and Multi-agent are task-gated and represented as independent benchmark arms rather than assumed wins.

## Measured baseline

A representative six-round Kimi task submitted 1,160,102 prompt tokens and 4,839 output tokens. Its per-round prompts grew from 185K to 194K tokens. Cache reuse was generally high, but one byte-stable round still returned zero cached tokens, so automatic provider caching cannot be treated as a cost bound.

The first-round snapshot contained 279 messages and 240 MCP schemas; schema JSON
alone was 223,534 bytes (about 55K tokens), roughly one third of the request.
Longer production tasks accumulated 5–25 million prompt tokens in tool loops.

## Policy

### 1. Progressive MCP schemas

When more than 16 MCP tools are enabled, Tofu exposes three stable meta tools:

- `search_mcp_tools` searches the local enabled catalog and returns bounded, exact schemas.
- `call_mcp_read_tool` accepts only tools annotated read-only.
- `call_mcp_write_tool` accepts only non-read-only tools and remains in the
  serialized, approval-eligible write partition.

The three schemas serialize to about 1.7 KB, over 99% below the measured 223.5 KB
catalog. A task needing a remote tool may spend one discovery call; every other
round avoids shipping and tokenizing the full catalog.

Controls:

```text
request config: mcpToolExposure=auto|inline|progressive
request config: mcpInlineToolLimit=16
environment:    TOFU_MCP_TOOL_EXPOSURE
environment:    TOFU_MCP_INLINE_TOOL_LIMIT
```

### 2. Economic working set

Window safety and economic efficiency are separate constraints. The existing
90%-of-usable-window trigger prevents overflow, but on a 1M model it waits until
roughly 778K tokens. The effective automatic summary trigger is now:

```text
min(window_safety_threshold, working_set_tokens)
```

The default working set is 128K tokens. It includes the live tool schemas in
token counting and changes the automatic Layer-2 preserve budget accordingly,
so a 1M window no longer prevents the summarizer from finding a cold region.

Controls:

```text
request config: compaction.workingSetTokens=128000
environment:    TOFU_WORKING_CONTEXT_TOKENS=128000
opt out:        set either value to 0
```

Fixed automatic L2 uses observed-survival ROI: rounds 0–3 keep the one-round
rewrite rule; completing rounds 4/8/16/32/64 earns horizons 2/3/4/5/6. The hard
remaining API-round budget caps the exact pre-summary/adoption checks. Manual,
reactive, hard-window and adaptive economics are unchanged.

On 2026-08-27, nine distinct long tasks' earliest declines projected 1.18–3.89
rounds to repay; all ran past break-even, a rough 70.3M prompt-token exposure
upper bound. This is not billed savings: cache discounts, prompt evolution and
behavioral effects require actual receipts. Retry witnesses record their
horizon; each earned step invalidates them and reruns exact economics.

### 3. GPT-5.6 Responses caching and compaction

Only GPT-5.6-family Responses requests receive these OpenAI-specific fields;
generic Responses-compatible providers are unchanged. The ChatGPT Codex
subscription profile keeps the fields its internal backend supports and omits
the public-only explicit breakpoint and `context_management` request fields:

- `prompt_cache_key` is a stable SHA-256-derived conversation namespace; raw
  conversation/task identifiers never leave the process in this field.
- On the public Responses profile, the last text block in the stable developer
  prefix receives an explicit cache breakpoint. The Codex subscription profile
  uses its implicit cache plus the stable `prompt_cache_key` because its Luna
  backend rejects `prompt_cache_breakpoint`.
- Encrypted reasoning output items are saved and replayed with `store=false`,
  with `reasoning.context=all_turns`.
- On the public Responses profile, `context_management` uses the same
  working-set threshold. The Codex subscription profile keeps Tofu's local
  compaction path. Returned opaque compaction items are persisted; input before
  the latest item is pruned while the current system instructions are retained.
- Usage conversion preserves both `cached_tokens` and `cache_write_tokens`, and
  cache fingerprints inspect Responses `input` rather than an empty Chat-Completions `messages` field.

## Expected interpretation

Prompt caching reduces a matching prefix's unit price; it does not make growth
free. Progressive schemas reduce the fixed floor while working-set policy bounds
history. Cache misses from eviction, rerouting or real prefix changes remain
distinguishable through request-wire and cache-write evidence.

## Safe A/B channel

Settings → Advanced exposes a low-code **Cost Optimization A/B Experiment**,
disabled by default. Off returns the original request config and adds no task
metadata. First enablement defaults to a 10% conversation canary with an even
arm split; the other 90% keeps its ordinary configuration.

The experiment has two fixed, reviewable arms:

| Arm | MCP exposure | Automatic L2 working set |
|---|---|---:|
| `control` | `inline` | `0` (window-safety trigger only) |
| `optimized` | `auto` | `128000` tokens |

Only enrollment, optimized share, experiment ID and minimum sample are editable;
policies are not arbitrary JSON. SHA-256 bucketing over experiment + conversation
ID keeps conversations in one arm. Requests explicitly setting `mcpToolExposure`
or `compaction.workingSetTokens` are excluded. Saves use the atomic server-config
writer and `server_config_change` audit event.

An experiment ID locks enrollment and arm percentages. Changing either requires
a new ID, preventing silent rebucketing. Enablement and the report sample gate
remain editable without changing assignment.

Every assigned assistant turn persists:

- provider-reported prompt/output/cache token counts;
- the all-in cost snapshot after compaction-model usage has been folded into
  the turn total;
- pricing provenance (`provider_override`, `model_table`, `qwen_tier`, or an
  estimate label), latency, API rounds, actual compaction mutations and the
  terminal-without-error operational proxy;
- the experiment/arm tag on the assistant message and API-round records.

The 14-day report samples by **conversation**, not by turn. It shows both the
conversation and turn counts, cost per fully priced conversation (the primary
metric), cost per priced turn (diagnostic), pricing coverage, prompt tokens,
cache-read ratio, latency, error-free-terminal rate, model/provider mix and
compactions. The readiness gate also requires the configured number of fully
priced conversations in each arm. Unknown models keep the legacy estimated amount in the normal
cost UI, but `default_estimate` is counted as **unpriced** in the experiment —
never as zero and never as a real-cost observation. The error-free rate is not
a semantic-quality score; evaluate task completeness/evidence separately
before promoting the optimized policy.

Rollback is one switch: disable the experiment. Existing tagged history stays
available for analysis, while all new requests immediately return to their
ordinary configuration. Per-request explicit overrides remain an additional escape hatch.

## Open-source harness comparison

The production policy borrows the convergent parts of three current harnesses,
without copying their implementation blindly:

| Harness | Current compaction shape | Tofu alignment |
|---|---|---|
| [OpenAI Codex](https://github.com/openai/codex/blob/main/codex-rs/core/src/compact.rs) | Builds a local summary, retains recent real user messages under a bounded budget, and reinjects initial context. It also records compaction telemetry and warns on repeated compaction. | Tofu retains stable system instructions, keeps a hot tail, archives boundaries and records experiment outcomes. |
| [OpenCode](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts) | Prunes old tool results before full summarization, protects a recent token/turn tail, then uses a separate compaction agent/model and folds the prior summary forward. | Tofu L1 performs zero-cost old-tool-result reduction; L2 uses a cheap summary model and preserves a bounded recent working set. |
| [Aider](https://github.com/Aider-AI/aider/blob/main/aider/history.py) | Keeps a recent tail, summarizes the older head, and recursively re-summarizes if the result still exceeds the budget; it can use a weaker model for the summary. | Tofu uses the same old-head/recent-tail split and folds summary-model usage into the all-in cost instead of hiding it. |

The initial experiment intentionally compares the compatibility baseline with
the shipped economic policy. It does not simultaneously vary summary prompts,
tail algorithms or models: changing multiple mechanisms at once would make a
cost delta impossible to attribute. Those can become later experiment IDs once
this channel has enough coverage and a semantic-quality evaluator.

### Upstream code-orchestration comparison (2026-08-10)

- [OpenCode CodeMode](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/code-mode.ts)
  is a real analogous implementation, but not the OpenAI Responses PTC
  protocol. Behind `OPENCODE_EXPERIMENTAL_CODE_MODE`, it replaces individually
  exposed MCP calls with one `execute` tool backed by a confined local
  JavaScript subset. It derives the callable tree from visible MCP permissions,
  checks permission and plugin hooks again for every child, gives each child a
  stable derived ID, records child lifecycle, carries structured MCP output and
  attachments, supports cancellation and caps live child concurrency at eight.
  Its reusable runtime supports timeout/call/output budgets, but the OpenCode
  host integration currently supplies none of those three limits. Tofu adopts
  the explicit authority boundary, lifecycle, UTF-8 output bound and concurrency
  cap, while retaining OpenAI's hosted V8/runtime/replay protocol and excluding
  writes, approvals, citations and native artifacts from program eligibility.
- [Pi's agent loop](https://github.com/earendil-works/pi/blob/main/packages/agent/src/agent-loop.ts)
  contains no `programmatic_tool_calling`, `allowed_callers`, `caller_id`, or
  `program_output` protocol path at the reviewed revision. It executes ordinary
  assistant tool calls either sequentially or in parallel, lets a tool declare
  `executionMode="sequential"`, preflights calls in order, runs the prepared
  parallel batch with `Promise.all`, and emits result messages in original call
  order. Tofu already preserves those useful scheduling/ordering properties,
  but adds a first-class program parent, explicit eligibility and per-program
  safety/evaluation records.

## Context-efficiency controls

Request-local switches support single-factor ablation without process-global
state. Public GPT-5.6 Responses requests additionally get automatic native
Tool Search for the non-pinned residual catalog:

```json
{
  "cache": {"gpt56BreakpointMode": "explicit"},
  "tools": {
    "nativeExposure": "routed",
    "programmaticCalling": "on",
    "toolSearch": "auto"
  },
  "responses": {
    "transport": "sse",
    "reasoningMode": "standard",
    "verbosity": "medium",
    "imageDetail": "auto",
    "promptProfile": "auto"
  },
  "orchestration": {
    "multiAgent": "auto",
    "maxConcurrentAgents": 3
  },
  "compaction": {"evidenceLedger": false}
}
```

- `cache.gpt56BreakpointMode=explicit` adds the GPT-5.6 public Responses API
  `prompt_cache_options.mode=explicit` control. The stable developer-prefix
  breakpoint remains; the dynamic implicit breakpoint is disabled. The
  default `implicit` value preserves the prior stable-explicit plus dynamic-
  implicit behavior.
- `tools.nativeExposure=routed` conservatively selects native tool families
  from the latest task while retaining read/inspection, MCP discovery and
  custom/plugin safety surfaces. `full` is unchanged. The report records both
  exposed tool count and exact schema-token cost so routing is judged on token
  reduction and discovery recall, not catalog size alone.
- `tools.toolSearch=auto` never removes tools from the task authority catalog.
  Eager tools, explicit caller tools, the current task's MCP active set and a
  forced `tool_choice` remain direct; searchable residual tools are deferred
  natively or reached through the local gateway. Each model turn assembles its
  wire catalog and discovery metadata from current settings; provider
  conversion uses that request-local projection.
- L2 always receives a bounded in-memory working-state ledger of modified
  files, tests, errors, query conclusions, command/agent results and unfinished
  work. This closes the state-loss gap created by intentionally excluding raw
  tool messages from the prose summary input. Stable evidence IDs make retained
  and lost facts auditable after compaction; the transient ledger is released
  after that measurement. Entries are past observations rather than proof of
  current mutable state, so the summary tells the agent to revalidate when freshness matters.
- `compaction.evidenceLedger=true` additionally persists exact L1 cold tool
  results and adds their recovery handles to that ledger. It remains opt-in
  because durable raw-result storage has a different privacy/I/O trade-off.
- `tools.programmaticCalling` accepts `on` (shipped default), `auto`, and
  `off`, and resolves per request into one of two execution backends via
  `resolve_programmatic_backend` (lib/tools/programmatic.py): `native_openai` on the GPT-5.6 public
  Responses API, or `local` for every other tool-capable wire (any
  protocol, provider gateway, or OAuth profile). `off` remains the
  immediate rollback; no eligible reviewed read-only tool also resolves
  `off`. Both backends mark only explicitly reviewed
  `ToolSpec.programmatic_tools` as callable from a program;
  retry/idempotency metadata is deliberately not used as a proxy for PTC
  safety. Both routes share the exact
  `{content: string, truncated: boolean}` envelope. Writes, approval-sensitive
  tools, plugins, web search/citation surfaces, image/native-artifact surfaces,
  skill loading, and MCP discovery remain direct calls.
  Application-side enforcement rejects direct-only or excess program calls,
  limits each program to 16 child calls, at most eight concurrent client-tool
  executions, 1 MiB of UTF-8 child output, and four protocol continuations,
  while the hosted V8 service owns program execution timeout.
  Raw and delivered output bytes plus per-child/per-program truncation are
  persisted in `programRuns`; initial benchmark promotion requires an observed
  completed route with zero rejection, budget violation, or output truncation.
  Runs carry a source tag, so local ToolScript execution cannot be mistaken for
  evidence that the provider-hosted OpenAI PTC protocol actually ran.
  `on` is the resident mode: any round whose assembled tool list contains a
  reviewed read-only tool exposes the tier-shaped programmatic surface, so
  EVERY tool-capable model can reach it without passing a text-intent gate
  (`resident_eligible_read_tools`). `auto` keeps the legacy bounded
  reduction triggers: it activates only when at least one reviewed
  read-only tool is eligible AND the round shows a bounded reduction
  shape — either the latest task text describes a many-result reduction,
  or recent rounds already show eligible read-only fan-out
  (`observed_read_fanout`: the model issued several reviewed read-only
  calls in parallel or across consecutive rounds, so collapsing the
  remaining reads is the same bounded reduction; this is the main
  small-model win because small models tend to serialize reads). `off` is the immediate rollback.
  The stateless replay preserves `program`, nested call `caller`, structured
  `function_call_output`, and `program_output` in API order. In chat, a program
  is persisted first as a canonical `programRuns` record, then projected to a
  backwards-compatible parent orchestration card with its generated
  JavaScript, enforced limits, child-tool names, live status, and aggregate
  result. Each real child remains below it with the ordinary arguments/result
  view. The UI records the execution source and labels the parent as either
  local ToolScript or native OpenAI; it never infers native PTC merely because
  code orchestration occurred. The `local` backend projects the `execute_tools`
  gateway schema
  at the wire boundary (`ptc_local_wire_tools`, resolved by
  `resolve_programmatic_backend` in `prepare_request`, following the Tool
  Search dual-backend precedent). There is no model-size split: every
  tool-capable model authors bounded ToolScript reductions whose child
  results stay server-side; a malformed program earns a typed, retryable
  interpreter error, and the read-only latch plus the hard call/byte
  ceilings bound any damage. `TOFU_PTC_TIER=batch` remains as an
  operator/benchmark override exposing only parallel `calls[]`. At a fixed
  tier, gateway bytes stay stable; eligible names stay in the execution latch
  and observed chains in telemetry, never provider schemas. Local programs may call the reviewed
  eligible read-only tools plus `search_tools` — the gateway handler
  enforces this per round from the `task['_ptc_local']` latch with a
  typed `tool_not_program_eligible` rejection, while ordinary
  `execute_tools` `calls[]` batches keep their normal admission/approval
  path. Local runs reuse the same `programRuns` bookkeeping and program card UI as the hosted backend.
- `responses.promptProfile=auto` selects compact GPT-5.6 but keeps Kimi on full.
  Explicit Kimi lean/ablation arms emit applied profile, SHA-256 and token proof;
  the benchmark rejects missing/mismatched adoption before any promotion.
- `orchestration.multiAgent=auto` is first-round-only and requires independent,
  complex workstreams; `read_only` forces the lane and `off` is the immediate
  rollback. The provider-neutral policy in
  `lib/tasks_pkg/tool_orchestration_policy.py` selects this control plane and
  the programmatic data plane independently. Both may therefore be active:
  agents partition independent workstreams, while the root and each eligible
  local worker use PTC to reduce repeated reads inside its own workstream.
  `lib/swarm/routing.py` selects native OpenAI acceleration only for a verified
  public GPT-5.6 Responses face and otherwise projects the existing local
  `spawn_agents` runtime for every model whose task authority includes it.
  Native and local workers are analysis-only: the shared execution boundary
  rejects file, shell, artifact, scheduler, project-state, integration, and
  other non-root mutations even if prompt guidance is ignored. Streaming and
  non-streaming surfaces expose only `/root`'s `final_answer`.
  Benchmark arms remain single-factor experiments for causal attribution; that
  experimental isolation is not a production mutual-exclusion rule.
- Each terminal task result keeps bounded reason, `expectedSavings`, and
  `projectionEvidence` per routing decision. Provider wire projection is never
  adoption: only a real program run, native multi-agent call, or launched local
  agent wave enters `adoptionEvidence`; `adoptionStatus` is re-derived from
  those trajectories whenever the task is persisted or benchmarked.
- `responses.reasoningMode=pro` keeps the selected GPT-5.6 model and adds
  `reasoning.mode=pro`; it never invents a `gpt-5.6-pro` slug. Reasoning effort
  stays independent and legacy `ultra` is normalized to official `max`.

The terminal outcome now stores `oraclePassed`/`oracleType` separately from
`terminalWithoutError`. The latter is only a health signal. It also stores the
dataset/task/agent/model/effort/arm identity, actual and frozen-public-price
costs, uncached/cache-read/cache-write/output/reasoning tokens, per-round
prefix/schema/tool-result shape and fingerprint, per-round API usage/latency,
compaction reason and evidence retention, plus MCP searches/misses. Historical
assistant rows remain valid because every new field is optional.

## Reproducible benchmark contract

`lib.benchmark_contract` defines an append-only `tofu-benchmark/v1` JSONL
format: one manifest followed by task records. A manifest freezes the task
list, environment snapshot, agent/model/effort/arm, timeout, network policy,
agent constraint, retry rule and budget. The Multi-agent arm records
`singleAgent=false`; all other arms remain single-agent. A task record carries the final
patch, oracle/tests, per-round usage, prefix fingerprints, costs, context and
compaction telemetry, artifacts and infrastructure error.

Only failures explicitly classified as `infrastructure` may be retried, up to
the manifest's predeclared limit; agent failures are final. The budget helper
pauses new tasks at $1,200 for a reforecast and stops at the $1,500 hard cap.
It also computes cost from a frozen public price card so subscription runs can
report throughput/limit consumption and a comparable shadow cost without
pretending the monthly subscription is a per-task API invoice.

Confirmation uses paired oracle results. A candidate with no fewer resolved
tasks but a negative one-sided 95% lower bound is reported as an observed tie
without a population-level non-inferiority claim. Cost per successful task is
considered only after the quality gate, and release additionally limits p90
latency regression to 20%. Rollout remains an operational 5% → 25% → 100%
decision; every arm can be disabled independently with the request switches.

Before a paid PTC arm, `python scripts/ptc_live_smoke.py --dry-run` validates
the request fixture. With an explicitly configured `OPENAI_API_KEY`, the same
script performs a deterministic read-only live check of program/caller/output/
final-message replay. No live check runs implicitly.

## Long-agent Codex parity contract (v2)

The 2026-08-24 comparison keeps the model fixed: Tofu and Codex CLI 0.149.1
both call the same Meituan `kimi-k3` slot with the same thinking setting. The
isolated loopback-only `evaluations/codex_kimi_proxy` translates one Codex
Responses request into exactly one Kimi Chat Completions request. It preserves
instructions/messages, strict function schemas, tool choice, thinking, usage,
SSE order, errors, and cancellation. It never joins production routing, reads
user configuration, or logs a key. `/responses/compact` invalidates the trial.
Codex uses local compaction and its pinned 0.149.1 fallback: 272,000 context tokens and a 244,800 compact limit;
metrics separate raw wall, proxy CPU, translation CPU and Codex-favored wall.
Namespace functions flatten; native `web_search` is suppressed and recorded;
unknown native types fail closed. A real CLI smoke proves one Kimi call.
The formal Harbor `codex-kimi` profile now owns the proxy for the whole
start/resume lifecycle, uploads and re-hashes the pinned CLI inside each
disposable guest, removes the Kimi URL/key variables from Harbor's child
environment, and retains tagged raw JSONL plus per-call provider usage. QEMU
exposes only `10.0.2.101:<fixed guest port>` through a private Unix relay whose
host destination is predeclared; the real host port never appears in QEMU's
arguments. Audit replays every completed trial through the raw/proxy projector.
The runtime freezes provider/Harbor/runner identities and preclaims release tasks
before dispatch; export binds raw/proxy/ATIF evidence and full retry wall/cost.
Dirty runners, hidden internal retries, dropped failure attempts, and identity
drift fail closed; launch failures never become completions.

The paired Harbor `tofu-kimi` profile runs the public production `AgentRuntime` with host-only credentials and only two exclusive guest client tools. Native
events, sanitized runtime/tool evidence, and ATIF-v1.7 are reconciled by current audit and `export-tofu-harbor`, including every model/compaction round, prompt,
schema, tool call/result, usage/cache/timing, final output, and verifier lifecycle. Frozen runtime/prompt/tool/provider/slot/thinking/arm/revision digests bind
the trial; candidate latency is raw task-start-to-oracle-ready wall with no proxy correction, and failed attempts, compactions, and paid tools remain charged.

`tofu-benchmark/v2` remains read-compatible with v1 and freezes pair/role/arm,
harness and agent hash, Kimi face/slot/thinking, prompt/tool-schema SHA-256,
permissions, sandbox/network, timeout, retry rule, three artifact byte limits,
dataset snapshot, price card, and task table. Its formal matrix is 1,845 trials:
SWE-bench Verified 500, Terminal-Bench 2.1 89×5, and frozen integrated-tool,
continuity, research, writing, and fault sets of 200/200/200/200/100. Each task
retains per-call usage/cache, queue/TTFT/model/tool/oracle-ready timing, context,
schemas/results, compactions, call graph, retries, oracle, incidents, judges,
and content-addressed raw trajectory. Failed/compaction calls and paid tools
count toward agent cost; the recorder reprices evidence with the frozen card.
Simulator and judge calls remain separate. Kimi costs are input $2.76/M,
output $13.81/M, with cache reads at 0.1× input.

The release decision is deliberately conjunctive: paired quality must meet the
Codex point/lower-bound gates, every family remain within -5pp, both blind
judges pass, critical incidents remain zero, cost/success and P90 oracle-ready
remain at most 85% of Codex, and infrastructure failures stay preregistered.
The full task records must also contain real program and agent trajectories
with zero projection-as-adoption claims. A failed gate yields `not
demonstrated`; no family or unused orchestration lane is hidden by an average.
Immutable `pair-report` re-audits finalized stores and their attempt ledgers,
derives all gates, and keeps pilots at `releaseEligible=false`.

The request-local experiment plugin exposes fixed, independently reversible
arms: `prompt_lean_kimi` plus five prompt ablations, `tool_surface_v2`,
`tool_result_v2`, context budgets 64k/96k/128k, `adaptive_compaction_v2`, and
`orchestration_v2`. `combined_v2` cannot be created through the maintained
helper until all six mechanisms are independently registered as winners.
Enrollment defaults to a 10% pilot; full paired evaluation and the operational
5% → 25% → 100% rollout remain separate gates.

The v2 runtime contracts are:

- `ContextPlanV2` and rebuildable `TaskStateSnapshotV1`, with locked required
  blocks, one deterministic global budget, explicit suppression/hash/token/
  recovery evidence, and cache epochs that advance only on semantic changes;
- `ToolContractV2`/`ToolResultEnvelopeV2`: every model defaults uncapped; an explicit
  neutral local budget preserves the code core, with 500/8,000/24,000-token gateway/result/round targets and owner-scoped artifact cursors;
- four explainable orchestration shapes—direct, bounded PTC reduction,
  independent read-only agents, or a verified loop—and a no-progress ledger
  that cannot infer completion from non-empty prose;
- adaptive compaction based on projected cache-adjusted savings, remaining
  rounds, call cost, and evidence loss, while window safety always wins and a
  summary that drops pending work or evidence is rejected.

`evaluations/long_agent_release` compiles locked SWE 500, TB 89×5, and five private packs into exactly 1,845 rows; drift or synthetic rows fail closed. Its run
store binds arm/task/oracle, reprices usage, verifies raw evidence, and finalizes only complete paired JSONL. Paired SWE/TB Codex and production-Tofu
launch/export paths are present, but the private 900 tasks, simulator launchers, and paid matrix remain absent, so no Codex-leading claim exists.
Root/endpoint share `run_agent_loop`; Paper remains 3,702 schema tokens and 29 executable tools (arms: 8,823/3,496).
