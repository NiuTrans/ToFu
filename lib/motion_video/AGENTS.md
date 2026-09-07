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
- Bound scenes, resolution, duration, assets, concurrent renders, subprocess
  time/memory/output, retries, temporary disk, and retained previews.
- Cancellation terminates child processes and removes reconstructible partials
  without deleting published/durable user artifacts.
- A quality failure is a typed stage outcome, never silently published as a
  successful deliverable.

## Verification

Run focused `test_motion_video_*` plan, engine, quality-gate, resume, abort, and
artifact tests. Add production-substrate tests when shared stage semantics are
affected.
