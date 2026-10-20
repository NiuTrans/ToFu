# Research cycle

## Ownership

The existing five-stage production recipe remains the only runtime. Evaluation
contains a checkpointed critique/revision/re-review cycle. Contracts live in
`lib/research/contracts.py`, `lib/research/revision.py`, paper resource constants,
and `contracts/research_program_v1.schema.json`. Persistence remains owner-scoped
research artifacts and the existing Research Foundry workspace, not a new store.

## Revision

`idea_id` and `idea_version` identify candidate lineage. At most two revisions
and 24 issues are admitted. Issue overflow stops explicitly, preserving the
review rather than silently dropping objections. Evidenced medium/high
retrieval, survey and ideation objections trigger checkpointed targeted harvest,
survey rebuild, dependency revalidation and ordinary novelty/rubric gates.
Parser/infrastructure objections remain unresolved work. Explicitly closed gaps
retire linked candidates; missing IDs cannot retire unlinked ideas. Publication
uses the revised survey/corpus and candidate state, not the original artifact.

Silence is not resolution. Explicit resolution requires evidence agreement from
at least two valid judges. This is model review, not scientific proof or enforced
model diversity. No change, forced-final generation, errors and budget exhaustion
stop revisions. Original snapshots, target versions, issue IDs, retirement,
usage and stage checkpoints survive reload. Publication retries do not repeat
completed model phases. Upstream snapshot changes invalidate revision caches.

Ideation repairs only unambiguous metadata. Excluded papers cannot be direct
novelty neighbors; cross-domain inspiration remains separate. Ambiguous semantic
relinking is not guessed.

## Corpus and context budgets

Default target is 60 papers, with a 200-record ceiling including revision
admission. `discovery.py` and `literature.py` combine arXiv, Semantic Scholar and
CrossRef metadata, canonical DOI/arXiv identities and real directed citation
edges. Limits: six keyword rounds, 40 rows/call, eight graph calls, two hops,
18 academic HTTP requests, 1 MiB/response and 64 status receipts. Cancellation,
source failures, deduplication and stop reasons are explicit. DOI-only records
remain metadata-only: existing harvest does not pretend to read them.

Harvest reuses 40-paper batches and the existing parser lease. Survey reads at
most 200 × 6,000 stored original parsed characters (1.2 million total), never
substituting generated reports for original evidence. Every admitted nonempty
paper receives bounded excerpts, then map/reduce synthesis. This is all-paper
**excerpt coverage**, not full-text coverage. Coverage records failed/unread
papers; partial maps cannot report complete coverage. Prompts remain bounded by
40,000 characters, and the stage reports its spend instead of being capped by
a stage-level token/dispatch envelope. Task-local source/model/
owner/version-keyed caches are reconstructible, not a new library authority.

Each hierarchical survey call admits one initial hard-error attempt through the shared dispatcher (`max_retries` counts total attempts, not extra retries), with explicit owner identity, cancellation and the finite production 429 allowance. Survey maps have no stage-level token or dispatch envelope: every call is accounted in the shared usage snapshot and the run ends on the no-progress breaker, cancellation, or an exhausted caller-declared `logical_budget`. Withdrawing tools at a stage envelope was rejected because the resulting tool-less round let the model print a tool call as prose, which then reads as a finished survey. All-map failure retains bounded per-batch causes and paper identities instead of replacing them with an opaque empty-survey error. Total map failure cannot publish a survey; its structured batch causes and paper IDs persist before the stage error. Partial maps may produce explicitly incomplete excerpt coverage with uncertain gaps, never complete coverage or proof that an unvisited paper leaves a gap open. Evidence cards retain exact character offsets, quotes, source version/hash and snapshot SHA-256. `entailment.py` checks quote identity deterministically, then independently assesses each scoped gap against bounded evidence batches. Supported competing evidence can close a gap. Semantic verdicts remain explicitly `model_assessed`; invalid or unvisited evidence leaves uncertainty. Neither citation existence nor retrieval absence proves support or novelty.

## Experiment and manuscript validity

Runs separate execution state from scientific outcome. API submission success,
pending/missing/failed receipts and ordinary tool success cannot manufacture
scientific support. Current hypothesis/protocol and evidence digests invalidate
stale claims. Manuscript scientific snapshots must remain current independently
of compilation byte digests.

Saved manuscript sources compile through the shared paper-build scheduler.
Only server-verified owner/source/artifact evidence can write a passing
compilation result; workspace PUT and model/tool output cannot supply it.
The artifact lease spans the workspace CAS, and stale/cancelled commits fail
without replacing newer work. Legacy tool-based success needs recompilation.
See [paper builds](PAPER_BUILDS.md#research-workspace-compilation) for cache and
artifact-retention boundaries.

Protocols include competing hypotheses and minimal discriminating experiments
with decision rules, expected information/value and explicit costs. Current run
receipts derive `retain`, `refine`, `refute` or `inconclusive` decisions and
prioritized next-action proposals. These proposals require confirmation; they
are not autonomous compute authorization. Scoped negative claims can be valid
outputs of refuted hypotheses. Structured evidence-backed baseline fairness,
uncertainty, seeds, validation split and claim traceability gate manuscripts;
mentioning these terms in prose does not satisfy the audit.

Problem: executor-authored `status=verified` and a result reference could satisfy all four checks without computable measurements. Decision: the server verifies `scientific_observations` against the retained `scientificObservations` schema. Named baseline, dataset/metric, dataset and split fingerprints, evaluation on the test partition, and positive matched budget quantities must agree. Direct audit also enforces the canonical seed-population bounds before allocating paired statistics. One lazy process-lifetime schema template supplies those bounds; public contract reads return isolated copies and cannot mutate the cached authority. Protocol-specific projections derive const constraints for frozen dataset/split digests, matched budget/unit and each side’s generation digest; unrelated contracts stay unconstrained. This gives producers immediate schema feedback while independent observation verification remains authoritative. Frozen seed membership, uniqueness and vector lengths are also projected into both observation formats; v2 binds each system’s seed list separately while allowing any ordering. The shared base contract stays unchanged. An explicit frozen protocol observation_format=tofu.scientific-observations/v2 requires identified pairing in both producer schema and independent audit; normalization, protocol digest and idempotency preserve that requirement. Omitted fields retain legacy admission; existing protocols are not upgraded. New default independent-validation steps persist required_observation_format=v2; preregistration derives that requirement into the frozen protocol and launch rechecks it. Saved steps without the field remain unchanged, and validation_context exposes the requirement to the executor. Successful required-v2 validation archives also bind the stored step, launch payload and run to the exact protocol, required outcome, command-execution policy and acceptance list. Drift refuses before archive or progress; ordinary run adjudication remains the verdict authority. Candidate-renewal tests run on both legacy and required-v2 parents: reviewed successors inherit the format policy, preregistration restores it even when the proposal omits the field, and original failures/protocols remain immutable. Exploratory drafts without a numeric decision rule retain the generic seed contract when their tentative seed list is incomplete; only frozen scientific protocols refuse invalid seed populations at projection. A cold Python 3.12 tracemalloc probe retained one entry: 3,651 serialized JSON bytes, 21,072 traced retained bytes and 168,058 peak bytes while loading the containing contract; these are Python allocations, not process RSS. No worker or persistent record is added. The retained offline benchmark is `python -m benchmarks.research_observation_contract --output /tmp/contract-benchmark.json`. It compares the same production projector with fresh versus cached schema loading, counterbalances seven pairs of 200 calls and records source/contract/protocol fingerprints. A retained-harness run measured median 68.4 ms versus 22.7 ms (3.02×); the earlier temporary probe measured 5.58×, demonstrating host/filesystem variation. These results concern warm-filesystem projection cost only, not application throughput or research quality. Benchmark harness v2 also records the shared launch resource probe’s CPU/affinity/cgroup, memory/headroom and volume snapshot; unavailable values stay null. It reuses the cached public probe before timing and adds no sampler or thread. Machine configuration is observable but does not make runs on different loads directly comparable. Harness v3 separately times the first projection after clearing its Python schema cache, then runs the existing warm paired comparison. This measures lazy schema-load plus projection cost, excluding module imports and uncontrolled filesystem-cache state; it is not an application-startup measurement. One v3 run recorded 0.72 ms for that first projection and 68.0 versus 22.6 ms per 200 warm calls (3.02×); all projected contracts matched. Tests verify equivalence and restoration after injected failure. Seed coverage matches the frozen owner's distinct planned seeds (at least two); paired finite values independently determine mean difference and sample standard error. Observation v2 requires method_seeds and baseline_seeds alongside their value vectors. Audit v5 verifies distinct identical seed populations and pairs by identity before computing statistics, allowing independent ordering while refusing missing, duplicate or foreign identities. V1 keeps its shared-order interpretation and cannot claim independently verified pairing; it cannot carry v2 identity fields. Reported identities still do not prove physical execution or authentic seed provenance. Audit v6 uses one strict scientific JSON decoder across intake and reduction: duplicate keys are refused at every nesting level. Decoder nesting exhaustion becomes the same typed invalid-input refusal, so malformed originals cannot escape receipt reduction as an uncaught recursion failure. Ambiguous originals carrying scientific markers invalidate the reduction even if a later receipt is valid; last-key-wins parsing was rejected because it silently chooses between contradictory claims. Old aggregates must replay their immutable receipts under the new audit version. Audit v7 also retains arrays or wrapped objects containing scientific markers as invalid evidence instead of silently ignoring their malformed envelope; scientific data must occur at the canonical top-level object. This aligns reduction with native scientific capture and prevents later receipts from masking that failure. Separate identities were chosen over assuming matching array order. Wrong/zero uncertainty claims, incomplete seed coverage, nonfinite/boolean data, partition overlap, gaps and tuning-partition evaluation do not pass. Half-open partition intervals cover the complete declared dataset without enumerating rows. Successful, digest-backed receipt references remain mandatory. The executor's original statements stay unchanged; their verification labels have no authority.

The audit returns `verification_basis=reported_observation_consistency/v1`.
This proves consistency of reported measurements and partition assignments;
it does not independently establish artifact contents, benchmark implementation,
data authenticity, significance, or experimental reproduction. Those remain
separate acceptance requirements. Model context receives the same retained
machine contract and requires measurements in observed tool output, not a final
model proposal. Current receipts retain a 4,000-character excerpt bound;
incomplete large evidence cannot be treated as a complete verification input.

Problem: actual command results have an executor command header and terminal exit trailer, so parsing the whole result as JSON loses otherwise valid measurements. Decision: command receipts decode one complete JSON result body inside the existing producer envelope, using the declared command's display header (including multiline commands), the anchored terminal exit, and the separate stderr section. Zero exit is required; nonzero, timeout, abort and interrupt results cannot provide passing evidence, and background handoffs remain pending. The complete raw envelope still determines the result digest and retained excerpt, preserving provenance and identity. Parsing arbitrary JSON snippets or repairing logs into measurements was rejected because either can select incomplete or contradictory evidence. Raw JSON provider results retain their existing path. This verifies reported observation consistency, not benchmark authenticity or independent reproduction. Old receipts are not rewritten or silently recertified. The formatter can merge stderr-only output into its result body, so this does not certify stream provenance; producers should emit the measurement object on stdout and diagnostic logs on stderr.

Problem: result presentation and aggregate model budgets can replace complete tool text with a preview before scientific receipt creation. Decision: execution captures the complete-text digest and structured scientific payload before that shared presentation boundary. The optional server-owned `observation_excerpt` retains only observations, audit links and reported outcome as complete compact JSON of at most 4,000 characters; oversize measurements are omitted rather than truncated or promoted. Normalization, immutable completed-run guards and paged archives preserve this payload. Verification and outcome extraction prefer the captured payload, while ordinary logs remain a bounded display excerpt. Original structured tool errors determine failure even when located after the preview. Later artifact descriptors may update the reference without rewriting evidence. Relying on rereading temporary log artifacts was rejected because expiration would make durable science depend on cache retention. Reexecuting to recover missing measurements was rejected because it creates a new observation. Old receipts are not retroactively repaired; source/benchmark authenticity and independent reproduction remain unproven. The workspace and archive admission budgets remain unchanged. One synthetic 1,049,972-character JSON result captured a 1,296-character payload in 3.17 ms, with 1,194,008 traced peak bytes; full JSON parsing uses memory proportional to the supplied tool result and does not retain its log copy or add resident workers/caches.

Problem: hashing only the excerpt aliases distinct large results whose first
4,000 characters match, silently collapsing completed observations during replay.
Decision: new receipt identities hash the complete observed tool text in bounded
UTF-8 chunks; the retained excerpt stays bounded. Different tails therefore
retain distinct observations even for reused task/run IDs. Existing historical
receipts remain unchanged; missing historical tails cannot be reconstructed.
Increasing every retained excerpt was rejected because it grows workspace and
model context with logs. This fingerprints observed text, not an unobserved
external artifact or the correctness of the experiment. A synthetic 6,291,456-
character Unicode result took 27 ms to fingerprint with 546,906 traced peak
bytes beyond the existing input; chunking avoids another full encoded copy.

Problem: cross-receipt unions can combine fair-budget measurements with unrelated
valid statistics even though neither observation passes every check. Decision:
each run requires one complete observation and linked evidence for all four
checks in the same receipt. Separate partial reports may remain visible, but
cannot collectively establish a coherent comparison. Combining receipts without
a shared immutable experiment identity was rejected because it silently joins
incompatible evidence. A complete later receipt can still establish the result.

Problem: a retained run could change an explicit tool outcome from refuted to supported while keeping valid measurements and evidence references. Decision: all completed, digest-backed receipts with an explicit outcome must agree with the retained conclusion; mixed outcomes require an inconclusive conclusion. Unknown explicit labels fail closed. A mismatch blocks the four manuscript checks and contributes an inconclusive observation to decisions and archive aggregates. Current receipt-backed inconclusive evidence contests claims and binds the manuscript snapshot, including uncited observations and archive relocation, so rewriting prose cannot hide an unresolved comparison. Pending/error receipts do not replace completed observations. Alternatives: trusting the run label or selecting one favorable receipt loses counterevidence. Outcome-free legacy receipts retain existing behavior and gain no new proof. This binds reported conclusions, not numeric metric direction or an owner-defined success threshold; those cannot be inferred from metric names.

Problem: a consistent paired measurement can still self-report support when its effect violates the intended success criterion. Decision: protocols may freeze `decision_rule` with exact `direction` (`higher`/`lower`), finite `minimum_effect`, and a positive finite `uncertainty_multiplier`. The shared schema owns this contract. The server recomputes paired mean and SE (overflowing intermediate sums or deviations use scaled finite arithmetic; ordinary values keep their existing calculation, while nonfinite observations still refuse), orients the effect, and supports only when effect minus the frozen SE margin exceeds the threshold; it refutes only when effect plus that margin is below it. Equality, crossing margins, overflow, missing measurements and contradictory labels are inconclusive. Negative thresholds explicitly permit noninferiority. A frozen SE margin is not a confidence interval or significance test. SE uses a scaled Euclidean norm rather than squaring tiny deviations to zero. Reported statistics must agree within relative tolerance; a fixed unit-dependent absolute tolerance cannot erase a tiny mean or SE. Invalid rules are rejected, never silently dropped. Rules bind the protocol hash, so changing a threshold stales old evidence instead of retrospectively relabeling it. Decisions, claim validity, manuscript snapshots/audits and archive aggregates use the same rule. Alternatives: inferring direction from metric names or selecting thresholds after observing results allows favorable reinterpretation. Rule-free legacy protocols preserve their existing consistency checks but do not establish numerical decision-rule verification. The protocol API and workbench expose this explicit policy. The editor starts blank for legacy protocols, never guesses a direction or threshold, rejects partial or nonfinite rules, and requires a complete rule before interactive experiment confirmation. Saving changes the protocol revision before execution. A synthetic 32-run corpus with this policy, audited 100 times with allocation tracing, averaged 16.41 ms per audit and peaked at 131,333 traced Python bytes; no resident workers, caches or persistent records are added.

Problem: interface validation alone leaves API and direct worker dispatch free to run an experiment with no numerical criterion. Decision: new experiment actions validate the saved rule at task admission, before allocating a task or worker, and again on the exact revision before history preparation, tool epoch construction or model execution. Queued edits therefore cannot change the frozen criterion. Missing/invalid rules produce a visible error without inventing values; stale revisions and owner cancellation keep their existing behavior. Acknowledged dedup retries return their original task without another dispatch. Legacy workspaces/observations remain readable and other actions remain available; launching another experiment requires a new frozen rule. The existing single-call draft exposes the same schema to propose grounded values or omit unknown choices, never creating authority by itself. Alternatives: browser-only checks can be bypassed; runtime-only checks waste task allocation; guessing a rule lets observed results dictate a favorable threshold. Generic Evolution command stages are not converted into Research actions by this boundary.

Archive audit versions 0, 1, 2 and 3 fail closed under verifier version 4. The next workspace save replays committed pages under the current verifier and publishes the recomputed aggregate through CAS without changing original pages or dropping old counterevidence. Verifier version also binds the scientific manuscript snapshot, so old status-only manuscripts cannot remain ready or published-current after the policy changes. Alternatives: another model's confidence label was rejected because it does not recompute measurements; trusting old cached success was rejected because it bypasses the upgraded checks. No model, worker, queue, store or resident cache is added. Baseline verifier v3 on a 32-run synthetic corpus audited 100 times with allocation tracing averaged 10.29 ms per audit and peaked at 13,502 traced Python bytes. This measures consistency checking, not experimental throughput.

Problem: a claim could select only supporting receipts while the same frozen hypothesis/protocol also had current decisive refutations (or vice versa). Decision: conflicting completed, receipt-backed outcomes contest claims and block manuscript readiness even when each individual audit is verified. The scientific snapshot binds uncited decisive counterevidence too, so an older manuscript cannot remain current; regenerating text cannot resolve the contradiction. Stale, incomplete, unreceipted and inconclusive runs never invent a decisive conflict. Consistent refutations still admit scoped negative-result manuscripts. Selecting only the claim's cited outcomes was rejected because it permits cherry-picked science; resolve the tested scope through the existing protocol/review path instead.

Completed observations bind the executing task, frozen dependencies and canonical result receipts. Reusing a model-proposed run ID with different completed execution/evidence retains a separate server-derived ID; incoming claim aliases bind that new result. Identical reapplication ignores delivery timestamps and preserves the completed observation and its scientific snapshot. Missing model IDs derive from the task rather than the current clock. Pending rows retain their existing settlement progression. Over-bound, duplicate-ID or malformed run collections fail explicitly before merging/review; earlier observations are never silently replaced or truncated. Problem: direct workspace saves could still erase observations retained by the experiment action. Decision: the application save boundary rejects deletion or any canonical-field edit of a completed observation, including legacy completed rows. Protocol/hypothesis edits still recompute validity; pending runs can settle. Alternatives: silently reattaching deleted rows was rejected because it obscures rejected edits, and action-only protection leaves the workspace PUT bypass open. Replacing completed rows by model ID was rejected because it erased refutations.
### Long-horizon observation history

Problem: the 32-run/640-KiB working document eventually stopped valid experiments.
Decision: `history.py` prepares immutable, content-addressed pages in the selected
record authority (`research.observations.v1`), with owner/direction/language keys.
Only the workspace revision CAS publishes a head. Ambiguous page acknowledgements
need exact immutable readback; an ambiguous workspace acknowledgement needs the
exact document and next revision. Competing CAS failures cannot publish their
pages. Prepared observations remain durable for identical recovery; they are not
accepted evidence or automatically deleted cache data. No other store starts.

Uncited completed rows move to pages at 24 working rows (target 16), or earlier
under byte pressure. Pages hold at most 16 observations and 640 KiB. In-flight and
explicitly cited evidence stays in the working basis; its 32-row/document bound
remains. A fully cited completed basis refuses new experiment dispatch before an
external effect; analysis can revise that basis. Pending experiments can settle. Legacy full
workspaces compact before the next experiment, using the new server revision.
Stop after preparation prevents model entry without erasing prior observations.

Server-owned scope aggregates retain supported/refuted/inconclusive outcomes and
missing audit checks. Historical refutations contest claims, and old audit gaps
still block manuscripts. Changing scope replays verified pages with bounded
memory; normal saves update only the current aggregate. The 256-bit multiset
commitment plus count preserves scientific snapshots across archival movement.
Archive counts are receipt observations, not proof of independent replications.
Read `GET /api/v4/research/workspace?direction=...&history_cursor=0`, then use
`history_page.next_cursor` for older pages. Each page carries its committed
workspace revision and page hash; run IDs are scoped to that page. Cursors must
match reachable page boundaries, so orphan or foreign-owner pages are excluded.

Alternatives: raising the document ceiling was rejected because it couples
history to model context and resident memory; scanning all history on each save
was rejected because cost grows with lifetime. Selecting only the working window
was rejected because it hides falsifiers and failed audits. Old-page membership
checks currently traverse the head chain; a future index must preserve that
committed-lineage proof and the same owner/deletion authority.

Operations measurement (synthetic receipts, 192 observations, Python allocation
tracing enabled): both SQLite and native TofuDB retained 16 working rows and 176
archived rows in 22 pages. Workspace JSON was 20,894 bytes, archive JSON 196,544.
Median saves were 39.0/59.3 ms; the last 16 medians were 39.0/58.8 ms. Latest pages
were 5.2/5.5 ms, oldest pages 23.1/23.4 ms. Traced Python peak allocations stayed
below 0.73 MiB. No worker, resident cache, queue or pool was added. Durable growth
follows retained user observations; preparation replays reuse identical pages.
These are storage/projection measurements, not real experimental throughput.

The experiment workspace exposes historical observations through a demand-loaded,
read-only browser. It retains one page (at most 16 rows), preserves the visible
page when a read fails, and retries only on explicit request. Direction, language,
workspace revision or disposal aborts the old transport and fences late replies.
Browsing never applies the returned workspace to editors or restores invalidated
submission gates. Evidence disclosures remain open across ordinary telemetry
repaints. The server continues to own scientific decisions and scope relevance.

Action context removes historical receipt prose but retains evidence references.
Input allowance follows the model window minus output reservation and a 2,048-token
safety reserve; explicit input limits remain. Output ceiling is 8k; unknown model
windows use 32k.
Oversized exact sources fail visibly instead of truncating. Existing confirmation
boundaries for experiments, analysis, compilation and publication remain intact.

## Workbench progress

`contracts/research_progress_v1.schema.json` owns the additive
`research_progress` projection carried by the existing research task events.
`scripts/generate_research_progress.py` generates TypeScript types and runtime
validation from that same schema. Projections carry a run ID, monotonic
revision, business-progress clock and bounded blocking reason; heartbeat/ping
observations cannot advance the revision or business clock.
Execution settlement remains generic task status; connection/reconnect state is
client-owned and quality is separate. A running projection is not permission to
revive a terminal task. Cancelled/terminal research callbacks are suppressed;
publication checks cancellation before each owner-scoped repository write.

Search-round receipts report actual retrieved/new identities and bounded unique
discovery counts. Per-paper receipts follow harvest persistence; admitted,
parsed, cache-hit and failed counts stay distinct. Stage done/recovery receipts
are emitted only after checkpoint acknowledgement. Candidate counts come from
checkpointed ideation; published-artifact readiness follows the publish stage,
not streamed model prose. Projection counts cap at 200, with unknown excerpt
coverage and candidate totals represented as null, never fabricated percentages.
Progress reuses the bounded generic task replay buffer; it is not a new durable
event journal. Recovery reconstructs stage summaries from existing checkpoints.

Survey/evaluation currently expose stage/tool activity, not a complete semantic
per-paper reading or per-judge review timeline. Excerpt coverage remains unknown
in the workbench projection until an owning coverage receipt is integrated.
Checkpoint durability and repository publication are distinct: a survey stage
checkpoint alone does not advertise a repository-ready survey artifact.

## Durable run records and reopening

`contracts/research_record_v1.schema.json` owns run snapshots. The repository `lib/research/records.py` writes through existing Sidecar `record.*` operations in owner-prefixed `research.runs.v1` / `research.run_index.v1` namespaces; no parallel local database is opened. Start acknowledgement requires a durable record before spawning. Snapshot/index writes require exact readback; response-shape-only ACK was rejected. Lost responses recover only identical content (1 MiB write+confirm medians: SQLite 15.85 ms/native 116.36 ms, five private samples). Business receipts (stage boundaries and individual harvest results) replace the snapshot before live publication. Write refusal stops execution with an error, never clean success. If terminal persistence also fails, the last committed snapshot remains recoverable as interrupted. When a progress/receipt write is refused mid-run, the run aborts and the terminal record names the persistence failure itself (`error_context: research:persistence`) rather than whichever stage observed the abort flag first; storage write-refusal envelopes classify by the stable StorageError code (`database_busy`/`database_unavailable` → retryable `server_busy`, `database_timeout` → retryable `timeout`), never as an upstream LLM failure.

Snapshots retain up to 200 captured paper metadata rows, bounded titles/errors, progress and acknowledged pipeline checkpoints; each record caps at 8 MiB. Token deltas and heartbeats do not cause durable writes. The compact index is separate from checkpoint bodies and lists at most 200 newest-created runs. Index and snapshot commits are sequential, not atomic; an index-write failure can leave the snapshot addressable by task ID before it appears in the list. Run records have no TTL; task replay and local workdirs remain separate runtime resources. Original paper content stays in the owner-scoped paper library. Research started as an Evolution run preserves its immutable `evolution_run_id` in task metadata, record and compact index; the private request and local manifest retain the full server binding. Distinct runs cannot deduplicate onto each other's same-direction worker. The workbench retains the returned run ID and restores it from exact task/record projections; Stop uses the owning run's cancellation API. Missing bound cancellation capability fails visibly instead of using a bare-task fallback. Legacy genuinely unbound tasks retain generic cancellation. After a terminal research record commits, the worker invokes the existing Evolution terminal receiver. Callback failure leaves durable evidence for the existing sweep, which also consumes cancelled runs with unsettled episodes.

Reopening a record checks the live registry. An absent pending/running task is `interrupted`, not running, and exposes explicit resume. Resume restores the acknowledged pipeline checkpoint into the existing production workdir and uses the same recipe; an incomplete stage may repeat and reuse paper cache. A still-registered terminal task is not revived. Explicit record recovery and the existing startup manifest sweep verify the same owner, research run, task and plan before admission, after carrier birth and before recipe execution; cancelled, settled, missing or mismatched owning runs cannot execute a recipe. To avoid lost stages or stale-manifest replay, the sweep restores acknowledged snapshots and rejects terminal records; local-only recovery was rejected. Records alone do not promise restart auto-execution. Snapshot reads never dispatch work. Revision substage scratch files are not included in the pipeline snapshot; losing those files may repeat unfinished revision work. Generic task events remain bounded live replay. Completed artifacts remain on the existing lookup endpoint, and run detail retains its own `result` so repeated directions cannot mix outputs.

## Workbench presentation

Discovery and parser receipts render in a compact, keyed table of at most 200
papers: title/reader link, venue or conference, year, citation count and processing
state. Bibliometrics come from provider metadata, never model guesses; zero is a
known value and unavailable fields stay explicitly unknown. Year and citation
sorting each support both directions, with unknowns last. Sort choice, focus,
scrolling and open disclosures survive telemetry updates.

`literature_discovered` carries bounded `papers`, `citation_edges` (320) and
`source_statuses` (64) before harvest. Sparse parser receipts merge without
erasing canonical IDs or metadata. Saved runs retain those same fields. Historical
reads recover only existing acknowledged checkpoint evidence, without provider
calls or guessed bibliometrics; missing historical metadata remains unavailable.
The optional SVG lineage places connected papers by recorded year and draws only
saved `cites` edges, directed citing → cited. At most 40 deduplicated in-library
edges (80 nodes) render, with scrollable bounds, explicit coverage and an
accessible relationship list. Dates and semantic similarity never create links.

History reopens by task identity and its frozen result, never by the latest
artifact for the same direction. Hydration is single-flight; a missing run record
does not authorize direction-level fallback, and a live legacy task cannot
borrow an older result. Failed records preserve a bounded error and failed stage;
missing verdicts are distinct from a completed gate accepting zero candidates.
Interrupted runs expose explicit resume; failed/stopped runs offer a new-run
retry of the same direction, never an automatic paid retry. The browser retains
one existing task subscription and no local persistence store.

## Verification

`tests/research/test_research_*.py`, `tests/paper/test_paper_survey*.py`,
`tests/paper/test_paper_ideate*.py`, and `tests/paper/test_paper_harvest.py` specify offline
execution, resume/cancellation, storage/usage, evidence bounds and stale science.
`quality_comparison.compare_quality` accepts at most 100 independently audited
matched run pairs with frozen task/corpus/model/budget identities. It excludes
missing metrics, self-scores and unmatched budgets; reports descriptive paired
differences only. References still require external audit. No live-model quality,
statistical significance, reproduction success or conference acceptance is
inferred from passing software tests.

Real Chromium specifications in `tests/research/test_research_runtime_browser.py` and
`tests/research/test_research_reading_browser.py` cover synthetic transport failure,
Stop rejection, late snapshots, long Chinese headings, narrow screens, keyboard
focus and 200-paper reading continuity. These do not establish external-provider
long-task reliability or an end-to-end performance baseline.

### Frozen measurement scope

Problem: matching method/baseline reports could silently change their shared
dataset, split or resource budget after protocol registration. Decision: the
canonical protocol accepts an explicit `measurement_scope` with dataset/split
SHA-256 identities, budget unit and positive per-system budget. Normalization
preserves it; numerical audit requires both systems to match every frozen value.
Evolution preregistration binds it under the existing immutable program CAS and
protocol digest. Invalid scope refuses admission. Legacy protocols without scope
retain consistency checks but establish no scope precommitment. Alternatives:
trust matching reports or infer digests from dataset names; neither prevents
post-registration substitution. Scope is reported provenance, not dataset
authenticity or independent reproduction. Four scalars add no workers or I/O.

Frozen system generations
-------------------------

Problem: matched data and budgets could still compare substituted system versions.
Decision: optional `measurement_scope.candidate_digest` and `baseline_digest`
freeze both inspected source/configuration generation identities together. Each
reported method/baseline `system_digest` must match its own frozen identity.
Partial pairs, malformed digests, missing reports and swapped generations refuse
numerical acceptance. Legacy protocols remain readable with no inferred version
precommitment. This proves reported identity consistency, not actual execution
of those bytes; independent reproduction and integrity still require evidence.
Label-only matching was rejected because a name can hide changed implementation.

Exact numeric seed admission
---------------------------

Problem: seed normalization silently rounded floats, parsed strings, discarded
booleans and truncated lists, changing a submitted experiment plan. Decision:
protocols carrying a numeric decision rule accept only an exact integer list
of at most 32 entries; malformed/over-limit submissions refuse before executor
allocation. Every accepted seed and its order are retained. Existing unruled
drafts keep their legacy projection; missing seeds stay a readiness gap. Silent
repair was rejected because precommitment must preserve the submitted plan.

Paired-seed execution admission
------------------------------

Problem: new experiments with a numeric rule but fewer than two distinct seeds
could spend resources before their observations necessarily failed numerical
audit. Decision: the API and worker share a 2..32 distinct integer seed admission
check before allocation/model/archive effects. Worker rechecking covers changed
queued inputs; cancellation keeps precedence. Drafts and existing observations
remain readable. Delaying rejection until result review was rejected because
the submitted plan cannot produce an accepted paired uncertainty estimate.

Measurement context execution admission
--------------------------------------

Problem: a numeric rule and paired seeds could admit an experiment whose absent
dataset, primary metric or baseline made numerical acceptance impossible.
Decision: the shared API/worker admission checks all three as nonblank strings
and reports missing fields before allocation, model calls or history preparation.
Queued inputs are rechecked; cancellation keeps precedence and drafts/history
remain readable. These are declared comparison identities, not verified dataset
availability or baseline competence. Post-execution rejection was rejected as
unnecessary spending on a protocol that cannot satisfy its current audit.

Worker protocol mutation fence
------------------------------

Problem: the admitted protocol is also held in a mutable workspace passed to
execution adapters. An accidental mutation could judge observed results against
changed thresholds, seeds or comparison identities. Decision: freeze the exact
portable JSON protocol identity at worker admission and recheck after epoch preparation
and after execution, before result application. Changes fail explicitly; a new
protocol requires a new confirmed workspace revision. Cancellation still wins
before result application. Canonical JSON distinguishes false from numeric zero
where Python equality does not. Revision CAS still fences concurrent workspace
writes; it cannot detect local object mutation, so relying on CAS alone was rejected.
This fence grants neither replay nor scientific authenticity.

Result protocol publication fence
---------------------------------

Problem: checking execution input alone does not bind the workspace returned by
result application. Decision: before the final workspace CAS, experimental
results must retain the exact worker-admitted protocol identity. Changed, missing
or type-substituted protocols refuse publication; cancellation is checked first.
An unchanged result keeps the original owner and revision CAS. This supplements
execution mutation checks and does not alter scientific audit or grant replay.
Trusting result application implicitly was rejected because that would leave
the final publication boundary outside the frozen experiment contract.

Experiment hypothesis binding
-----------------------------

Problem: run dependency digests bind the central hypothesis as well as protocol,
but a mutable execution/result workspace could substitute that hypothesis.
Decision: freeze its exact portable JSON identity at worker admission and check
after epoch preparation, after execution and before final publication. Changed
or removed hypotheses fail explicitly; protocol diagnostics and cancellation
precedence remain. Missing legacy hypotheses are not invented or promoted into
validated claims. Legitimate changes use a new confirmed workspace revision and
existing dependency invalidation. Checking only protocol was rejected because
it would attach earlier observations to a different post-measurement claim.

Frozen observation prompt contract
----------------------------------

Problem: experiment instructions requested a revised protocol while execution
kept it frozen; generic observation schemas left generation reports optional
even for version-bound protocols. Decision: experiment output prompts request
results without protocol revisions, and shared observation-contract projections
require both system_digest fields when both generations were frozen. Research
and Evolution use that projection. Base/legacy schemas remain unchanged; prompt
construction does not mutate workspace facts. Executed generation identities
must be inspected, not copied from expected digests. This clarifies output
requirements, not artifact authenticity. Keeping contradictory prose or duplicating
separate schemas was rejected.

Named comparison declarations
-----------------------------

Problem: whitespace-only candidate names and matched budget units could pass
reported comparison checks. Decision: numerical validation requires nonblank
names/units and the canonical observation schema requires a non-whitespace
character in both systems' declarations. Empty, ASCII-whitespace and Unicode
space-only reports remain unverified and cannot admit independent review.
Strings are checked, not rewritten; scope/digest matching retains exact identity.
This establishes declared context, not baseline competence or execution integrity.
Truthiness/length checks alone were rejected because spaces satisfy both.

Research-quality comparison uncertainty
--------------------------------------

Problem: descriptive paired means alone hide sparse or unstable task-level
evidence and exclusion coverage. Decision: quality comparison reports submitted,
valid and excluded pair counts plus each metric's mean, minimum, maximum and
sample standard error of paired differences. Fewer than two valid pairs yields
unknown standard error, never zero; excluded pairs contribute no values and
remain visible. Inputs are unchanged. These summarize supplied externally-audited
receipts; reference authenticity, pair independence and representativeness still
require outside evidence. Formal significance or conference acceptance is not
inferred. Means alone or fabricated certainty for a singleton were rejected.

Quality comparison duplicate identities
-------------------------------------

Problem: retaining the first duplicate pair silently selects evidence by input
order. Decision: count exact nonempty pair IDs before adjudication and exclude
all copies of any duplicate, including when another copy fails its audit. Each
copy retains its original position and other exclusion reasons; independent
pairs still contribute normally. The existing 100-pair admission bounds this
extra metadata pass. No record, worker or model call is added. Rejected: first
or last copy wins, which lets contradictory replicas change the reported mean.
Duplicates provide no estimate of independent replication or uncertainty.

Quality comparison system generations
------------------------------------

Problem: paired metrics could pool multiple candidate/control versions into one
mean. Decision: optional arm system_digest declarations activate version-bound
comparison for the entire input. Both arms then require valid lowercase SHA-256
identities; unbound legacy pairs cannot inherit another pair's version. Multiple
otherwise-valid generation cohorts refuse aggregate statistics without selecting
a favorable cohort; exclusions retain original input positions. A consistent
cohort reports its exact declared versions. All-legacy comparisons remain
descriptive with unknown generations. Declared digests do not authenticate
execution. Choosing the first cohort or silently pooling versions was rejected.

Offline quality comparison runner
--------------------------------

Problem: descriptive comparison had no retained command for reproducible report publication. Decision: `python benchmarks/research_quality_compare.py pairs.json --output report.json` uses the shared comparison authority, with at most 100 pairs and 8 MiB input. The report records exact input/runner/evaluator SHA-256 and Python version, comparison coverage, version cohorts and descriptive uncertainty. It executes no models, experiments, network calls or application-store operations. Exit 0 means all supplied pairs were analyzed without exclusions, not superiority; exit 3 saves incomplete comparison diagnostics; exit 1 indicates malformed input or publication failure. Duplicate fields and non-finite numbers refuse, and unrepresentable integer budgets remain excluded. Atomic output publication preserves existing reports on failure and never replaces input evidence or its alias. Benchmark outputs remain reconstructible, untracked artifacts. References still require external audit; fabricated input labels cannot prove research quality. A second statistics implementation or silently successful partial coverage was rejected.

Declared benchmark task coverage
-------------------------------

Problem: a complete comparison of submitted records cannot reveal omitted tasks. Decision: the offline runner accepts `--manifest manifest.json`, whose version-1 object declares a nonempty `pairs` array with pair_id, task_digest, corpus_digest, model and budget_digest per planned pair. Unique nonblank identities are required. The report retains exact manifest SHA-256 and separately lists missing, unexpected, duplicate, unidentified and identity-mismatched records. Incomplete coverage saves diagnostics and exits 3 even when supplied pairs otherwise compare successfully. Output cannot replace the manifest or its alias. Without a manifest, coverage is explicitly unknown; descriptive statistics still concern submitted valid pairs. A supplied manifest does not prove that it predates outcomes or that records authentically execute planned work. Checking pair names alone or treating partial submission as planned completion was rejected.

Planned benchmark system generations
-----------------------------------

Problem: task identity coverage could pass when all submitted records consistently used an unplanned system version. Decision: each manifest pair may additionally freeze `system_digests` with both control and treatment lowercase SHA-256 values. Missing, changed or swapped reported versions make manifest coverage incomplete and preserve explicit generation-mismatch diagnostics, even when comparison cohorts are internally consistent. Partial/malformed declarations refuse. The report counts version-bound planned pairs; legacy manifests declare zero such bindings rather than inheriting submitted versions. Digest shape validation is shared with comparison admission. This is declared plan consistency, not proof of actual execution or pre-results registration. Cohort consistency alone was rejected because it cannot identify a consistently substituted generation.

Research integration verification boundaries
-------------------------------------------

Problem: old claim tests expected current inconclusive evidence to leave positive
claims valid, and a controlled cancellation test invoked a real-TeX skip inside
a background thread before reaching its fake compiler. Decision: executable
contracts distinguish stale/unproven opposition from current uncertainty: the
latter contests support without inventing refutation or a conflicting outcome.
The cancellation test directly exercises the shared compilation boundary with
its controlled compiler, independent of TeX availability. Real PDF generation
checks retain their explicit compiler requirement and remain separate coverage.
Weakening scientific decisions or masking the barrier with a longer timeout was
rejected; legacy wording alone is not a behavioral contract.

Model-scaled Research input admission
------------------------------------

Problem: a fixed 48k input ceiling rejected material even when the selected
model had enough context capacity. Decision: per-request input allowance uses
the registered model window minus actual output reservation and a 2,048-token
safety margin, including tool schemas. Callers can impose an explicit positive
input_limit. Unknown windows retain the 32k fallback; output reservations and
input limits reject invalid numeric types. Oversized inputs still fail before
dispatch without truncating durable evidence. This changes per-request admission,
not the separate action usage/dispatch ceilings, which remain long-horizon work.
Always clipping to 48k or ignoring output/tool overhead was rejected.


Research progress ownership
---------------------------

Problem: the Research usage meter independently counted identical tool arguments
as repetition, even when repeated experiment inspection observed new world state.
Decision: Research disables that argument-only detector; the shared guarded loop
owns call-plus-world no-progress decisions. Token accounting and the dispatch
ceiling remain enforced. Keeping both detectors was rejected because an argument
fingerprint alone cannot establish stalled scientific execution.


Complete bounded receipt admission
----------------------------------

Problem: extraction and normalization silently kept only the first 64 tool
receipts, allowing a later failed observation to disappear from review. Decision:
both boundaries reject overflow visibly instead of publishing a partial evidence
basis. The existing schema limit remains 64; this does not implement long-horizon
receipt paging or recovery of uncommitted observations after process loss.
Silently selecting the earliest or most favorable receipts was rejected.


Exact observed reference matching
--------------------------------

Problem: a proposed artifact/job/publication reference was accepted when it was
merely a substring of successful tool prose, including longer identifiers and
negative descriptions. Decision: retain exact ledger/artifact references or an
exact string value in a complete parsed JSON result; keys and prose substrings
do not grant reference authority. Failed receipts still grant none. Malformed or
truncated JSON cannot establish additional references. This proves observed
identity only, not the existence or independent authenticity of an artifact.
Substring matching was rejected because it admitted references never reported.


Research action original-observation journal
--------------------------------------------

Problem: an experiment tool could finish, but a later model/commit failure or
process exit lost its original result before workspace publication. Decision:
experiment callbacks write immutable 30,000-character parts on the selected
record authority, then an immutable manifest, before model-visible projection.
The schema is `contracts/research_action_observation_v1.schema.json`. Bindings
include owner, task, direction, language, workspace revision and a digest of the
exact hypothesis/protocol basis. Per-task ordinals cannot be rebound. Full UTF-8
SHA-256, part positions, lengths, versions and bindings are checked on read.
Identical retries and ambiguous acknowledgments accept only exact readback.
Unpublished parts grant no observation; a refused write stops model continuation.

The journal is durable observed user evidence, not a disposable transport cache.
Parts bound individual storage records without capping total result length.
Recovery reconstructs acknowledged originals by task/ordinal and frozen binding;
it neither executes commands nor publishes scientific decisions. Final workspace
CAS remains the authority for reviewed results. Automatic action continuation,
manifest discovery in the product, the 64-receipt working set, and bridging a
crash between an external effect and capture remain separate unfinished work.
A parallel filesystem store or automatic replay after an ambiguous effect was
rejected because it would split authority or duplicate experiments.

A local native-store specimen with 1,000,000 Unicode characters (3,500,000 UTF-8
bytes) wrote 34 parts plus binding/manifest in 36 acknowledged commands. Encoded
record values totaled 3,526,947 bytes; the largest was 105,358 bytes. Capture took
556 ms with 4.07 MB traced Python peak allocation; restart readback took 63 ms
and matched exact bytes. These are one synthetic-host measurements with zero
model calls, not a concurrency or crash-at-arbitrary-instruction guarantee.
Growth follows retained observed bytes plus one binding per task and one manifest per observation;
there is no TTL reclamation of these durable experiment originals. Full-original
recovery materializes the requested result, while writes use bounded chunks.


Research observation discovery after process loss
------------------------------------------------

Problem: original-result recovery required the caller to already know the lost workspace revision and hypothesis/protocol digest. Decision: `ActionObservations.open` reads the immutable task binding using explicit owner/task identity; it never substitutes the current workspace. `page` reads at most 17 manifests per query (1..16 requested plus lookahead), excluding full result parts. Its opaque cursor is scoped to the task and uses lexical storage order, not experiment chronology. Missing ordinals remain missing; a binding without a published manifest gives an empty page. Each row must preserve owner/task/basis/version and exact key/ordinal identity. Malformed, duplicated, unsorted or foreign rows fail visibly. Original bytes still require `read` digest/part verification. These are read-only recovery primitives, not automatic execution, a scientific verdict, or a product UI entry. Requiring the live task or guessing from the current protocol was rejected because both can disappear or change after the original observation was recorded.


HTTP access to retained experiment observations
-----------------------------------------------

`GET /api/v4/research/actions/{task_id}/observations` calls the owner-scoped
recovery query under the shared authentication gate. `limit` is 1..16 (default
8); `cursor` is task-scoped. The response follows the observation contract's
`recoveryPage`: found, saved binding, observation manifests and next_cursor.
Absent and other-owner tasks return found=false with no binding or rows.
Invalid pagination returns 400. Storage faults remain errors, never an empty
successful result. The route works without any live task, reads no original
parts, dispatches no model or tool, and grants no scientific conclusion.
This connects durable recovery to the product HTTP surface; UI discovery,
full-original access and automatic continuation remain unfinished work.


Workbench retained-observation inspection
-----------------------------------------

Problem: retained experiment observations had a recovery API but no workbench reader after an action failed. Decision: the experiment section exposes a read-only, demand-loaded observer for a task ID. Live experiment polling suggests the active task; remembered IDs remain usable after restart. Loading and next page controls use the shared UI kit. Results show saved workspace revision, observed tool status and escaped result excerpts, explicitly distinguished from reviewed research conclusions. No workspace save, model dispatch or command replay occurs. API failures remain visible and retryable. Editing the requested task, switching workspace identity or disposal aborts requests and fences late completion. Ordinary telemetry preserves this DOM island and unsaved editors. Automatic execution or merging these receipts into claims was rejected because observation recovery alone cannot establish safe replay or scientific validity.


Verified original experiment downloads
--------------------------------------

Problem: recovery exposed excerpts but the original was inaccessible over HTTP; full-result reads also allocated memory proportional to the original. Decision: `GET /api/v4/research/actions/{task_id}/observations/{ordinal}/original` verifies all owner/basis/part identities and the complete UTF-8 SHA before returning a plaintext attachment. The repository reconstructs into an anonymous temporary file using bounded chunks; no unverified prefix is published. The response streams through the existing request executor and closes its file on completion, disconnect, unconsumed-response teardown, or preparation failure. Temporary disk growth follows the requested original's UTF-8 size; it is reconstructible and never replaces durable records. Responses are private/no-store, nosniff, and carry exact byte length and digest ETag. Unknown and foreign observations return 404; corrupt or missing parts remain failures before download success. This is read-only provenance access, not artifact authentication or scientific approval. In-memory reconstruction and streaming before final verification were rejected because they either scale resident memory with results or publish partial data before discovering a corrupt later part.

A private native-store specimen exported 3,500,000 UTF-8 bytes (34 parts) and
read them back with matching SHA in 83 ms, with 1,669,860 bytes of extra traced
Python peak allocation. The file was closed afterward; no model was called.
This is one host/specimen, not an RSS or throughput guarantee. Disconnect
teardown shields and settles any pending executor read before closing the spool,
including repeated cancellation during settlement, instead of abandoning a
worker that may still hold the descriptor.


Frozen basis preservation before experiment tools
-------------------------------------------------

Problem: retained observations referenced a basis digest but not the original hypothesis/protocol; later workspace edits made faithful recovery impossible. Decision: before each experiment tool executes, the action acknowledges an immutable exact basis snapshot on the selected record authority. Its owner/task/ revision binding and canonical JSON digest are checked; identical retries accept only identical values. The same bound repository captures the resulting original, so a later mutable workspace cannot rebind that tool result. A refused basis write prevents tool execution. `load_basis` recovers a detached snapshot after process loss and rejects corruption or a missing task binding. Legacy absent snapshots remain absent; current workspace content never fills the gap. This freezes the declared basis, not external artifact authenticity or safe automatic replay. Snapshots grow once per task, not per model/tool round. Keeping only a hash or substituting the latest workspace was rejected because neither preserves the original experimental meaning.

A native-store specimen with a 12,000-character hypothesis and 2,000-character
dataset declaration stored a 14,366-byte basis record. Five identical freezes
left one version-1 snapshot; the first cold freeze took 25 ms with zero model
calls. This measures one host/specimen, not universal latency. Durable growth is
one exact declared-basis snapshot per task; ordinary observation pages do not
load that snapshot. No TTL silently reclaims these user research declarations.


Experiment effect-intent fences
-------------------------------

Problem: after a crash between tool execution and original-result capture, no
record distinguished "never attempted" from "effect may have happened".
Decision: experiment execution acknowledges an immutable intent after basis
freeze and before crossing the tool boundary. It records owner/task/basis,
ordinal, call ID, effective tool, a canonical argument digest and a fresh
admission ID, without persisting raw arguments or credentials. A fresh call
cannot reuse an admitted ordinal. Distinct admission IDs make concurrent CAS
contenders mutually exclusive; an ambiguous ACK accepts only its own exact
immutable readback. Intent write refusal prevents the tool effect. Result
publication checks that its call/tool identity matches the intent.

`GET /api/v4/research/actions/{task_id}/effects` pages 1..16 intents under the
shared auth gate. Missing result manifests are `unresolved_effect`, never
"failed" or "not executed". Matching manifests are `observation_published`;
this is publication metadata, not full-original verification, remote completion
or scientific approval. Both states have replay_allowed=false. Queries validate
bindings, basis and storage order, read no original parts, and hide foreign
owners. Unknown old records do not acquire invented intents. The fence protects
reuse of the same slot; cross-new-task/ordinal duplicates, model usage recovery,
terminal task completeness and automatic continuation remain unfinished work.
Treating no result as permission to rerun, or allowing same-payload CAS retries
to admit two concurrent effect executors, was rejected.

A private native-store specimen recorded eight intents in 3,424 encoded record
bytes. Four result manifests were published and four remained unresolved; setup
plus inspection took 57 ms with zero model calls. Durable growth is one compact
intent per tool slot. Paging bounds live metadata and does not load original
parts. These are one-host measurements, not a throughput or replay guarantee.


Durable Research logical model accounting
-----------------------------------------

Problem: Research agent usage lived only in task memory; a restart lost paid-call
progress and could reset the next attempt's budget. Decision: after context
admission, a unique immutable logical-call intent is acknowledged before entering
dispatch. It binds owner/task/basis, dispatch ordinal, logical round, requested
model, request digest, output reservation and the current meter snapshot. A fresh
call cannot reuse a slot; concurrent candidates use distinct admission IDs under
CAS. The next slot requires the previous acknowledged usage, and every accounting
field except dispatch/forced-final admission flags must match, preventing token,
call, cost or budget reset before payment. Raw prompts and credentials are not
persisted in this ledger.

The shared chassis accounts the response first. Its synchronous result hook
acknowledges the cumulative snapshot before tool execution; write refusal raises
and stops progression. Mapping receipt is recorded literally, not as proof of
complete provider billing. Missing/partial usage and pricing estimates retain the
shared meter's disclosure fields. `GET /api/v4/research/actions/{task_id}/model-calls`
serves owner-scoped pages with unresolved_usage or usage_recorded; both have
replay_allowed=false. Unknown/foreign tasks remain indistinguishable. These are
logical agent-call checkpoints, not provider-internal attempt accounting,
terminal task seals, restored agent context, or automatic continuation. Resetting
usage on restart, inferring a free call from missing usage, and proceeding after
an unacknowledged result checkpoint were rejected.

A private native-store specimen recorded two simulated logical calls and their
usage checkpoints in four records totaling 3,476 encoded bytes. Writing and
reading the page took 61 ms with zero actual model calls. Growth is one intent
and one usage checkpoint per logical call. Snapshot pages are bounded and load
no raw model prompts or original tool parts. This measures one host/specimen,
not end-to-end provider latency or complete invoice reconciliation.


Research action terminal settlement receipts
--------------------------------------------

Problem: intent/usage/original records survived restart, but task completion and
workspace publication remained only in the live registry. Decision: Research
opts into TaskRuntime's terminal durability port. Resource settlement precedes
this callback; terminal status/events become visible only after its ACK. Failure
retains the same immutable candidate and prevents TTL eviction. Finish or poll
retries only its metadata write; no model, worker or tool is restarted. A later
error cannot replace an already-settled candidate. Successful workspace CAS
stamps its revision before compilation-resource cleanup, independently of final
task success/failure.

`GET /api/v4/research/actions/{task_id}/terminal` exposes an owner/task-bound
immutable settlement fact after eviction/restart: status, committed workspace
revision when known, result digest, safe error kind and settlement time. Unknown
and foreign tasks return found=false; no current workspace or journal absence is
used to invent success. A task error can coexist with a committed revision.
Receipts grant no replay, remote-job completion, complete billing or scientific
approval. Callback ACK loss accepts only exact readback. Generic runtimes remain
transient unless configured with this port. Persisting after visible completion,
rerunning execution to repair storage, or replacing a settled outcome on a retry
was rejected.

A private native-store specimen with a 12,000-character result summary retained
only a 396-byte terminal fact (the full result is represented by its digest).
First finish/ACK took 67 ms with zero model calls. Growth is one immutable fact
per settled action; a pending candidate retains one result reference in the
existing capacity-bounded task registry and is never silently TTL-evicted.
These are one-host measurements, not provider completion or throughput claims.


Private exact model request material
------------------------------------

Problem: logical-call intents retained a digest but could not reconstruct the material actually sent after workspace, research artifacts or tool history changed. Decision: after context admission and before model admission/dispatch, Research snapshots the exact messages, admitted wire tools and output reservation on the selected record authority. Private chunks are content-addressed within one owner/task so successive request prefixes share bytes. All chunks ACK before a manifest; the model intent then binds its exact canonical request digest. Failed preparation never dispatches the model. Reads require that admitted intent, check binding, chunk order/length/hash, whole digest, canonical JSON shape and output reservation before returning a detached material copy. Legacy absent snapshots remain absent; prepared but unadmitted material is not exposed as a request. Provider transport headers and credentials injected after this boundary are outside the snapshot. User research data and user-supplied defaults already present in messages remain private owner/task state, not public ledger metadata.

This stores actual prior tool history as seen by the model; it grants no tool
execution authority, budget credit, current-model equivalence or automatic
continuation. Raw request reconstruction materializes only the explicitly
requested snapshot; writes remain chunk bounded. Rebuilding from the latest
workspace or duplicating full request prefixes per round was rejected because
it either changes historical meaning or amplifies long-run persistent growth.

A private native-store specimen with two successive Unicode request snapshots
referenced eight logical chunks but stored four unique chunks totaling 201,968
encoded bytes. Writing and restoring both took 272 ms with zero model calls.
The repetitive fixture benefits from both within-request and prefix sharing;
this is not a general compression or throughput claim. Growth follows unique
private chunks plus per-request descriptors. No TTL silently discards recorded
request material. New intents mark expected snapshot version 1: a missing
acknowledged snapshot is an integrity failure, while old unmarked intents remain
explicitly missing without synthesizing current material.


Exact model reply preservation before progression
-------------------------------------------------

Problem: usage checkpoints proved response accounting but did not preserve the
reply; a failed workspace commit could lose generated plans/results and force
another model call. Decision: each admitted Research model reply is captured as
exact message plus finish marker in private task-local content-addressed chunks.
Parts ACK before the immutable response manifest, which binds task/basis,
dispatch ordinal and admission ID. The usage checkpoint then names its response
digest; new intents require that response link before tool progression.
Response-write refusal stops the loop before tools. User content/tool proposals
are preserved as data and are never executed by inspection.

ModelResponses.load_response verifies binding, admission, chunk boundaries/order,
part and whole hashes and canonical JSON before returning a detached reply.
Inspection can recover an acknowledged reply even if later usage publication
failed, but grants no budget credit or replay. Lost acknowledged manifests are
integrity errors; a call with no received reply remains explicitly unknown.
Legacy usage without a reply is not filled from current material. Generic private
chunk helpers are shared with request snapshots, preserving task isolation and
prefix/content sharing. Recalling the model, copying only summary prose, or
progressing after a refused response write was rejected because they change the
original outcome or lose the recovery boundary. Task continuation and scientific
approval remain separate.

Measurement: a synthetic Unicode reply on this host used three private parts
(221,169 encoded bytes) and one manifest; write plus verified restoration took
203.98 ms on the native backend. No model calls were made; this is a storage
measurement, not evidence of research quality or provider latency. SQLite and
native clean restart, missing/corrupt chunks, immutable replacement, and live
response-write refusal are covered by focused tests.


Cumulative Research accounting continuity
----------------------------------------

Problem: checking total tokens alone allowed a response checkpoint to erase
previous cache/reasoning tokens, priced calls, costs or uncertainty. Decision:
checkpoint publication rejects decreases or removal of every previously present
cumulative token/cost counter, removal of accounted model identities, and clearing
sticky cost-estimation or budget-violation flags. Older minimal snapshots retain
their declared fields; omitted history is not invented. Merely trusting the newest
snapshot was rejected because recovery could then grant budget from lost history.
This validation does not yet reconstruct the live meter or authorize continuation.


Restoring acknowledged Research usage
------------------------------------

Problem: durable usage remained read-only metadata; a newly constructed meter
would reset consumed budgets. Decision: PaperAgentUsageMeter.from_snapshot
restores the complete versioned snapshot, including counters, declared budgets,
rounded displayed costs, model identities, uncertainty and forced-synthesis
reason. It rejects missing/malformed counters, non-finite costs and inconsistent
exhaustion state. Restored meters use the canonical chassis repetition detector;
its call/world history is a separate requirement for task continuation.

ModelCalls.restore_usage_meter reads the frozen basis and an acknowledged call, and refuses unresolved usage or a checkpoint with an already admitted successor. It creates no records and grants no execution ownership. Normal call admission still compares cumulative snapshots and CAS-fences the next ordinal; a concurrent successor cannot be replayed by merely restoring an earlier meter. Recreating a fresh meter or estimating absent legacy costs was rejected because it grants unearned budget. Historical per-round tool authority is not reconstructed; future rounds pass through the shared tools_for_round/decide_round policy. SQLite and native restart tests restore the meter and admit the next distinct logical call, while exhaustion/forced-synthesis tests retain tool restrictions. These tests use synthetic usage and no real provider calls. Automatic Research task continuation still requires persisted loop progress and execution ownership.


Durable Research completed-round material
----------------------------------------

Problem: restoring a meter and chassis separately left the post-tool model
history only in process memory. Decision: Research's production on_checkpoint
callback persists messages, settled tool-round records, cumulative usage and
chassis state together through private task-scoped chunks. The immutable
loopCheckpoint manifest binds the complete material digest to the model dispatch
ordinal and admission ID. All parts acknowledge before manifest publication;
publication refusal propagates before another provider dispatch.

LoopCheckpoints verifies chunk ordering/length/hashes, task/basis identity,
accounted usage equality, next-round identity and settled tool status. Inspection
returns detached data without executing tools. restore_checkpoint additionally
requires complete meter accounting and refuses an already admitted successor;
old checkpoints remain inspectable without granting stale budget. Missing,
corrupt or unsettled material cannot become a continuation point. Publishing
only a summary or persisting the pieces independently was rejected because it
loses exact post-tool context or permits a mixed-generation recovery.

Research save/read ports also invoke the shared complete chassis validator;
missing limits/progress, malformed counters, extra policy keys or invented retry
credit fail as storage integrity errors, even with recomputed material hashes.
Valid halted state stays inspectable, with no grant to execute another round.

The selected authority retains these immutable private records without a TTL. Chunks share content with request/reply material where their boundaries match; full checkpoint material includes cumulative messages and tool-round history, so this interface does not claim constant-space history or perfect cross-round sharing. New research actions use an explicit `logical_budget` (token_budget and dispatch_budget, nonnegative integers); omitted/zero values mean no cumulative cutoff. Single-request model-capacity admission, cancellation, no-progress protection and durable call/tool accounting remain active. Task deduplication includes the normalized allowance; changing it never mutates an existing task. Frozen recovery snapshots retain their original finite or unlimited policy. Problem: a fixed ten-round ceiling forced unfinished experiments to synthesize. Decision: caller-selected cumulative policy; rejected: another larger fixed cap. Logical token accounting is not a complete provider billing ceiling. Durable checkpoint growth follows completed work and is not constant-space; the local 17-dispatch chain saves 16 tool-round checkpoints: about 0.51 MiB of cumulative JSON payload on each store (record envelopes/shared chunks excluded), with controlled provider frames and real local commands. This is not a disk-growth or paid-model performance benchmark. This removes implicit action cutoffs, but does not bypass context/storage capacity or confer restart ownership. Long-horizon continuation remains separate work. This change captures production continuation material but does not yet automatically restart Research tasks. Restart tests use controlled tools/providers; the local long chain uses real commands with controlled model frames.

New model intents mark expected loop checkpoint version 1. Admission of the next
logical call independently requires the previous complete checkpoint; ignoring
a callback exception cannot bypass that requirement. Old unmarked intents do
not acquire invented checkpoint history. A synthetic Unicode checkpoint used
three private parts (266,562 encoded bytes); write plus verified restoration
took 184.92 ms on the native backend on this host, with no provider calls.

Problem: a temporary storage failure before a later model admission discarded a productive live loop. Decision: the same caller may resume its last acknowledged checkpoint after retryable busy/unavailable/timeout errors in that unpaid phase. It reopens the configured authority, verifies the exact original process/nonce, requires no admitted successor, and compares settled tools and original budgets. History, usage, round frontier, claimed IDs and progress breakers are restored; the captured model preference and tool epoch stay with the caller. Retry hints wait on the existing abort event. Unpaid storage interruptions have no attempt ceiling: the shared storage delays (1/2/4 seconds, then 4 seconds) provide backoff, with longer server hints respected. Recovery reads may also wait on transient errors; each successful probe still verifies ownership, accounting and tools. Progress to a new completed frontier resets backoff. Cancellation interrupts the wait. Waiting status emits once per frontier; continuing emits only after the next admission succeeds, avoiding repeated status churn during a long outage. Storage authority restart is distinct from executor restart: a live caller can reconnect to its retained claim and checkpoint without minting a new owner or budget. The `storage_restart` local-chain regression actually restarts SQLite or TofuDB before the eighth admission; the owner nonce and seventh usage record remain exact, 16 physical commands execute once, and workspace settlement commits once after 17 controlled model transports. No application-executor takeover is granted by this storage restart. Rejected: restart the action, reset budgets, replay tools or retry paid/uncertain dispatches. Nonretryable, unresolved, changed-owner/state and aborted paths retain their failure behavior. This is live continuation, not restart ownership transfer. No worker, store, namespace or steady-path read is added. Failure recovery reads one complete checkpoint and retains its detached history/chassis without a second full material copy. Verify fault guards and `live_retry` local settlement: two seven pre-admission faults across two frontiers, 17 controlled model transports, 16 uniquely logged real commands and one workspace commit; scientific measurements remain synthetic. The SQLite fixture's two restored JSON materials measured 36,436 and 50,440 bytes; all 16 checkpoint materials totaled 700,762 JSON bytes. These measure serialized material, not resident memory, disk amplification or paid-model cost.

Problem: live Research loops retained every completed model admission repository and intent despite durable checkpoints owning that history. Decision: release the in-memory admission only after its checkpoint is exactly acknowledged; the current in-flight call stays available for response/accounting/checkpoint hooks. Rejected: clearing before acknowledgment or retaining all completed admissions. The controlled 17-transport SQLite chain measured peak live admission repositories falling from 17 to 1, and their serialized intent payload from 17,360 to 1,024 bytes. Weak-reference lifetime checks cover the real production loop; these are object counts/JSON sizes, not whole-process RSS. Durable journals, tool history, budget and replay guards remain authoritative; this does not claim constant-space whole-task history. No worker, store, query or persistent-growth policy changes.


Research agent execution ownership
---------------------------------

Problem: checkpoint recovery had no durable identity for the original executor;
a registry miss or elapsed timeout could be mistaken for permission to run again.
Decision: before its first paid dispatch, the Research agent claims one immutable
owner/task/frozen-basis record with a distinct worker nonce and observable local
process incarnation. Concurrent workers CAS the initial record; only one wins.
The winner checks its claim before provider dispatch, tools and round checkpoint
publication. Unsupported process evidence permits an initial unique claim but
never certifies process loss. Forked handles with a different process stamp fail.

lib/process_owner.py is the shared local evidence authority; Evolution review recovery retains its existing public imports and semantics. Host, OS boot, PID namespace, kernel start time and terminal process state distinguish a live incarnation from PID reuse or an exited, unreaped process. Kernel `Z`/`X`/`x` states establish exit only with one remaining thread; an exited leader with other threads retains authority. Stopped, sleeping and I/O-waiting remain alive. Problem: PID/start-time equality misclassified zombies as live executors. Decision: inspect state and `num_threads` in the same [kernel stat read](https://www.kernel.org/doc/html/latest/filesystems/proc.html); rejected: waiting for PID removal after whole-process exit, declaring a threaded process dead from its leader alone, or inferring death from inactivity. Malformed, inaccessible or foreign evidence remains unknown. Executor exit does not settle billing or external jobs. Verify `tests/test_process_owner.py`, including a real unreaped child and cold Research task query, plus Evolution review-recovery guards. No extra probe read, worker, scan or retained state is added. Claims have no expiry and are not released merely because agent-loop context returned: workspace/task settlement has its separate durability boundary. Initial claims cannot be replayed even when their recorded process is dead. Timeout stealing and assuming an absent registry proves quiescence were rejected because they allow duplicate effects. This change supplies execution identity and fencing, not automatic takeover, external-job quiescence proof, or resumed workspace publication authority.

Malformed non-decimal process start ticks remain unknown, never proof of loss.
Measurement: one native owner record occupied 496 encoded JSON bytes; initial
claim plus three fence reads took 50.99 ms on this host with no provider calls.
There is one owner record per action, not one per tool round; no new worker pool
or expiring ownership timer is introduced. A closed storage transport fails
execution checks even when a reopened connection confirms the claim survived.


Execution facts before result truncation
---------------------------------------

Problem: job state/identity after the 4,000-character preview boundary disappeared
on persistence, and normalization rewrote background pending receipts as errors.
Decision: tool capture extracts optional execution_observation from the complete
producer payload before presentation. The research-program schema owns its
states and bounded job identity; normalization preserves the detached observation
and explicit pending status. Legacy receipts without it remain readable without
inventing missing state. Generic result/API IDs are not guessed as job IDs;
oversized IDs are omitted rather than truncated into a different identity.

Execution state uses that captured fact before legacy excerpt parsing. Exact captured job IDs can satisfy an observed reference even when the preview is not complete JSON. A named job or explicit handle with no recognized state remains planned, even when its command or outer API succeeds. Reusing generic success as job completion was rejected because it falsely settles submitted work. A known queued/running/planned job remains unresolved until a successful response for the same job and provider scope observes a terminal state; another job, another provider or a failed poll cannot clear it. Provider binding or namespaced MCP server scopes identity; an unscoped tool uses its own exact name. Once no jobs remain active, any latest failed job makes the run failed; another job’s completion cannot clear it. Legacy receipts supply no correlation proof. These observations prove neither process quiescence nor scientific authenticity.

Reparsing lossy summaries, substring IDs and letting the final unrelated receipt
imply completion were rejected because they lose or misattribute producer facts.

Measurement: retaining a running state plus an eleven-character job ID added
42 compact JSON bytes per receipt. Capturing 100 synthetic responses with a
20,000-character log prefix took 9.03 ms on this host, without provider calls.
Metadata is bounded by the receipt schema; complete originals retain their
existing journal lifecycle and are not replaced by this small state projection.

Cross-action unfinished-run receipt continuity
---------------------------------------------

Problem: a later status action replaced an unfinished run's receipts, losing
other active jobs and their original provider interpretation. Decision: current
experiment receipts receive server-assigned source_task_id and a frozen provider
scope; pending run progression carries previous receipts before applying current
observations. Scope tags distinguish explicit providers, namespaced MCP servers
and unscoped tools. Historical scopes are never recalculated from new bindings.
Known historical job observations remain relevant when current bindings no longer
include the submission tool. Scientific basis changes cannot rebind pending data.

Only the same source task plus nonempty call ID can identify a repeated receipt.
Reapplication preserves its original timestamp; conflicting facts reject rather
than overwrite or append a second fact. Another task remains a distinct source.
Old unidentified receipts remain retained without invented origin or scope.
Replacing all receipts, correlating bare IDs across providers, and silently
truncating overflow were rejected because they lose or misattribute pending work.

Unpaged runs still reject 64-receipt overflow atomically. ReceiptPages stores owner/run/basis-bound immutable pages of at most 32 receipts/128 KiB, without a total history ceiling. Workspace CAS publishes verified prefixes; raw pointers and tail-only settlement are refused. Complete reads verify every page via disk reversal and clean up on every exit. Workers activate paging for pending runs. Problem: paged terminal results could not enter decisions or scientific audits. Decision: typed terminal ports recheck owner/basis/revision and complete job state; success recomputes full receipt_evidence bound to the immutable run, pages and tail. Alternative: model/tail summaries were rejected because they lose earlier failures. Oversized science resolves an owner/basis/tool/call-bound journal original; hashes and parts verify before reuse. Missing legacy originals stay incomplete; ambiguity/corruption fail. Original recovery 24455 chars/3.73 ms; successful 66-source settlement 206 ms. Workspace CAS binds receipt_progress source/job indexes to exact receipt heads; updates verify only new pages. Native: after 82 rows, commit/checkpoint read 23.88 ms; poll medians 79.71→4.68 ms (seven each). Workers and read-only execution inspection use published indexes; After snapshot load, 1,026-row inspection needs at most three checkpoint/root reads (SQLite/native), zero page reads. Legacy scans and full terminal rechecks remain.

Consumed receipt identity across settlement
-------------------------------------------

Problem: replaying a settling poll supplied only its current-action receipts,
while the completed run retained its full lineage; comparison treated that subset
as another experiment. Decision: before constructing a new experiment record,
index explicit source_task_id/call_id pairs in completed working observations.
An input containing only already consumed, identical facts preserves those
observations and ignores new experiment/claim proposals. Changing a model's run
label cannot make the source novel. Timestamp changes are not new facts; a
conflicting explicit source rejects. New or unidentified sources retain their
existing processing path and are not discarded by a guessed identity.

Comparing only proposed run IDs or replacing full lineage with a final poll was
rejected because both can count the same consumed source twice. Replay never
rebinds old execution to the current hypothesis. This guard covers completed
working observations; archived source indexing and pending receipt paging remain
required for full long-horizon recovery. SQLite/native restart tests preserve the
single consumed result across a repeat workspace save without rerunning tools.

Measurement: 100 synthetic reapplications retained one run and its three
original receipts in 70.58 ms on this host, with no provider calls. The lookup
indexes references to bounded working receipts; it introduces no durable copy
of each replay or new worker pool.

Published source index across archival
-------------------------------------

Problem: completed-source replay checks lost their witness when working results moved into paged history. Decision: workspace CAS publishes a content-addressed hexadecimal trie of explicit receipt source keys and immutable fact hashes. Lookup follows at most 64 key digits; nodes ACK before publication and orphan roots are not evidence. Owner/direction/language bind node access. Missing, corrupt, foreign or conflicting nodes fail closed. New completed records whose sources were all consumed reject at the save boundary. Legacy archives backfill once; pre-migration replay scans the verified published chain with bounded state. Scanning all history on every call or trusting unreferenced index nodes was rejected due to latency and false consumption. Index migration does not change scientific conclusions or authorize task execution. Node cache derives from the cached launch memory/CPU probe; failed or invalid probes fall back to one trie path. The cache lifetime ends with its repository; there is no total-source ceiling. Full paged sources enter new accounting/backfill/legacy matching; tail-only accounting was rejected because it loses early sources. Native measurement: 130 paged sources prepared in 219.65 ms with 65 cache entries; preparation grants no publication authority. Old path copies persist; reclamation remains separate maintenance work and must retain every published root.

Publication ACK integrity
-------------------------
Problem: a success-shaped workspace ACK could report another document/version.
Decision: accept the exact proposed document and next integer revision; otherwise
accept only one exact selected-authority readback. Lost ACK recovery uses the same
proof. Malformed or competing readback rejects without retrying tools/models.
Trusting any mapping was rejected because it could advertise an unpublished
source root. Fault tests cover both stores and retain orphan nodes as candidates.

Scientific decision interpretation: Problem: a frozen standard-error multiplier
can be misread as a calibrated confidence interval or significance test.
Decision: executor and independent reviewer share the same explanation of the
paired-mean threshold rule. `supported`/`refuted` describe that frozen rule;
confidence levels and significance claims require separately inspected methods,
calculations and assumptions. Alternatives considered: silently treating a chosen
SE multiplier as a confidence level invents calibration; changing the numeric
rule would invalidate preregistration. Numerical acceptance remains unchanged.

Research action admission
-------------------------
Problem: non-experiment actions created a task before detecting an already stale
workspace revision. Decision: all new actions check the owner-scoped workspace
revision before task birth; the worker still rechecks before executing. Existing
deduplicated task identities remain readable. Alternatives: worker-only rejection
was safe before provider dispatch but created avoidable failed tasks and delayed
feedback to an orchestration caller. This check is not a workspace lock: concurrent
edits remain governed by worker checks and the existing save CAS.

Problem: simultaneous research-action starts could both miss the task index and
create separate workers for the same owned action and workspace revision.
Decision: creation uses `ProductionRuntime.claim_task`; the losing request joins
the winner's task and never spawns. Owner, direction, language, action and revision
remain the key; worker/save checks still fence workspace changes. Alternatives:
a new research lock duplicates the shared lifecycle authority; index registration
after creation leaves the race open. This is process-local admission, not durable
external-effect replay authorization after process loss.

Problem: workspace storage collapses direction whitespace while action keys did
not, allowing equivalent direction spellings to dispatch into one workspace.
Decision: action admission uses the existing `research_direction_hash` identity;
owner, language, action and revision stay explicit. Alternatives: a second text
normalizer risks diverging from storage again; raw text keys permit duplicate
work. The task retains the supplied direction for display and existing readers.

Problem: an explicit different model could join an existing research action whose
model was absent from the dedup key. Decision: model-driven actions include the
exact bounded model value stored in the task. Compile remains model-independent
because it uses the build service. Alternatives: joining all model choices loses
caller intent; splitting compile by model duplicates identical build work. Task
identity distinguishes requested configuration, not verified final provider or
scientific independence; workspace conflict checks and evidence audits still apply.

Problem: research tool rounds omitted the shared dispatch display query. A command
could execute, then result finalization raised `KeyError`, replacing its receipt
with a tool error and obscuring external effects. Decision: construct the query
with the shared `tool_round_label` before dispatch. Alternatives: suppressing the
finalizer error hides missing input; replaying the command risks duplicate effects.
A tool error after dispatch does not establish that execution never happened.

Problem: project-less command rounds use the shared `code_exec` display identity,
while research measurement extraction recognized only `run_command`. Decision:
recognize the shared `paper_effective_tool_name` mapping when parsing command
stdout and reducing execution state, retaining command-header identity and zero-exit requirements. Alternatives:
renaming stored rounds diverges from shared execution; treating every tool as a
command broadens evidence authority. Raw JSON, stderr and failure guards remain.

Problem: handwritten measurement payloads can mismatch seed order, statistics,
partitions or frozen comparison context. Decision: `build_paired_observation` in
`lib.research.measurements` accepts measured seed/value records and explicit
comparison/partition identities, aligns frozen seeds, and validates the emitted
contract through the existing auditor. Both paths use `paired_statistics` from
that auditor. Alternatives: a second formula drifts; repairing reported summaries
hides inconsistencies. The producer does not discover identities, emit verified
audit flags, invent source references or establish execution/reproduction.

Problem: workbench experiment actions omitted the selected model, unlike discovery.
Decision: model-driven actions capture the current selection at invocation and
send it after workspace save; compilation remains model-independent. Alternatives:
server-default fallback loses caller choice; reading selection after save can
silently substitute a later choice. An empty selection preserves server defaults.

## Discovery model accounting and recovery

Problem: process-local discovery execution lost cumulative authorization after
restart and could repeat provider work. Decision: bind policy to the acknowledged
owned task, journal physical attempts, and recover only after old-process death
and complete usage. Recovery tests cover a terminated subprocess, preserved
allowance/deadline, refusal at cumulative exhaustion and expired deadline, and
terminal Evolution accounting on both stores. These use controlled usage, not
long-duration provider or scientific quality evidence. Alternatives rejected: per-stage budgets, zero-consumption
restart, TTL takeover and reusing experiment frozen-basis ownership.

`budget_binding` retains one immutable policy per task in
`research.model_budget_bindings.v1`, hashing its original request identity.
Exact selected-store readback confirms normal and lost ACKs. The binding grants
no ownership or replay. `discovery_owner` creates a nonce/process-incarnation
claim; send intents check it before and after journal ACK. The worker passes
one shared account through the public recipe into translation, survey,
ideation, auxiliary search filtering and independent review. Completed
checkpoints skip paid work; typed model aborts stop stage retries unchanged.

`model_attempt_journal` retains immutable producer-tagged intent and terminal
records with per-attempt and cumulative tokens. Failed observations fence later
sends while allowing cleanup of the original unsent lease. Unknown usage, gaps
and pending attempts never become zero-consumption evidence. Its paged summary
keeps scalar totals; it is not an atomic snapshot or whole-pipeline coverage
proof and requires old-writer quiescence before recovery use.

Manual and startup recovery of active interrupted tasks require proven process
death and complete retained usage. Recovery archives the predecessor, uses a
versioned CAS to transfer the current claim, and binds the restored account
before worker entry. New calls continue after the retained ordinal and token
total; original replies, commands and receipts are never replayed or replaced.
Public `resumable` uses a read-only eligibility hint; the command repeats the
proof and CAS. Alive/unknown owners, empty/incomplete/gapped journals, settled
Evolution runs and bound terminal error/aborted tasks remain unavailable.
Rejected admission precedes replacement task birth; storage outages preserve
their typed failure. Eligibility streams bounded pages only for stopped
budgeted tasks, with linear work in attempt history and scalar memory.

Growth bounds: one policy and current owner record per task, at most 4096 encoded
bytes each; up to two 4096-byte records per admitted request lease (including
unsent voids); one predecessor archive per recovery transition, with concurrent
contenders sharing the old-nonce key. Prepared archives may survive failed CAS.
Representative encoded sizes are policy 268, terminal 346, initial owner 331 and
recovered owner 442 bytes, excluding database overhead/RSS. Durable evidence is
retained; no additional worker, timer or history-sized cache is introduced.

Verify budget binding, physical journal and summary suites,
`test_research_worker_budget.py`, `test_research_recipe_budget_entry.py`,
`test_discovery_account_recovery.py`, `test_discovery_manual_recovery.py` and
startup checkpoint tests. Recovery tests use actual subprocess death and both
SQLite/TofuDB authorities with controlled provider dispatch. Native artifacts
are verified against retained Rust source before the gate. Discovery goal
execution limits remain disabled pending complete pipeline accounting and the
automatic discovery-to-experiment budget handoff; these tests prove lifecycle
behavior, not scientific quality or superior system performance.

Protocol drafting validates the explicit owner before material access and sends
the same owner to model dispatch. Problem: owned research material previously
entered a call that omitted provider-route ownership. Decision: bind both
accesses to the request principal. Alternative rejected: default-user routing
for background or auxiliary research work. Verify
`test_research_draft_protocol.py`; protocol drafts remain proposals, not measured
results or authority to launch experiments.

Research creation accepts optional model-only `execution_limits`: validate before claiming a task, then confirm its immutable policy after source persistence and before spawn. Deduplication requires an identical existing policy; it cannot retrofit or replace a running account. Problem: external policy binding raced dispatch. Decision: bind inside creation; rejected: post-spawn attachment. Policy failure finishes the unstarted carrier. This adds no resident worker/cache and does not yet admit Evolution's tool budget. Terminal snapshots and their compact index retain server-derived `execution_limit_reason` (1–80 characters) so archive reads preserve why authorization stopped after registry loss. Problem: the typed reason was memory-only; rejected: inferring it by parsing error text. Verify record-ACK, worker-budget and binding tests.

Protocol draft requests may bind `source_task_id` to the original owned completed research record. The workbench sends the currently opened task identity; draft reading uses that record's accepted idea, survey and gap map rather than latest per-direction publication. A missing, foreign, incomplete or mismatched source refuses before model dispatch, with no latest-artifact fallback. Calls omitting the optional field retain direction-based drafting. Problem: repeated directions and reused idea IDs could mix a historical candidate with newer evidence. Decision: explicit original-task binding; alternative rejected: infer source from direction/idea ID alone. The request field is declared by the route's OpenAPI metadata; drafts expose the bound task ID without granting experiment authority. Verify draft protocol tests and the live source-binding browser journey. No additional durable record, worker or timer is introduced.

Workspace selection retains optional `selected_idea_source_task_id` through promotion and protocol drafting. New/changed selections resolve one exact accepted idea in an owned completed record before workspace publication; source validation is repeated at experiment admission and worker entry. Existing selection pointers survive clients that omit the field, and unchanged pointers do not force receipt-publication writes to reread discovery material. The shared hypothesis identity includes the original task and idea ID when bound; action freeze checks and run validity use the same payload. Empty/omitted legacy selections retain their original hashes. Problem: drafting carried a source only transiently, so saving/experiment execution could lose its origin. Decision: persist source selection and bind it to experiment identity. Alternatives rejected: browser-only provenance and replacing legacy basis hashes. This is provenance, not evidence of scientific validity or permission to replay. One bounded ASCII task identity (at most 160 characters) is added to the existing workspace; no new journal, worker, timer or cache is introduced. Verify selection source tests, the local action chain on both stores, and the live draft/save browser journey.

Research-action context resolves bound selections through `records.load_idea_source`, the drafting/workspace authority. It carries the complete original survey/gaps and exactly the selected candidate, including candidates beyond the twelve-idea preview. Final wire admission counts messages/tools and reserves output against the selected model's context window. Oversized bound evidence uses the existing owner-scoped artifact repository as an exact JSON original and `read_tool_artifact` cursors, with source task/digest/length in the input descriptor. A reference is not observed evidence; unread portions remain unverified. Missing reader, failed retention or an oversized remaining workspace still refuses before dispatch. Raw JSON avoids backend differences in canonical-result manifest expansion. The existing 24-hour artifact TTL applies; equal originals reuse the content address. The Unicode round-trip fixture retains 480,119 UTF-8 bytes with a descriptor under 1 KiB (excluding storage overhead); foreign owners cannot read its body. Durable research records remain original authority, and source retention failure never grants replay or scientific support. Before a bound source read, one bounded probe checks availability. Missing cache is reconstructed from the owned original only when task ID, digest and length match; returned content address must also match. Drift or storage failure refuses restoration. Signed cursor authorization is not renewed: retain its ordinary error and instruct the model to start a fresh query on the same source. Restoration notice is added once, without rerunning the producer or resetting its budget. Problem: TTL interrupted long tasks; rejected: unbounded retention, producer replay and bypassing expired cursors. Verify source-continuation and local-action-chain tests on both stores. No new cache or worker is added. Verify source-continuation tests on both stores. Unbound directional previews keep the 16,000-character/twenty-gap bounds and report omissions and a half-open gap range. Problem: fixed previews withheld evidence even when the execution model could fit it. Decision: exact bound sources plus model-capacity admission; rejected: universal preview caps and silently fitting by truncation. Sources retain the existing 8 MiB record bound; no resident cache/store/worker is added. Verify context-budget and local-action-chain tests. These prove grounding/lifecycle, not research quality.

Frozen experiment observation bases carry both selected idea and original source
task when bound, alongside hypothesis/protocol. Unbound bases keep their exact
legacy shape. Problem: large bound measurements could not reload because original
bases omitted the identity used by dependency digests. Decision: preserve the
selection in the immutable basis; two IDs add 80 UTF-8 bytes in the fixture
(each at most 160 characters), without a new namespace, worker or cache.
Alternatives rejected: borrowing the current workspace or
rebinding old observations. Changed selections refuse original science recovery.
Verify original-science and observation tests; this enables exact measured-packet
recovery, not task takeover, command replay or scientific authenticity.

Research-action terminal records retain optional shared-meter `usage_snapshot`,
including forced-final reason and logical counters, on both completion and loop
failure. The owner result also exposes it. Legacy/compilation paths without a
loop keep it absent. Problem: bounded synthesis hid the resource boundary after
task memory expired. Decision: retain the existing scalar snapshot; rejected:
inventing zero usage or treating a completed action as complete research. This
is diagnostic, not physical billing proof, continuation permission or a new
budget. The forced-final fixture adds 472 UTF-8 bytes (storage overhead
excluded) to one existing terminal record; no worker or namespace is added.
Terminal ACK/readback and ordinary error behavior remain authoritative.

Action-terminal visibility requires exact selected-authority readback after normal
and lost write ACKs. A success-shaped reply without the owned expected record
keeps terminal persistence pending; polling retries the same candidate. A
different immutable record never becomes completion. Problem: accepting only a
version-shaped ACK could discard the restart fact. Decision: verify the exact record
before visibility; rejected: trusting the reply shape. One bounded scalar-record
read is added per normal terminal publication, with no new pool/store/worker.
Verify terminal fault-injection and selected-store restart tests.

Completed action history can page whole exchanges from acknowledged loop checkpoints when the current request exceeds model capacity. The initial task input and newest complete exchange remain when they fit; otherwise the complete history is referenced. No model summary replaces original evidence. Problem: removing cumulative cutoffs still left history growth to exhaust context. Decision: owner/task-bound exact checkpoint intervals through existing artifact reads; rejected: silent truncation, command replay and a separate history store. Private `actionHistoryReference` descriptors bind ordinal, interval and digest. Cache expiry restores only matching checkpoint bytes, including strictly earlier reference chains, without renewing old cursors or changing usage/ownership. Absent reader, unavailable/drifted checkpoint or insufficient remaining capacity refuses before the next paid call. Cancellation and execution fences apply during restoration. The controlled-capacity fixture executes 16 real local commands plus one history read in 18 model dispatches on both stores, with two history references; expired-cache variants restore the original without repeating commands. Cumulative re-serialized checkpoint JSON is about 2.45–2.47 MiB in this larger-output fixture (excluding record envelopes/chunk sharing); history retention remains nonconstant. These measurements are not a paid-model or disk-growth benchmark. Verify history-context, source-continuation, checkpoint and local-chain tests.

Every immutable Research journal write requires exact selected-authority readback,
including ordinary success ACKs. Missing, changed or invalid records stop model,
tool and checkpoint progression; identical retries and lost ACKs retain their
existing exact-value recovery. Problem: successful ACK headers alone could claim
publication of a record that was not actually retained. Decision: one value read
per newly acknowledged record; rejected: trusting normal headers while checking
only exceptional replies. The 60,001-character fixture writes admission, three
parts and one manifest: five confirmation reads plus its intent/prior-manifest lookups.
This adds I/O for fresh records, without another cache, worker or namespace.
Verify observation, request/response, model accounting, ownership and loop tests.

New research-action terminal records retain the exact public result, structured error and original creation time. Result bytes must match the stored digest before cold delivery. Owner-scoped GET /api/v4/tasks/{id} reads these facts after registry eviction or server restart, so the existing workbench polling can consume the original workspace/readiness result. No task is registered or executed by reading. Legacy digest-only records expose result_available=false and no invented payload; the current workspace is never substituted for an older result. Problem: completed results were inaccessible after hot-task loss. Decision: retain producer output at the existing terminal durability boundary; rejected: reconstructing it from a possibly newer workspace or minting replacement tasks/events. Retention adds one public-result copy to the existing durable terminal record, without a new store or worker. A 6,000-byte UTF-8 result fixture adds 6,069 bytes in the measured record (transport and storage envelopes excluded). API source/owner/corruption and selected-store restart tests verify this.

New Research request snapshots and their small manifests retain the original action definition: direction, language, action, expected revision, write confirmation, model override, normalized cumulative budget and declared tool epoch. Request publication checks the frozen binding and refuses changes after the first admitted definition; model admission checks its budget and model override. The transport uses that admitted model-selection flag, rather than rereading mutable task fields. Problem: crash inspection lacked complete launch settings. Decision: bind them through existing private request material and admission; rejected: inferring old settings from current defaults or retrofitting legacy rows. Missing tool-epoch evidence remains empty, never invented. Old snapshots remain readable; neither a definition nor a checkpoint grants task takeover or replay. The compact definition is duplicated once in its existing request manifest so admission needs small header reads instead of loading large source chunks. The compact fixture adds 263 bytes to request material, plus its manifest copy; new admission uses two extra metadata reads initially and three on later rounds. No namespace, cache or worker is added. Verify request-context, model-accounting, local-chain and recovery tests.

Completed-round checkpoints require a typed, unique claimed-call ledger. Each
completed/rejected tool row has one nonempty unique ID present in that ledger;
extra reserved batch IDs remain fenced even when no corresponding tool executed.
The same cross-check applies on publication and restoration, including correctly
hashed but semantically inconsistent material. Problem: tool rows and chassis
identity reservations could disagree. Decision: bind their identities before
progression; rejected: trusting status flags or hashes alone. This is identity
consistency, not proof of producer quiescence or authority to replay effects.
Validation is linear in retained calls, with temporary sets only; no persistent
record, worker or cache is added. Verify checkpoint and real local-chain tests.

Actual model admission compares every request definition with the first admitted
request, including previously staged snapshots and definition presence/absence.
Problem: preparing different requests before the first admission bypassed the
save-time comparison. Decision: enforce at the consuming admission boundary too;
rejected: assuming request preparation and dispatch always interleave. Context
may advance with unchanged definition and cumulative usage. This adds two small
metadata reads on later snapshot-backed admissions, with no new store or worker.
Verify pre-staged request tests and the real local action chain.

Research actions pass their launch-time resolved model as the transport preference
on every round, including actions using the default selector. Explicit model
selection remains strict; default selection retains its existing fallback policy.
Problem: default transport selection could change while context and accounting
still used the original model. Decision: freeze the preference for the action;
rejected: rereading mutable global defaults each round. This does not certify
fallback provider identity. No worker, cache, record or extra model call is added.
Verify the live-loop default-change and model-accounting tests.

Cold task queries expose `interrupted` when an owned frozen Research basis and execution claim survive but the original local process incarnation is proven dead. A retained terminal record takes precedence. Missing registry entries, elapsed time, alive or unknown process evidence do not establish interruption. The response has no invented finish time, result or settlement; metadata marks `executor_process_lost` and `settlement_recorded=false`. Problem: restart made in-flight actions appear nonexistent. Decision: report durable executor loss; rejected: infer completion or grant replay from process death. This is inspection, not automatic continuation, tool replay or proof that external jobs stopped. Cold unfinished queries add at most six journal reads (binding, frozen basis, binding verification, initial claim, recovery head and its immediate predecessor), with no scan, writes, workers or retained growth. The workbench releases action polling and shows the interruption reason while preserving the workspace. Verify terminal/owner guards, cold v4 restart queries and the interrupted-action browser test. Process death does not establish when execution stopped, whether a workspace commit occurred or whether paid usage is fully settled; these remain separate continuation checks.

Reopening a Research workbench checks owner-scoped pending and running actions
with exact direction/language filters applied before the list limit. One matching
action reattaches its original task ID; this lookup starts no model call and grants
no new budget. Failed or ambiguous discovery blocks new actions. Problem: refresh
lost the browser task pointer and allowed duplicate starts. Decision: recover the
existing identity from owned task listings; rejected: automatically launch another
action. Navigation generations discard late lookups, polling uses one request per
task/generation, and completion preserves unsaved editor changes. This covers live
task reattachment, not process ownership transfer. Verify scoped HTTP filtering
and real-browser rejoin, ambiguity, failure, draft and late-navigation tests.

Research action process-continuation admission

Problem: immutable action claims fenced duplicate executors but could not transfer a complete unpaid frontier after process death. Decision: retain the initial claim and admissions unchanged, archive each predecessor, and CAS one versioned owner head. Reject time-based takeover, missing process identity, incomplete billing, terminal settlement, admitted successors, changed tool epochs or chassis limits. The returned material, cumulative meter and original action definition preserve completed tools and the original budget. Exact readback resolves a lost ACK; concurrent claims yield one winner and fence the old handle. This repository port does not activate a new worker or automatically resume application startup. `action_continuation.prepare_action_continuation` verifies the owned original workspace revision, scientific basis and selection before transferring the claim, then returns the original task fields and frozen tool epoch. Existing `run_research_action` accepts this typed context and restores exact messages, tool rounds, cumulative usage and chassis state before the next model admission. The original resolved model remains pinned even when the default selector changes. Preparation retains the checkpoint digest and policy, not a second full private checkpoint. The worker reloads the immutable material once and uses its message and tool values directly; temporary root containers are released before dispatch. Weak-reference checks observe zero retained restored checkpoint containers during provider dispatch; this is a lifecycle measurement, not a whole-process RSS claim. Completed effects are retained, and only new rounds execute tools. An explicit owned POST `/api/v4/research/actions/{task_id}/resume` now re-registers the original task and admits transfer inside one detached worker. Same-process duplicate requests share that worker's admission future; cross-process claims still use the durable ownership CAS. Scheduling/admission refusal removes the pending carrier and records no invented terminal settlement. Original metadata, model selection, external-write confirmation and budgets cannot be replaced in the request. The workbench offers a kit-built Continue recorded task button after observed interruption, preserves unsaved drafts and fences navigation responses. The task-worker process role invokes `action_startup.resume_interrupted_actions` after readiness, behind the existing read-only preview fence. The operator sweep reads immutable binding pages and supplies each validated owner explicitly to the same resume command. It skips terminal publications, already registered tasks, and alive/unknown/missing execution claims; each eligible action repeats the full checkpoint, policy, billing and CAS proof. Refusal isolates that action, without new settlement, replay, budget reset or another authority. Cold workbench enumeration remains separate from this startup execution recovery.

The sweep retains at most one 16-row binding page plus scalar counters. Settled
actions require one terminal existence read each; only proven-dead candidates
load private continuation material. No persistent index, periodic timer or extra
executor is added. The sweep checks the existing application shutdown event
before each page, row and resume command; it starts no further scan/admission
after observed shutdown. The 24-binding fixture uses two binding-list RPCs.
Scan time remains linear in retained action identities, including settled ones;
a faster active projection is not claimed by this implementation. Actual model/tool concurrency uses the existing host-derived
task capacity; overflow remains durable and explicitly resumable. Verify paged
multi-owner/refusal tests and a fresh-interpreter exit followed by startup recovery
on each selected store. This proves controlled lifecycle continuity, not science.

Latest admission discovery folds 16-row header pages with numeric ordinal order
and rejects sequence holes; no usage/material chunks, new index, timer or store
is added. The 33-header fixture uses three list RPCs and constant page memory.
One admission future lives in the existing bounded task registry until retention
cleanup; one ordinary worker replaces the lost execution and uses existing
TaskRuntime ownership, context and cancellation boundaries. A fresh-interpreter
checkpoint-exit test verifies this worker path through real commands, workspace
CAS and terminal persistence on each selected store; provider frames/prices are
controlled lifecycle fixtures, not scientific or commercial billing evidence.

No worker, timer or model call is added. Initial claim checks perform two record
reads; recovered checks perform three, independent of completed round count.
Recovery retains one predecessor record per successful ownership generation plus
one mutable head; failed attempts add at most the same immutable predecessor.
These are durable authority records, not an evictable cache. Verify process
recovery tests, selected-store competing claims, and existing owner/checkpoint
guards. Scientific quality and remote producer quiescence remain separate proof.

Pending lookup and startup prioritization

Problem: immutable binding discovery waited on settled history before reaching
unfinished work. Decision: keep a reconstructible pending binding namespace,
retained after initial execution claim ACK and retired only after validated
terminal ACK. Index failures never weaken claim, checkpoint or terminal durability.
Retirement deletes only cache pointers; model/tool/checkpoint/ownership and
settlement evidence remain unchanged. A fresh cache-write identity permits rebuild
after deletion; ambiguous write/delete ACKs require exact readback.

Startup visits indexed pointers first, validates their original journal binding
and execution claim, then reconciles all immutable bindings for legacy/missing or
corrupt cache entries. A malformed cache page falls back to original journals.
There is no unproved coverage watermark or cache-miss completion inference.
Rejected: skipping the journal pass, which could strand old or partially indexed
work; deleting durable historical evidence to reduce scan cost. Total background
reconciliation remains linear in retained identities. Only first indexed recovery
latency is independent of settled-history volume, not total bootstrap scan cost.

Measurement: with 2,048 settled historical bindings, a cached active action reaches
the ordinary resume command after five metadata RPCs, before any historical
binding page is read. This is controlled RPC-count evidence, not a wall-clock or
whole-process RSS measurement. Each pass holds at most 16 lookup rows plus scalar
counters. One small pending pointer is added per unsettled identity and removed
after terminal publication; cleanup failure leaves a repairable pointer. No timer,
worker pool or fixed machine concurrency limit is introduced. Verify pending
ACK/cleanup/cache corruption tests and selected-store restart/retirement tests.

Cold workbench unfinished discovery

Problem: browser refresh loses the live task registry and its interrupted task
pointer. Decision: the authenticated unfinished-actions query scans owner-scoped
pending pointers, then original bindings, in 16-row pages. It validates original
scope, settlement and process ownership before projecting public task metadata.
Only proven-dead owners appear interrupted; unknown ownership blocks another
action. Two candidates block selection as ambiguous. Missing or corrupt cache
data falls back to original journals; no private request chunks are loaded.
Rejected: treating an empty cache as completion or granting execution from a
request header. Resume still performs full request, policy and claim validation.

The workspace merges this projection with live tasks and retains unsaved drafts.
The existing Resume control targets the original identity. Discovery creates no
worker, timer or durable record; original reconciliation remains linear in owned
history, with one page and at most two candidate projections held at once. Verify
`test_research_action_discovery.py`, the authenticated resume API suite and cold
cases in `test_research_action_resume_browser.py` on the shipped browser page.

Cold discovery settled-history reads

Problem: original history inspection reread every binding before finding its
terminal settlement. Decision: after the original list row passes schema, exact
owner/key and workspace scope checks, validate terminal identity and result digest
first. Settled entries require no second binding read. Cached pointers still
revalidate their original binding before settlement inspection; unfinished work
retains the existing ownership and request-header checks. Rejected: trusting the
cache as complete or dropping terminal integrity checks to accelerate scanning.

A controlled 2,048-settlement fixture retains the same original interrupted task
and validates every settlement with zero redundant settled-binding RPCs, saving
2,048 reads versus the previous traversal. This is metadata RPC-count evidence,
not wall-clock or RSS evidence. Full reconciliation remains linear in owned
history, with the same 16-row page and two-candidate bounds. No worker, cache,
persistent record or execution authority is added.
