# Motion-video guidance

## Scope

This package is the motion-video production recipe. Shared durable stage,
recovery, and publication behavior lives in `lib/production/`. Read
`docs/modules/ingest_media.md` and `docs/modules/production.md`.

## Editing rules

- Keep planning, asset acquisition/generation, scene rendering, assembly,
  quality gates, and publication as explicit finite stages.
- Persist validated checkpoint inputs and outputs with version/digest lineage.
  Resume is idempotent; an upstream change invalidates every dependent suffix.
- Treat remote media, model output, codecs, fonts, and subprocess arguments as
  untrusted. Validate type, dimensions, duration, paths, and output existence.
- Bound scenes, resolution, duration, assets, subprocess
  time/memory/output, retries, temporary disk, and retained previews.
- Cancellation terminates child processes and removes reconstructible partials
  without deleting published/durable user artifacts.
- A quality failure is a typed stage outcome, never silently published as a
  successful deliverable.
- Scene revisions (regen/edit) are bounded sub-tasks over a FINISHED job:
  resolve the parent via `engine.resolve_revision_parent` (live task, else
  the job.json disk anchor), spawn via `engine.spawn_scene_revision`, and
  never bypass the no-regression commit — an instruction-driven rewrite that
  grades worse than the user's current frame must be kept as a draft, not
  adopted. An unchanged composition finishes `applied: false` without
  re-rendering.
- Deterministic ops (`set_narration`, `set_subtitle_style`, `swap_asset`)
  own their single-purpose task entries and must honor persisted user
  choices: revision-path re-burns read `subtitle_style.json`, and a
  stock-miss video query may try ONE bounded text-to-video clip
  (`lib/production/t2v_media.py`, `TOFU_T2V_*` env) before becoming a
  finding. Interactive storyboard gates persist their decision
  (`storyboard_gate.json`) — a crash-respawned job never re-asks.

Scene authors, narration and renders start on demand through the production
concurrency owner. Font/craft installation locks protect shared destinations;
failed optional prewarm does not serialize the whole scene batch.

## Verification

Run focused `test_motion_video_*` plan, engine, quality-gate, resume, abort, and
artifact tests. Add production-substrate tests when shared stage semantics are
affected.
