# Research quality and delivery measurement

This is the operational interpretation of the offline comparison owned by
[`lib/research/quality_comparison.py`](../lib/research/quality_comparison.py)
and [`benchmarks/research_quality_compare.py`](../benchmarks/research_quality_compare.py).
Scientific evidence, execution authority and recovery remain in
[Research cycle](RESEARCH_CYCLE.md) and [Evolution](EVOLUTION_DESIGN.md).

## What the existing owners establish

| Research stage | Existing owner | Evidence boundary |
|---|---|---|
| Literature and prior art | `research/literature.py`, `discovery.py`, `survey_hierarchy.py`, `entailment.py` | Source identity and excerpt checks; excerpt coverage is not full-text reading or proof of novelty. |
| Candidate selection | `paper/ideate.py`, `research/evaluation.py`, `revision.py` | Reviewed candidate lineage and unresolved objections; repeated judges do not guarantee independent scientific judgment. |
| Protocol and experiment | `research/draft.py`, `action.py`, `measurements.py` | Frozen criteria, observed execution and numerical consistency; reported measurements alone do not establish authentic input use or competent baselines. |
| Long-horizon progression | `evolution/programs.py`, shared run/executor/reviewer owners | Existing discovery, baseline, alternatives, integration, fresh validation and delivery stages; the research workbench can pass an owned selected idea into a program. |
| Evidence and recovery | Research observation/model journals, workspace CAS and paged history | Preserve observations, counterevidence and execution ownership; lifecycle tests do not measure scientific productivity. |
| Manuscript and figures | `research/manuscript.py`, research manuscript action, `paper/builds/` | Evidence currency, exact sources and retained PDF builds; compilation is not scientific acceptance. |
| Citation and submission checks | `paper/citations.py`, `citation_audit.py`, `checks.py`, `review_dossier.py` | Identifier checks, local source/PDF checks and a reviewer packet; missing coverage remains unknown. |
| External outcome evaluation | `research/quality_comparison.py` | Independently adjudicated, matched receipts; the comparator cannot authenticate the references supplied by its caller. |

These owners share execution, storage and review boundaries. A paper workflow
uses the existing program and action ports; it does not need a second agent
loop. Actual hands-off completion must be measured from runs. A unit test,
synthetic provider trajectory or internal reviewer score is not evidence of
conference-level research quality.

## Quality before throughput

**Problem:** faster generation can conceal weaker claims, missed counterevidence,
failed reproduction or more human repair. A successful pipeline and a compiled
PDF cannot establish that the scientific objective was achieved.

**Decision:** compare identical frozen tasks, literature, model, declared budget
and control/candidate generations. Preserve all planned tasks in `--manifest`
and retain failed, interrupted and abandoned runs. The existing quality metrics
remain separate: supported-claim precision, counterevidence recall, useful
hypothesis rate and reproduction success rate. Actual cost remains visible.
Do not combine these into a model-generated score or infer acceptance probability.

Ground evaluation in externally inspected artifacts: closest prior work and
the claimed difference, competent matched baselines, frozen experiments,
negative results, uncertainty, held-out confirmation, controls/ablations,
claim-to-result links and runnable reproduction instructions. Scope requirements
to the paper's actual contribution. The
[NeurIPS checklist](https://nips.cc/public/guides/PaperChecklist) provides relevant
submission questions; satisfying a checklist does not predict acceptance.
[PaperBench](https://arxiv.org/abs/2504.01848) illustrates evaluation through
concrete reproduction tasks. Reproduction and original discovery remain distinct
outcomes and must not share an invented success rate.

**Alternatives:** internal judge agreement cannot replace external outcome
assessment; selecting only completed or favorable papers hides failure costs;
increasing parallel calls before measuring the critical path can multiply
cost without improving useful output.

## Optional paired efficiency receipts

Each arm can additionally declare `efficiency` with the format below. The
admission vocabulary lives in `quality_comparison.py` (`EFFICIENCY_FORMAT`,
`EFFICIENCY_METRICS` and the shared comparison), not a second statistics engine.

```json
{
  "format": "tofu.research-efficiency/v1",
  "measurement_protocol_digest": "<64 lowercase SHA-256 characters>",
  "evidence_refs": ["<auditable timing and intervention record>"],
  "wall_seconds": 3600,
  "human_active_seconds": 600,
  "human_interventions": 5
}
```

Freeze the measurement procedure before collecting a comparison. Define the
start/end boundary, terminal outcomes, clock source, treatment of restart/wait
time, operator population and intervention-counting rule. For an end-to-end
paper task, start at accepted task intake and end at artifact acceptance or
the recorded unsuccessful terminal outcome. Include model/tool waits and human
waits in elapsed time; measure active human labor separately, including source
selection, protocol correction, environment repair, experiment steering and
manuscript revision. Report aggregate human person-seconds if multiple people
help. The original request is intake, not an unrecorded free intervention.

Both arms must use the same measurement-protocol digest and declare their
system generation digests. Seconds are finite numbers; elapsed time is positive,
active human time is nonnegative, and interventions are nonnegative integers.
Explicit zero is a measurement. Missing effort is unknown. Nonempty evidence
references are required separately from the scientific audit references.
Digests and labels establish declared consistency only; an external audit must
establish actual timing, input use and human involvement.

The report retains arm values, evidence references and treatment-minus-control
differences in their original units. It reports means, ranges and sample standard
errors; one pair has unknown standard error. Lower effort alone is not a win.
`quality_regression_pair_ids` exposes any measured decrease in a quality axis
alongside time savings; this is a descriptive warning, not a significance test.
Different measurement protocols cannot be pooled. No aggregate papers/hour or
autonomous-completion rate is inferred from incomplete or heterogeneous runs.

Legacy inputs retain quality/cost comparisons with efficiency `unavailable`.
Once any efficiency receipt is supplied, missing/invalid measurements and pairs
excluded by the quality checks remain visible, and efficiency is `incomplete`.
Partial descriptive summaries name only the measured subset. CLI exit 3 retains
these diagnostics. Complete supplied comparisons return exit 0, which means
successful analysis, not scientific superiority. Planned coverage still requires
`--manifest`; the manifest does not itself prove preregistration.
`--require-efficiency` additionally returns exit 3 for absent measurements,
so an automated throughput evaluation cannot silently accept legacy quality-only
inputs. The report records that requirement; ordinary legacy comparisons remain
available without it.

```sh
python3 benchmarks/research_quality_compare.py pairs.json \
  --manifest manifest.json --require-efficiency --output report.json
```

The command is offline and read-only with respect to application state. It
does not collect measurements, run experiments, call models or validate the
contents of referenced external artifacts. Reports are reconstructible outputs,
not durable user state; no persistent store, retention policy or worker is added.

## Scientific projection performance

**Problem:** validation repeated the same run's numerical audit for every claim.
Increasing manuscript claims multiplied receipt parsing and statistics work
without adding evidence.

**Decision:** `program._validated_evidence` derives supported references by
outcome once within each call. Every new call revalidates original receipts and
frozen dependencies. Claim-specific digests, unresolved counterevidence, negative
results and archived-receipt checks retain their original authority.

**Alternatives:** a process cache risks certifying changed evidence; skipping
numerical validation or checking only cited favorable runs loses scientific
guards. Neither is needed for this call-local reuse. Performance comparisons
must preserve exact normalized output, readiness and scientific digests and
measure small as well as large claim sets. These component timings exclude
models, experiment execution, storage and full paper delivery.

## Long-history preparation qualification

Measure preparation separately from bounded indexed inspection. The shared
`source_index.PreparedSourceIndex` stages path copies in receipt windows, then
persists only final reachable nodes. Both pending progress and settled-source
preparation reuse it. Independent hexadecimal root branches use the shared
joined production executor; storage I/O never holds the node-cache lock.
The [research lifecycle contract](RESEARCH_CYCLE.md) owns publication,
acknowledgment, scratch lifetime and retained-node behavior.

The synthetic 1,024-receipt fixture in `test_research_index_preparation.py`
alternates one job between running and completed. With a 65-node lookup cache,
the original sequential preparation uses 4,979 puts and 6,242 gets; the prepared
root uses 1,390 puts and 1,390 exact readbacks. Retained node values shrink from
2,483,077 to 276,573 canonical JSON bytes, with identical root hashes, source
counts and job state. These are logical component counts, not physical disk
allocation, storage latency or whole-research performance. Existing persisted
nodes are never deleted by this optimization.

`test_research_indexed_inspection.py::test_long_history_inspection_has_bounded_reads`
still prepares 1,024 new receipts on both real storage authorities before checking
the 1,026-row state with at most three checkpoint/root reads and no page scans.
Run this alongside restart, publication refusal, lost-ACK, source-conflict and
joined-failure tests. Raising the deadline, skipping readback, truncating history
or switching stores cannot qualify the path; storage stalls and teardown failures
must remain visible separately from successful indexed reads.

## Verification

`tests/research/test_research_efficiency_comparison.py` covers paired units,
explicit zero/missing effort, malformed measurements, cohort separation,
quality regressions, finite statistics and CLI incomplete-coverage behavior.
Neighbors are `test_research_quality_comparison.py` and
`test_research_quality_benchmark_cli.py`.

`test_research_claim_validation_work.py` specifies that adding claims does not
repeat numerical verification, while edited measurements, criteria and
counterevidence still invalidate claims. Run the claim, manuscript, numeric-rule,
scientific-stream and paged-history suites for semantic parity. Pure tests
establish software behavior; real research quality and human-effort gains
require separately collected runs and independent evaluation.
