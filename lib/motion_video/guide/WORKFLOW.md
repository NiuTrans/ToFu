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
   topic instead of an SRT, write a spoken-style narration script first,
   confirm length/tone with the user, and use that as the scene text source
   (a true SRT with timestamps comes from the TTS pass — P2; for now you may
   estimate timings by narration length and TELL the user they are estimates).
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
      start from `skeleton.html`, set `data-duration` to the EXACT scene
      duration (`end - start`), and author the animation per
      `COMPOSITION_CONTRACT.md`. The scene must fill its FULL duration —
      trailing time after the text's point is made stays as hold/outro;
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
4. **Assemble**: `motion_video_concat` with all scene MP4s in order →
   `final.mp4`. It normalizes mismatched specs automatically and verifies
   the total duration.
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
3. After `motion_video_concat` produced the silent `final_silent.mp4`,
   concatenate the scene WAVs in order (a plain `motion_video_concat` of
   WAVs is NOT supported — use run_command ffmpeg concat or just pass the
   per-scene WAVs to your concat step) and call `motion_video_mux`
   (video + narration → `final.mp4`, loudnorm on). Probe the result: it
   must HAVE an audio track now.

## Workdir convention

Put everything under `.tofu/motion_video/<slug>/` in the CURRENT PROJECT
(it is the per-project tofu data dir — hidden, gitignored, survives
re-renders):

```
.tofu/motion_video/<slug>/
  transcription.srt
  scenes.json
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
  `activate_skill` `hyperframes-motion` / `hyperframes-design` when a scene
  needs real choreography or brand-level design.
- **The automatic engine** (`produce_video` / paper reading mode): its
  per-scene author reaches the SAME corpus with no installation at all — the
  packs are fetched once into the managed motion root and the author is given
  the index in its prompt plus a `craft_reference` tool to read any entry in
  full. `activate_skill` is NOT in that loop's toolset, so never write engine
  instructions that assume it.

Either way this guide's contract is enough for clean kinetic-type / stat /
icon scenes; reach for the corpus when the beat needs more.
