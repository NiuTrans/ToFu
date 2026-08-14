# Tofu Motion Video — Agent Workflow Guide

> Read this BEFORE generating any motion video. It is the tofu-native
> replacement for auto-motion's Codex/Claude-Code two-agent relay: YOU are
> both the storyboarder and the scene author; the `motion_video_*` tools are
> the deterministic render/verify/concat machinery around you.
> For the composition HTML contract read `COMPOSITION_CONTRACT.md` next;
> copy `skeleton.html` as the starting point of every scene.

## Pipeline

1. **Get the transcript** — an SRT the user pasted (write it to a file) or
   one produced upstream (e.g. a podcast/TTS pass). If the user gives a bare
   topic instead of an SRT, write a spoken-style narration script first and
   synthesize TTS before fixing timestamps. A topic video uses measured audio
   duration for its true SRT; never estimate the delivery clock from character
   count.
2. **Storyboard** — split the SRT into scenes. Coarse-grained: merge
   consecutive cues that express one topic / one causal chain / one visual
   concept. Rules:
   - Scenes are contiguous and cover the FULL SRT span (first cue start →
     last cue end). Silence gaps between cues fold into the PREVIOUS scene
     as hold/outro; a long gap may become its own transition scene.
   - Write `scenes.json`: a list of
     `{"id": "scene-001", "start": <sec>, "end": <sec>, "text": "<cue text>",
       "visual": "<one-line visual concept for yourself>"}`
     Times are float seconds with millisecond precision (e.g. `2.833`) —
     never round to integers.
   - **Gate (mandatory)**: call `motion_video_storyboard_check` with the SRT
     path + scenes.json path. Fix and re-check until it passes (contiguity,
     full coverage, duration sum ±0.1s).
3. **Per scene, sequentially** (parallel rendering is a later phase):
   a. Create the scene workdir `scenes/<id>/` and write `index.html` —
      start from `skeleton.html`, set `data-duration` to the scene's exact
      `render_duration_s`, and author the animation per
      `COMPOSITION_CONTRACT.md`. The scene must fill its FULL duration —
      `content_duration_s` is spoken program time; any extra
      `outgoing_handle_s` is a resolved-state visual tail consumed by the next
      overlap transition. Trailing time after the text's point is made stays as hold/outro;
      never cut early. Visual complexity serves the copy; do not gold-plate.
      If the text names a real product/brand you don't know, web-search it
      and download the official SVG/logo (professional logos beat generic
      icons); save assets into the scene dir.
   b. **Static gate (mandatory)**: `motion_video_check` on the scene dir.
      On errors, repair IN PLACE and re-check (each finding comes with a
      fix hint). Max 2 repair rounds; if still failing, tell the user which
      scene and why, and stop — do NOT render a broken scene.
   c. **Render**: `motion_video_render` (quality `standard`; use `draft`
      while iterating on timing/layout, `high` only for the final take).
      Then `motion_video_probe` the output MP4: it must match
      width/height/fps and the scene duration within ±0.15s, and be silent.
4. **Assemble**: `motion_video_concat` with all scene MP4s in order and the
   ordered N-1 `transitions` from `motion-timeline-v1` → `final.mp4`. It
   normalizes mismatched specs, runs real `xfade` for non-zero boundaries,
   and verifies that visual handles were consumed without shortening program
   duration. Never request overlap unless the preceding render includes an
   equal outgoing handle.
5. **Deliver**: report the final path (+ per-scene directory). If anything
   failed, report the failing scene id, the failure category from the tool
   result, and the suggested fix.

## Narration (P2 音画合成 — when the user wants sound)

Do this BETWEEN storyboard (step 2) and scene authoring (step 3):

1. `motion_video_narrate` with the checked scenes.json → per-scene WAVs +
   an alignment manifest. Default `loose` mode: each scene's
   `target_duration = max(srt, audio + tail_pad)` — when a scene's
   `target_duration` EXCEEDS its SRT duration, set that scene's
   `data-duration` to the target before rendering (the extra time renders
   as hold/outro; never trim the audio). `strict` mode keeps the SRT span
   and reports `overflow` instead — shorten the scene text (or raise the
   TTS `speed`) and re-narrate that scene until overflow is ~0.
2. If the tool reports `degraded` (no TTS slot configured), tell the user
   and continue with the SILENT pipeline — never error out.
3. Scene WAVs are already padded to their target content duration. Concatenate
   them with zero extra gap; adding another per-boundary pause makes audio
   longer than video. Then call `motion_video_mux` (video + narration →
   `final.mp4`, loudnorm on). Probe the result: it must HAVE an audio track.

## Film-level BGM / SFX (`motion-audio-v1`)

When the film needs sound design, create one JSON plan beside its local audio
assets and pass `audio_plan_path` plus `scenes_path` to `motion_video_mux`.
Use `GET /api/v1/motion/audio-contract` as the copyable schema example.

- Every asset is local at render time. `license` is mandatory; non-original
  material also needs `source_url`, and CC-BY needs attribution text. The tool
  stages files by SHA-256 and writes both normalized `audio_plan.json` and
  `audio_attribution.txt`.
- Put a cue's internal peak on the visible action with `peak_offset_s`; target
  it by `at_s`, `scene_id` + `progress`/`offset_s`, or a beat number.
- A beat target requires a verified grid. Supply one seconds/null entry per
  expected beat in `beat_observations_s`; the runtime recomputes the metrics.
  `verified=true` is accepted only when residual ≤15ms, match ratio ≥98%,
  mean absolute error <10ms, drift <5ms, and the first beat is valid. Use
  `beat_sync_mode=required` only when
  every real overlap transition must land within three frames of a beat.
- Narration sidechains the BGM automatically; the final mix is limited and
  loudness-normalized. Do not download audio during render and do not invent
  source or license metadata.

## Workdir convention

Put everything under `.tofu/motion_video/<slug>/` in the CURRENT PROJECT
(it is the per-project tofu data dir — hidden, gitignored, survives
re-renders):

```
.tofu/motion_video/<slug>/
  transcription.srt
  scenes.json
  audio_plan.json + audio_attribution.txt (when sound design is enabled)
  audio/assets/<sha-prefix>-<name>
  scenes/scene-001/index.html (+ assets) → scene-001.mp4
  scenes/scene-002/...
  final.mp4
```

## Environment

- First time only (or when a tool reports `env_missing`): call
  `motion_video_env_check` with `install=true` — it auto-installs the pinned
  HyperFrames CLI into the tofu data dir and reports node/ffmpeg/Chrome.
- Rendering is ~3.5× realtime on this class of host (a 10s scene ≈ 35s);
  warn the user that a multi-minute video takes minutes, not seconds.
- Renders are deterministic: same composition → same pixels. If a scene
  looks wrong, fix the HTML and re-render JUST that scene, then re-concat.

## Going deeper (optional)

The full upstream knowledge packs — 29 atomic motion rules, 13 multi-phase
blueprints with runnable examples, and 13 design frame presets — carry
working GSAP code well beyond this guide's summary.

**Which path are you on?**

- **You, the chat agent, driving the `motion_video_*` tools by hand**: the
  packs are installable from Settings → Skills (search "hyperframes"), then
  `load_skill` `hyperframes-motion` / `hyperframes-design` when a scene
  needs real choreography or brand-level design.
- **The automatic engine** (`produce_video` / paper reading mode): its
  per-scene author reaches the SAME corpus with no installation at all — the
  packs are fetched once into the managed motion root and the author is given
  the index in its prompt plus a `craft_reference` tool to read any entry in
  full. `load_skill` is NOT in that loop's toolset, so never write engine
  instructions that assume it.

Either way this guide's contract is enough for clean kinetic-type / stat /
icon scenes; reach for the corpus when the beat needs more.
