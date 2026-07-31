# HyperFrames Composition Contract (distilled for tofu)

A **composition** is one HTML file that HyperFrames renders frame-by-frame:
the renderer takes a time value and produces pixels. Every frame must be
reproducible from its time alone. Three contracts enforce that.

## 1. Structure contract

- Root `<div>` DIRECTLY in `<body>` (standalone composition — NEVER wrap it
  in `<template>`), carrying:
  - `data-composition-id="<id>"` — unique id (use `main` for single-file scenes)
  - `data-width` / `data-height` — pixel frame size (e.g. 1080×1440 vertical)
  - `data-duration` — seconds, the render duration (== scene duration)
- The root must be a sized box (`width`/`height` in px in CSS too); ancestors
  must have resolved heights or content collapses to the top-left corner.
- Timed content lives in **clips**: elements with `class="clip"`,
  `data-start`, `data-duration`, `data-track-index` (distinct tracks don't
  overlap; same-track clips must not overlap in time).
- A full-screen background goes on a full-bleed CHILD
  (`position:absolute; inset:0`), NEVER on the composition root itself — the
  renderer's frame compositing can drop the root's own background (black frame).
- Every `id` must be unique across the assembled page.

## 2. Animation runtime contract

- GSAP via CDN: `<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>`
- EXACTLY ONE timeline, built SYNCHRONOUSLY at page load:
  ```js
  window.__timelines = window.__timelines || {};
  const tl = gsap.timeline({ paused: true });
  tl.from('#title', { y: 48, opacity: 0, duration: 0.6, ease: 'power3.out' }, 0.2);
  window.__timelines['main'] = tl;   // key === data-composition-id
  ```
- NEVER `tl.play()`; never build timelines inside async/Promise/setTimeout/
  event handlers; never `gsap.set()` clips that start later (use `tl.set(...)`
  at a time ≥ the clip's `data-start`).
- Render duration comes from the root `data-duration`, not the timeline length.

## 3. Determinism bans (hard errors at the static gate)

- No `Date.now()` / `performance.now()` / render-time clocks.
- No unseeded `Math.random()` (seed a PRNG if you need scatter).
- No render-time network fetches for required assets — inline or download
  them into the scene dir first (CDN <script>/<link> at page load is fine).
- No hover/scroll/pointer-driven state (the renderer has no input).
- No `repeat: -1` — compute a finite count:
  `repeat: Math.max(0, Math.floor(duration / cycle) - 1)`.
- Animate only visual properties: opacity, x, y, scale, rotation, color,
  backgroundColor, borderRadius, transforms. NEVER `display`/`visibility`.
- Don't animate the same property of the same element from two timelines.

## 4. Layout contract

- Build the visible END state in static HTML/CSS first, then animate from/to it.
- Layout with padding/flex/grid/max-width; `position:absolute` is for layers
  and decoratives, not main content.
- No `<br>` in body text — let text wrap via `max-width` (a forced break +
  a natural wrap = overlap).
- Transformed elements must be block-level AND sized (a scaled auto-width
  span shows nothing).
- Pulsing/overshooting decoratives (`yoyo`, `back.out`) need clearance at
  their PEAK size, away from `overflow:hidden` edges.
- CJK text renders fine on this host — but keep glyphs inside their
  containers; `inspect` catches spill.

## The static gate will catch (before you burn a render)

`motion_video_check` runs lint + validate + inspect: missing `data-*`,
unregistered/mismatched timelines, overlapping same-track clips, runtime
console errors in headless Chrome, WCAG contrast, text spilling containers
across timeline samples. Fix what it says (each finding has a fix hint);
only render when it's green.
