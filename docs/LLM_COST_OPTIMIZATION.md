# LLM request cost and context policy

This note records the request-path cost audit and the defaults introduced on
2026-08-08. It is about repeated agent/tool rounds, not ordinary one-shot chat.

The 2026-08-14 GPT-5.6 P0/P1 rollout follows OpenAI's current builder guide:
official providers use Responses, the Sol/Terra/Luna family and exact current
limits/prices; Pro is a reasoning mode rather than a model slug; lean prompts,
PTC and Multi-agent are task-gated and represented as independent benchmark
arms rather than assumed wins.

## Measured baseline

A representative six-round Kimi task submitted 1,160,102 prompt tokens and
4,839 output tokens. Its per-round prompts grew from 185K to 194K tokens. Cache
reuse was generally high, but one byte-stable round still returned zero cached
tokens, so automatic provider caching cannot be treated as a cost bound.

The first-round snapshot contained 279 messages and 240 MCP schemas. The MCP
schema JSON alone was 223,534 bytes (about 55K tokens by a rough JSON estimate),
roughly one third of the serialized request. Longer production tasks had
accumulated 5–25 million prompt tokens across their tool loops.

## Policy

### 1. Progressive MCP schemas

When more than 16 MCP tools are enabled, Tofu exposes three stable meta tools:

- `search_mcp_tools` searches the local enabled catalog and returns bounded,
  exact schemas.
- `call_mcp_read_tool` accepts only tools annotated read-only.
- `call_mcp_write_tool` accepts only non-read-only tools and remains in the
  serialized, approval-eligible write partition.

The three schemas serialize to about 1.7 KB, a reduction of more than 99% from
the measured 223.5 KB MCP catalog. A task that actually needs a remote tool may
spend one extra discovery call; every round that does not need one avoids
shipping and tokenizing the full catalog.

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
  cache fingerprints inspect Responses `input` rather than an empty
  Chat-Completions `messages` field.

## Expected interpretation

Prompt caching reduces the unit price of a matching prefix; it does not make an
ever-growing prompt free. Progressive schemas reduce the fixed per-round floor,
while the working-set policy bounds the growing history. Both are necessary for
long coding-agent sessions. Cache misses can still occur because of upstream
eviction, rate-limit rerouting, or a genuine prefix change; the request
inspector now has the wire and cache-write details needed to distinguish them.

## Safe A/B channel

Settings → Advanced now exposes a low-code **Cost Optimization A/B
Experiment**. It is deliberately `enabled=false` by default. With the switch
off, assignment returns the exact original request-config object and adds no
task metadata. The first enablement defaults to a 10% conversation canary with
an even control/optimized split; the remaining 90% keeps its ordinary request
configuration.

The experiment has two fixed, reviewable arms:

| Arm | MCP exposure | Automatic L2 working set |
|---|---|---:|
| `control` | `inline` | `0` (window-safety trigger only) |
| `optimized` | `auto` | `128000` tokens |

Only enrollment traffic, optimized-arm share, experiment ID and minimum sample
size are editable. The policies themselves are not arbitrary JSON, which keeps
an admin typo from changing a model, tool permission or unrelated compaction
setting. Assignment uses a SHA-256 bucket over experiment ID + conversation ID,
so a conversation never crosses arms. A request that explicitly supplies
`mcpToolExposure` or `compaction.workingSetTokens` is excluded rather than
overwritten. Saving the switch/split travels through the existing atomic server
config writer and `server_config_change` audit event.

An experiment ID also locks its enrollment and arm percentages. Changing
either routing threshold requires a new ID; the server validates this before
any other settings mutation, so an in-flight experiment cannot silently
rebucket an existing conversation. Enable/disable and the report sample gate
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
ordinary configuration. Per-request explicit overrides remain an additional
escape hatch.

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
    "programmaticCalling": "auto",
    "toolSearch": "auto"
  },
  "responses": {
    "transport": "sse",
    "reasoningMode": "standard",
    "verbosity": "medium",
    "imageDetail": "auto",
    "promptProfile": "auto",
    "multiAgent": "auto",
    "maxConcurrentSubagents": 3
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
  current mutable state, so the summary tells the agent to revalidate when
  freshness matters.
- `compaction.evidenceLedger=true` additionally persists exact L1 cold tool
  results and adds their recovery handles to that ledger. It remains opt-in
  because durable raw-result storage has a different privacy/I/O trade-off.
- `tools.programmaticCalling=auto` is GPT-5.6 public-Responses-only. It marks
  only explicitly reviewed `ToolSpec.programmatic_tools` as callable directly
  or from a program; retry/idempotency metadata is deliberately not used as a
  proxy for PTC safety. Both routes share the exact
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
  `auto` is the shipped default, but it emits the provider feature only when
  the latest task describes a bounded many-result reduction and at least one
  reviewed read-only tool is eligible. `off` is the immediate rollback.
  The stateless replay preserves `program`, nested call `caller`, structured
  `function_call_output`, and `program_output` in API order. In chat, a program
  is persisted first as a canonical `programRuns` record, then projected to a
  backwards-compatible parent orchestration card with its generated
  JavaScript, enforced limits, child-tool names, live status, and aggregate
  result. Each real child remains below it with the ordinary arguments/result
  view. The UI labels this only as code-orchestrated; it does not guess serial
  versus parallel flow from JavaScript text.
- `responses.promptProfile=auto` selects the compact GPT-5.6 operating
  contract and keeps the full legacy prompt for other families. The benchmark
  promotes it only when the same oracle/evidence gates pass.
- `responses.multiAgent=auto` is first-round-only and requires independent,
  complex workstreams. Native subagents are analysis-only: Responses agent
  attribution survives conversion/replay, and the execution boundary rejects
  every non-root state-changing tool even if prompt guidance is ignored.
  Streaming and non-streaming surfaces expose only `/root`'s `final_answer`.
  Automatic PTC and Multi-agent are mutually exclusive per round: a bounded
  reduction prefers PTC, while an explicit read-only Multi-agent selection
  suppresses automatic PTC. Their benchmark evidence therefore remains
  attributable to one mechanism.
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
