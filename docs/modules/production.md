# Production substrate

This domain is the shared execution layer for long-running “prompt to
deliverable” capabilities. It owns job lifecycle, deterministic stage graphs,
checkpoint recovery, deduplication, and shared content/research contracts.
Motion video, slides, long-form reports, research, and paper podcast remain
separate recipes with their own quality rules.

## Ownership

| Concern | Owner |
|---|---|
| Stage graph and checkpoint file | `lib/production/stages.py` |
| Long-job registry, deduplication, retention | `lib/production/runtime.py` |
| Crash-resume manifest and rescan | `lib/production/jobs.py` |
| Media-neutral narrative/source contracts | `lib/production/contracts.py` |
| Time-aware research evidence and gates | `lib/production/research.py` |
| Generic task discovery and streaming | `routes/api_v1/tasks.py`, task runtime |
| Capability recipes | `lib/motion_video/`, `lib/slides/`, `lib/longform/`, `lib/research/`, `lib/paper/podcast_engine/` |
| Binary publication | each capability’s artifact/deliverable service |

`lib.production` is the only public Python entry point for shared production
contracts. A capability imports the specific owner module when it needs a
narrow dependency. Historical capability-local copies and import shims are not
part of the architecture.

## Execution model

1. The capability validates the request and derives a stable deduplication key.
2. `ProductionRuntime` atomically joins existing live work or creates one task.
3. The capability writes a minimal owner-scoped job manifest before spawning.
4. Its recipe invokes `run_stages` with explicit names, gates, retry limits,
   checkpoint versions, and freshness windows.
5. Each successful stage atomically commits one JSON-serializable checkpoint.
6. Capability code publishes a final artifact only after its final quality
   gate passes, then settles the generic task exactly once.
7. Startup rescans manifests still marked running and reconstructs their tasks;
   the stage graph skips only valid complete checkpoints.

Heavy media is referenced by path/artifact identity, never embedded in stage
JSON, task events, or logs. A process exit code alone is not a quality verdict.

## Stage contract

A `Stage` declares:

- a stable semantic name;
- `run(context) -> artifact`;
- an optional deterministic `gate(context, artifact) -> errors`;
- a bounded retry count;
- whether it is resumable;
- an optional freshness TTL and semantic checkpoint version.

When a stage is absent, stale, version-mismatched, corrupt, or fails its gate,
that stage and its dependent suffix are recomputed. Upstream checkpoints that
remain valid are preserved. Cancellation is checked between stages and inside
long-running capability operations.

## Capability boundary

The substrate contains a primitive only when multiple independent recipes need
the same semantics. It does not own:

- video timelines, scene authoring, rendering, subtitles, or muxing;
- slide layout or deck rendering;
- podcast script, TTS, or audio joining rules;
- long-form outline, prose, or document export rules;
- capability-specific binary formats or user-facing artifact projections.

Sharing a filename or similar code shape is not enough to move behavior into
the substrate. The shared contract must have the same lifecycle, failure, and
recovery semantics.

## User experience and failure semantics

- Repeating an identical request joins visible in-flight work instead of
  silently spending twice.
- Progress is expressed as stable task events and semantic stages, not a
  fabricated timer.
- Abort prevents later callbacks from publishing or changing terminal state.
- A gate failure reports the stage and actionable findings; partial output is
  retained for diagnosis/resume but never presented as the finished product.
- A restart resumes from durable manifests/checkpoints when the capability
  declares recovery; it never claims process-memory work survived.
- Missing artifact bytes, malformed manifests, and corrupt checkpoints are
  explicit failures, not empty success responses.

## Invariants

- One shared stage implementation and one long-job lifecycle implementation.
- Dedup claim/create/register is atomic with respect to competing submissions.
- Active dedup keys are never evicted merely to satisfy a capacity target.
- Checkpoints are atomic, complete, JSON-serializable semantic boundaries.
- A changed upstream contract invalidates its dependent checkpoint suffix.
- Generic task routes discover runtimes by their declared `kind`; route code
  does not maintain a parallel hard-coded lifecycle.
- Owner identity and artifact provenance survive every handoff.
- Recipe-specific quality rules do not leak into the horizontal substrate.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| Stage/recovery semantics | `lib/production/stages.py` | resume, suffix invalidation, gate, abort |
| Dedup/retention/task lifecycle | `lib/production/runtime.py` | concurrent claim, terminal pruning, capacity |
| Restart behavior | `lib/production/jobs.py` | corrupt manifest, idempotent rescan, respawn failure |
| Shared content contracts | `contracts.py`, `research.py` | every consuming recipe and finite validation |
| One media recipe | its capability package | quality gate, artifact publication, task settlement |
| Generic task projection | `routes/api_v1/tasks.py` | discovery, events, stream, abort |

## Test map

```bash
pytest -q tests/test_production_substrate.py tests/test_production_runtime.py
pytest -q tests/test_task_registry_discovery.py tests/test_production_lifecycle.py
pytest -q tests/test_motion_video_p4.py tests/test_motion_video_p5.py
pytest -q tests/test_paper_podcast_api.py tests/test_longform_p7.py
```
