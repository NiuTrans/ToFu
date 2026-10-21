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
Ordinary slot-based admission retains its route window; its configured output
cap is resolved against current input before being used as a reservation.

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
allowance. If that preferred allowance does not fit the smallest protected
suffix, the summary transport cap uses the measured remaining room, provided
the existing nominal summary target fits. The preferred allowance alone is
never an irreducible floor. Incomplete provider output is still rejected without text slicing.
An impossible required floor refuses before summary spending; an overfull
completed candidate is rejected before archive/adoption. Deterministic suffix
planning performs no model calls or durable writes. Suffix trials are bounded
by the preservation budget's bit length and use pass-local projections; no
worker or retained cache is added.
**Alternatives rejected.** Repeated summary calls add latency and cost;
post-summary head deletion loses evidence; raising admission ceilings conceals
incorrect accounting. Specs: `test_dispatch_compaction_plan.py` and
`test_compaction_history_wire.py`.

**Problem.** Append-only project-rule updates and consumed agent inboxes can
accumulate inside a single Turn. Keeping every snapshot and notification in
the protected shell produces a false `irreducible_prompt_floor` even after
several successful summaries.
**Decision.** At pressure-L1 and L2 planning/adoption, the composer retires superseded managed
tail sections only when their canonical hashes and a later replacement are
verified. Current sections, mixed-carrier siblings, system policy and authored
text survive; malformed replacements cannot withdraw earlier policy. Restoration
reserves count the same current sections once. Ordinary appends keep their cache
prefix unchanged; rejected/cancelled candidates leave source messages untouched.
The shared pressure pass can admit a snapshot-only reduction without tools or
a paid summary; insufficient or cache-negative candidates remain speculative.
Consumed non-human inbox deliveries join the following assistant/tool execution
unit and reach the summarizer in source order. Unread deliveries and human
steering remain protected. All child invocations, including those without a
conversation ID or durable storage, retain source on summary failure through the
same live-task policy used by L2 and reactive recovery.
**Alternatives rejected.** Keeping all superseded snapshots makes the floor grow
without bound; rewriting every round breaks cache reuse; treating agent evidence
as human instruction creates false authority. Specs:
`test_compaction_accumulated_context.py`, `test_context_snapshot_compaction.py`.

## Output cap versus bound reservation

**Problem.** Ordinary `config.maxTokens` is an output cap subject to later body
clamping, not an already-bound allocation. Subtracting a 128,000-token default
from a 128,000-token route before counting its prompt collapsed the local
ceiling to one token, even for 15,197 message + 13,833 schema = 29,030 input
tokens. The 80,640-token window-safety ceiling was not the defect.

**Decision.** `_admission_budget.resolve_admission_budget` accepts a current
full rendered `input_tokens` count. Ordinary caps use the existing family,
learned model and authorized route output clamps, followed by the canonical
completion-window clamp (10% input margin, 512-token overhead and existing
1,024-token floor). The prompt ceiling is still the smaller of window safety
and window minus resolved completion; economics never raises it. The clamp can
consume validated full-input evidence without message rescanning. Missing or
invalid evidence plans at the safety boundary, never from a stale task receipt;
body construction and final selected-slot recount remain authoritative. No
configuration, registry, dispatch or accounting allocation is written.

For an ordinary 128K cap allowing the full output range, 29,030 current input
resolves to 95,555 completion and a 32,445 prompt ceiling: the input fits with
headroom. Without current evidence, safety-boundary planning retains the
80,640 ceiling. A smaller explicit cap is never raised. Included schemas count
once, in the complete input; they are not a second output reservation.

Native `output_tokens` from the matching owned scope is strict and already
bound: Python never reclamps it to admit a prompt. A native 127,999 reservation
in a 128,000 window therefore correctly leaves a one-token ceiling and refuses
the incident-sized prompt. Native ownership and learned window shrink remain
unchanged. Over-safety input still requires compaction and fail-closed refusal
when the complete required projection cannot fit.

Current-input callers refresh resolution after every changed measured
projection (L1, retention, summary and recomposition), not only at entry. A
failed-summary physical-fit check must use the same resolved output/window
and must not bypass mandatory safety pressure. Receipts distinguish the output
policy, requested and capped output, resolved output, resolution input and their
sources; the archive preserves those fields, including an absent explicit cap
as null. Aggregate reserved headroom includes safety and must not be labelled
as output alone. Dispatch details explain the message/schema split, route
window and headroom; snapshot message estimates remain a separate scope.
These content-free diagnostics never authorize reuse of a historical count.

**Alternatives rejected.** Increasing the ceiling hides accounting defects;
a universal fixed completion cap wastes larger-model capability; treating
ordinary caps as strict native reservations creates false refusals; reclamping
native reservations violates their issued accounting ownership. Reimplementing
the completion margin in compaction would create drifting budget authorities.

Verification: `tests/compaction/test_admission_output_policy.py` exercises real
admission/body construction with controlled complete counter results, initial
and later rounds, changing input, output floors, route/learned caps, native
isolation and strict reservation refusal, and mandatory compaction failure.
These are offline boundary specifications, not live provider acceptance claims.

## AA-cheapest compression-first routing
**Problem.** Parent-provider/family restrictions and mandatory benchmark/profile
coverage exclude usable owner-authorized summarizers, including cross-family
subscription endpoints. AA-first ranking could instead pay more than a usable
cheaper route.
**Decision.** Task-owned summaries default to `aa-cheapest` (`qualified-cheapest`
is a compatibility alias). All compiler-authorized enabled text routes compete,
independently of main provider/family and optional AA/profile coverage. Explicit
`main` opts out; the main task model and configuration never change.

Native candidates come from all enabled authorized chat Offerings in the
same owner/tenant repository, compiled by the existing `RouteCandidateCompiler`.
Discovery is secret-free and never mutates the main model/group. Candidate
discovery uses the shared compiler's demand-sized default, not a retired
request-slot cap; only the chosen witness is minted at dispatch. Dispatch mints
one separately owned candidate (no automatic model fallback), binds stable
provider/access/offering/deployment/connection/credential identity and revision
before resolving plaintext, nests/restores the provider pin, and disposes and
neutralizes credential references after accounting even on cancellation or
`BaseException`. The parent native binding must pass its existing readiness
check before discovery. Full parent witnesses are recompiled against the same
repository revision; their credential kind determines resource policy, not OAuth
transport. An independently authorized mixed-parent group may plan privately only
with complete witnesses; an incompatible issued native binding still refuses.
Legacy callers without usable native authority retain the existing same-pin
API-only AA cost-first policy. Native API mains also admit subscription/resource
alternatives, even if parent pricing/capacity is unknown. Paid-to-paid alternatives
need known Offering prices and strictly lower projected spend than main. Every candidate needs confirmed identity, text
capability and context/output headroom from shared declared/family/learned clamps.
Missing Deployment output limits use the same model/route clamp as transport,
not an invented limit or an unconditional rejection.

The auxiliary transport allowance is bounded by the existing shared output
clamp and remaining learned context, but never below the priced summary target;
complete-output validation still refuses truncation. Early source planning may
reserve the larger allowance conservatively. Legacy API compatibility preserves
its historical default behavior but honors an explicitly configured paid ceiling.
The existing `cached_routing_scores` authority excludes stale, ambiguous and
unmatched score evidence, not otherwise usable routes, without fetching inline.
Missing AA is unknown, not zero;
zero is a valid known score. Ordinal quality/roles/auto-selectability are not
summary-specific AA thresholds. This compression policy does not change their
meaning for other model-selection consumers.

For native routes, evaluate every deployment/credential witness before ranking;
a first unusable or expensive witness must not hide a later valid one. Already-
authorized subscription/local resources rank before known-cost affordable APIs.
Within each class prefer maintained resource-reference cost or paid cost, then
known exact AA descending, declared lightness and stable full route identity.
Missing resource proxies/scores do not block eligibility and are never zero.
Paid-to-paid selection needs strict savings against the best reachable main cost.
Legacy groups still use worst reachable candidate cost and weakest exact AA.
Credential kinds `oauth`, `subscription`, and `local_identity` express resource
access; explicit `subscription` can use an API-key envelope. A plain API key or
provider brand does not prove subscription billing; unpriced API-key routes remain
unqualified until authoritative billing/rate-card metadata exists. No gateway
MSRP fallback establishes actual price. Subscription reference pricing is a
resource proxy, never zero or known marginal cash cost. Confirmed text admission
without optional quality evidence records `routing_admission` with a null score;
complete-output adoption checks still reject partial or unusable summaries.
`compaction.summaryMaxCostUsd` defaults to 0.05; finite nonnegative overrides
bound each paid attempt's estimated maximum quote separately from the shorter
output projection used for ranking/payback; retries retain their existing limits,
so this is not a cumulative bill ceiling. Unknown gateway pricing cannot borrow creator MSRP.
Explicit `main` and `disableModelFallback` prohibit auxiliary compiler/mint work.
Dispatch still revalidates stable identity, quote and resource policy before
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
silent unqualified fallback after the chosen route fails. Authorized paid
subscription alternatives are an explicit policy, not a transport escape hatch.
No live paid-model speed/fidelity result is implied. Specs:
`tests/compaction/test_auxiliary_summary_integration.py`,
`tests/compaction/test_global_auxiliary_candidates.py`,
`tests/compaction/test_auxiliary_offering_cost.py`,
`tests/compaction/test_summary_routing.py`,
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
example-corp/YourProvider) came from spelling these as needles `deepseek-flash` /
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

### Round result evidence

**Final delivery accounting.** Single-result and aggregate fitting render known
host facts before measuring each candidate: canonical execution status, typed
failure advice, existing producer recovery, program-input references and bounded
retention notices. Generic and per-file previews use the same finalization port.
The tool pipeline issues eligible program-input metadata before the first fit;
reprojection preserves it inside the same budget. Each pipeline occurrence
revalidates the advertised handle and withdraws inaccessible cached references.
Plain diagnostics use the available final-view allowance without an intermediate
fixed summary cap. Evidence byte counts follow the delivered artifact alias;
canonical recovery references remain unchanged. Canonical retention occurs
outside the fitting search, at most once per single-result projection, and
does not become a model retry. Failed executions never acquire generic source
recovery merely because their diagnostics overflow. Successful source reads
keep their declared recovery policy.

**Problem.** Adding verdicts or recovery metadata after measuring a preview can
exceed the advertised limit; late unclassified failure stamping can also leave
success-only retry advice in the model text. **Decision.** Measure the final
semantic projection while retaining the original canonical capture, with pure
candidate finalization and explicit persistence outcomes. **Alternatives
rejected.** A fixed safety reserve wastes useful context and cannot bound
variable recovery records. A second truncation pass risks losing original
evidence and repeating storage writes. Irreducible required metadata remains
subject to complete-prompt admission rather than being silently discarded.

Verify `tests/tools/test_final_result_budget_facts.py` and
`tests/tools/test_final_result_budget_backends.py`: exact token boundaries,
failed file identities, original recovery/program references, persistence
failure, real pipeline delivery and original-source readback after SQLite and
TofuDB restart.

**Canonical verdict handoff.** Problem: an already-budgeted envelope could
return its earlier success view after host settlement had rejected, cancelled
or failed the occurrence. Its old artifact could still describe success, and
round reduction could miss a failure carried by an unwrapped canonical result.
Decision: reconcile final canonical facts before single-result and round fast
paths, then fit the corrected view. Preserve producer error classification and
explicit recovery; withdraw program-input advertisements from failed results.
A changed verdict or source coverage republishes an existing canonical reference
once. Publication failure clears the stale advertisement and reports unavailable
retention while the original capture and historical artifact remain intact.
An unchanged envelope keeps its bytes and identity without storage acquisition.
Alternatives rejected: patching only the model message leaves replay evidence
inconsistent; mutating the original manifest rewrites history; synthesizing an
error code or source retry invents recovery policy. Verify
`tests/tools/test_canonical_envelope_verdict.py` and
`tests/tools/test_canonical_envelope_verdict_backends.py`, including failed
publication, actual dispatch/cache recovery and both storage authorities.

**Problem.** A second reduction under aggregate pressure can erase a typed
failure or its compact cause identifiers after single-result budgeting retained
them. Generic overflow advice can also recommend repeating a completed operation,
and replacing small facts with longer recovery hints increases the real budget.

**Decision.** The common result fitter counts typed error metadata and prioritizes
its compact items ahead of optional diagnostic prose. Aggregate reduction keeps
the original error code, retryability, advice, canonical result and explicit
producer recovery. It never synthesizes a source retry for a failed execution;
unclassified host failures retain their error status without an invented code.
Missing retained bytes call for inspecting the prior outcome; declared source
recovery for successful observations keeps its existing narrowing rules.
Only a strictly smaller final model projection is adopted, with program metadata
and execution/retention fields included during fitting. Irreducible facts remain subject to
complete-prompt admission. Mandatory retention still refuses lossy replacement.

**Alternatives rejected.** Raising the round limit hides the loss without
repairing it; dropping failure identities makes unchanged failures appear
recoverable; replaying the source can repeat effects and cannot recover historical
evidence. Application JSON never gains authority from resembling an error record.

Verify `tests/tools/test_aggregate_result_recovery.py`, the single-result typed
error budgets, and `tests/tasks_pkg/test_gateway_failure_recovery.py`. The latter
drives the real outer tool pipeline and shared root loop under aggregate pressure,
including changed arguments, successful correction and unchanged-failure stopping.

**Native artifact failure projection.** Problem: nested native query errors lost
their explicit retry policy when the shared delivery path wrapped them as generic
structured results. Decision: the artifact producer uses the existing typed error
envelope for single failures and the shared all-nonretryable batch classifier,
while the same canonical capture retains exact native query JSON and media for
program decoding and storage. Mixed-success or retryable batches retain their
per-item outcomes without claiming all-terminal failure. The delivery owner
projects owned envelopes in both legacy and v2 wrapping modes; source documents
that merely resemble envelopes remain ordinary data. Hiding internal transport
fields is not source compaction. Native artifact budget fitting counts
the final error projection, including cause identifiers. The declared batch
argument fields in `tool_result_surfaces_v1.json` bind those causes to complete
requests; changed causes or arguments rearm recovery. A whole-batch metadata
refusal has no success-item list and remains a classified error during media
projection. Alternatives rejected: interpreting arbitrary nested JSON as authority
misclassifies successful documents; replacing decoded query JSON with a display
wrapper breaks program callers. Verify artifact-failure-policy/recovery, decoded
results, model budgets, and gateway failure recovery.
