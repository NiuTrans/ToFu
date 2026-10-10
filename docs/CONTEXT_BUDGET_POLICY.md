# Coordinated route budgets and prefix rewrites

## Native route scope

Native application admission binds the compiled route's declared window, actual
clamped output and logical counter model through native_context_scope. Matching
owner/task/model identities are required; learned provider/wire limits can only
shrink the declaration. The scope resets after the canonical preparer completes.
The native stdio contract owns tool projection for both Rust requests and Python
counting. Semantic L2 needs an existing authorized pin whose matching slots have
the compiled owner/provider/offering/deployment/connection/credential identity;
without that port, native pressure refuses rather than selecting a global route.
Ordinary slot-based admission retains its existing window and output behavior.

**Problem.** Model-routing v2 declared Model/Offering context windows were not
carried into runtime Slots. Compaction consulted the separate legacy registry
and model-name estimates; a configured 1M route could therefore run as 128K.
Separately, final admission forced the economic working-set target after the
proactive ROI gate had declined a cache-negative rewrite. A 48K L1 tool tail
was incorrectly treated as if it bounded all messages despite cache protection
and reasoning replay.

**Decision.** Both pooled and request-scoped Slots carry the smaller declared
Model/Offering window. `lib/llm_dispatch/context_policy.py` resolves only within
an execution pin or explicit owner, conservatively intersecting matching route
candidates; the selected slot remains authoritative on retry/fallback. Learned
provider/wire shrink can reduce the declared window, never expand an Offering
restriction. Body construction, completion clamping and final transport
recount receive that same window. No alternate persisted registry is created.
The existing conservative window-safety reserves remain; this change does not
claim that larger prompts or smaller output reserves are risk-free.

Every dispatch, including the first, now uses the same route-derived safety
ceiling and full rendered-prompt measurement. The former fixed 256K first-round
host ceiling is removed. Explicit requested output is reserved within physical
capacity. Model pricing tiers and economic targets cannot force a summary below
safety pressure, and mandatory pressure cannot justify an incomplete summary:
failure preserves history and refuses the overfull dispatch.

**Problem.** Folding only tool spans retained every intervening assistant row
in a long single turn. A preservation budget therefore did not bound the
actual working projection, and a complete summary could still fail admission.
**Decision.** Automatic L2 treats preceding assistant commentary and reasoning
as part of each execution unit. Cold units enter the same summary in source
order; the latest complete unit, unfinished execution, native policy, current
request and managed state remain intact. Before its single summary call, the
planner measures the attributed retained projection with tools, history
wrappers, summary decorations and managed restoration. It selects a fitting
whole-unit suffix and reserves the actual summary transport allowance, derived
from one quarter of the prompt ceiling and capped at the existing 65,536-token
allowance. Incomplete provider output is still rejected without text slicing.
An impossible required floor refuses before summary spending; an overfull
completed candidate is rejected before archive/adoption. Deterministic suffix
planning performs no model calls or durable writes. Suffix trials are bounded
by the preservation budget's bit length and use pass-local projections; no
worker or retained cache is added.
**Alternatives rejected.** Repeated summary calls add latency and cost;
post-summary head deletion loses evidence; raising admission ceilings conceals
incorrect accounting. Specs: `test_dispatch_compaction_plan.py` and
`test_compaction_history_wire.py`.

## AA-cheapest compression-first routing
**Problem.** Requiring a cheaper summarizer to match the main model's ordinal
quality tier prevents the owner's requested cheap, high-AA compression route;
AA-first ranking could instead pay more than the cheapest eligible option.
**Decision.** Task-owned summaries default to `aa-cheapest`. Selection is
lexicographic: **lowest projected USD cost, then highest exact AA Intelligence
Index, then deterministic execution-pin/model ordering**. “Cheapest options”
means exact minimum-cost ties, not an undocumented near-cheapest price band.
`qualified-cheapest` is a compatibility alias; explicit `main` opts out.

Eligible candidates remain inside the same initialized execution-provider,
billing-provider, provider-access, owner and tenant scope. No slots/credentials
are created; no network probe or fallback expansion occurs. Every reachable
offering needs confirmed identity, text capability, known input/output pricing
for the prompt tier, context/output headroom and a fresh exact-model AA score.
The existing `cached_routing_scores` authority excludes stale, ambiguous and
unmatched variants without fetching inline. Missing AA is unknown, not zero;
zero is a valid known score. Ordinal quality/roles/auto-selectability are not
summary-specific AA thresholds. This compression policy does not change their
meaning for other model-selection consumers.

Compare the worst reachable candidate cost with the best reachable main cost;
require strict savings. Use the lowest reachable AA score for a candidate, not
its strongest offering. Main needs confirmed identity, price and capacity, not
an AA score. Invalid/missing evidence, equal main cost, subscription pricing and
`disableModelFallback` retain main. Dispatch still revalidates route/cost before
optional spending; route drift or a higher quote refuses and keeps source.
Receipts optionally record bounded `summary.route.selectionBasis` (score,
exact-match source, timestamp, ranking and eligible-route count) separately
from price evidence. The canonical knob/qualification contract is
`contracts/config_knobs_v1.json`.

**Limits and rejected alternatives.** AA is an accepted general-intelligence
proxy, not a measured summary-fidelity or latency guarantee; thinking remains
disabled on the summary call, and complete-candidate validation still controls
adoption. Reject a hidden price band, AA-first higher spending, ordinal-tier
blocking despite exact AA, variant score inheritance, stale-score ranking and
silent subscription-to-paid fallback. No live paid-model speed/fidelity result
is implied. Specs: `tests/compaction/test_summary_routing.py`,
`tests/compaction/test_summary_completion_policy.py`,
`tests/compaction/test_compaction_receipt_contract.py`.

## Route-budget model knowledge
**Astra capacity and pricing (2026-09-30).** The official [Astra model specification](https://developers.openai.com/api/docs/models/gpt-6-astra)
declares 1,050,000 context tokens and 128,000 maximum output. Exact model knowledge
uses the existing `lib/model_info/data/openai.json` contract rather than a
global 272K subscription-default override. The 272K rate-card threshold
remains economic policy, not capacity or a measured quality optimum. A narrower
registered Offering or learned provider ceiling still wins runtime admission;
no existing registrations are automatically expanded. Generic model knowledge
alone does not prove a live subscription route accepts the public API maximum.
The economic target can request a rewrite but cannot force one. Before local
L2, `_pressure.py` may evaluate one detached L1 candidate using the same target.
It may fold a warm tool/image prefix, keeps the newest complete tool batch and
all reasoning, and derives the hot tool budget from one quarter of the available
target (capped by the normal 48K tool budget). Adoption requires that the complete
candidate fit the target; optional adoption also uses the shared cache-payback horizon
and minimum reduction gate. Mandatory window/host recovery may bypass ROI.
A candidate that still needs L2 is discarded untouched, avoiding a partial L1
rewrite immediately followed by an L2 rewrite. No summarized durable Turn is
edited. Tools remain paired; unconsumed newest evidence can make L1 insufficient.

The opt-in evidence-ledger archival transform stays on its existing L2 path;
pressure trials never create speculative files. Rejected trials emit no
tool-compacted events, compaction telemetry, or cache-break notifications.
Accepted L1 records the earliest changed message index, preserving detection
of unrelated earlier cache breaks. L1 adoption does not publish an L2 receipt.
Default cache-safe L1 remains cheap on every round; pressure candidates only
run when a compaction gate fires. Economic hysteresis still suppresses repeated
low-yield/negative attempts. The candidate is pass-local O(prompt) memory,
released immediately; there is no new worker, queue or long-lived cache, at most
one candidate plus one deterministic application per decision. Task diagnostics
retain one pressure decision and one budget decision; admission history remains
bounded to eight records. Budget audits include window source, effective window,
economic target and safety ceiling, separating estimates from provider usage.

**Legacy model-name knowledge (the remaining name-based half).**
`lib/model_info/_context.py` still resolves windows for models no configured
route declares, and the Context Health Bar reads it through
`per_model`/`resolve_model_context_profile`. Its matching contract is now
explicit and shared:

1. LINE rules parse a tiered line's tier token and generation through one
   parser per family (`lib.model_info._family.deepseek_line_version`), so every
   spelling of one served model resolves identically and a NEWER generation of
   a known line needs no new entry.
2. Needle rules are compared against the separator-folded spelling ('.' / '-' /
   '_' version punctuation is not identity, matching `_folds.family_key`).
3. Family estimates last; unknown stays `None`.

A line's window belongs to the line: `deepseek-v4.1-flash` (catalogue
identity), `deepseek-v4-1-flash`, `deepseek-v4-flash` (legacy wire spelling)
and the versionless canonical `deepseek-flash` are one served model at 1M. The
2026-09-21 gauge report (128K shown for a route enforcing 1M, live on
sankuai/Meituan) came from spelling these as needles `deepseek-flash` /
`deepseek-v4-flash`, neither of which is a substring of an id whose version is
punctuated with a dot, so the V3-era 128K family estimate won silently. The
max-output table (`_max_output._deepseek_max_output`) rides the same parser.

Known bound: `per_model` prefers this knowledge over an Offering's declared
ceiling, so a route whose declared window differs from knowledge can still
display the knowledge value (pinned by `test_context_profile_contract.py`,
2026-09-04: a discovery claim must not shrink a verified vendor window). The
gate and the gauge agree for every model whose declared window matches its
line's knowledge — the failure mode this contract removes is the silent
family-estimate fallback, and a spec now pins every DeepSeek V4+ spelling
shipped in any template.

**Alternatives considered.** Adding only a DeepSeek spelling would hide the
configuration propagation defect for other gateways. Globally shrinking the L1
hot tail would needlessly rewrite cheap warm prefixes. Increasing all safety
thresholds would reduce output headroom without token-error evidence. Removing
all host limits would break the personal-computer resource budget. None fixes
the conflicting economic and mandatory authorities, so those alternatives are
not used.

Verification: `test_route_context_budget.py`, `test_compaction_pressure_policy.py`,
`test_prompt_admission.py`, `test_wire_input_admission.py`,
`test_context_profile_contract.py` (line rules, separator folding, shipped
spellings), `test_claude_line_version.py` (both generation parsers), and the
existing compaction invariant/pairing/recovery suites. Offline tests verify
decisions and boundaries, not actual provider cache hit rates, TTFT, or
task-quality gains.


## Dispatch recovery

**Problem.** A successful L2 summary can leave an oversized unconsumed tool
batch. A conservative estimate then refuses local admission, while Continue
reconstructs hundreds of already summarized tool records. Estimator calibration
also had no connection to successful main-provider usage.

**Decision.** Final admission stashes the raw estimate of the final rendered
request, including its selected tool schemas. Response-authoring usage feeds
the task-local calibration loop using the shared cache/input convention; the
next measurement consumes each matched sample once before applying its ratio.
Discarded billed retries, mismatched models and summary calls cannot calibrate
the main request. Existing physical reserves and uncalibrated safety guards
remain. Local refusal after unsuccessful convergence raises the typed local
compaction error; it is not evidence of provider-window rejection.

Under mandatory pressure, the newest complete tool batch may keep its calls
and replace oversized textual results with bounded v2 views after exact
owner-scoped artifact retention succeeds. Source reruns do not count as
retention. Persistence failure, cancellation, opaque media and candidates that
still exceed final admission leave the live messages unchanged. Execution
errors remain errors; the durable tool history and side effects are unchanged.
Economic-only pressure retains its existing ROI policy.

Accepted L2 archives may carry an additive `resumeWitness` in the existing
32 KiB receipt. Continue checks fresh owner-scoped history/revision evidence,
policy, late background receipts and the canonical tool-prefix digest against
that witness before reading the saved `postMessages`. It then restores managed
runtime context through the ordinary composer and replays only uncovered tool
rounds. Full checkpoint records still own display, accounting and execution
recovery. Missing/legacy/unaccepted archives, edits, permission/model changes,
steering, media and storage errors retain full canonical replay. No new store
or transcript authority is introduced.

Resume reads at most eight archive metadata candidates and one matching post
snapshot, bounded by the existing 8 MiB/16,384-message continuation contract.
Digest work is linear in the existing tool prefix; temporary reconstruction is
released after validation. There is no resident pool, queue or extra persisted
message copy. The witness adds roughly 450 bytes to an existing receipt. An
offline synthetic 518-round/2,724,052-character source restored four model
messages in 66 ms with 662,426 additional peak traced bytes on this host;
these are local replay measurements, not provider latency or quality claims.

**Alternatives considered.** Raising the global ceiling would hide estimation
errors and weaken output headroom. Dropping the latest result would discard
unconsumed observations. Replaying and summarizing the entire transcript on
every Continue repeats expensive work; a second resume database would duplicate
authority. Checked reuse of the existing archive avoids those changes.

Verification: `tests/compaction/test_dispatch_context_recovery.py`, calibrated
token/admission tests, archive receipt contracts, Continue wire-parity tests,
and existing compaction pairing/cancellation/model-replay suites.
