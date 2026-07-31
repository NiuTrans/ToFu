# Motion Craft — how to make a scene LOOK designed

> The composition contract says what is *legal*. This says what is *good*.
> Distilled in-tree from the upstream HyperFrames knowledge packs (29 atomic
> motion rules / 13 scene blueprints / 20+ frame presets) so the DEFAULT
> authoring path carries it. The full packs are an optional Settings → Skills
> add-on; nothing here may depend on them being installed.

## 0. The one rule that matters

**A centred line of text on a gradient is not a design.** It is the zero-LLM
fallback. If your composition has exactly one text element and one background,
you have not authored a scene — you have retyped the template. Every scene
needs at least: a **hierarchy** (2+ type sizes with different roles), a
**staged reveal** (elements arrive at different times, not all at 0.2s), and
one **supporting graphic** (rule, shape, bar, icon, number, or divider).

## 1. Pick an archetype from the copy, then hold it

Read the narration and choose ONE. Mixing archetypes inside a film reads as
inconsistent; holding one reads as art-directed.

| The beat is… | Archetype | Build it from |
|---|---|---|
| A number / measurement | **Stat card** | Big tabular number + unit + label + bar or ring |
| Two things contrasted | **Split frame** | Two columns, opposing entrances, a divider |
| A claim / hook / punchline | **Kinetic type** | 2-4 short phrases on a shared beat, distinct entrances |
| A process / sequence | **Step chain** | Numbered rows revealed in order, connector line |
| A definition / concept | **Label + subject** | Centre subject, eyebrow label above, caption below |
| A named product/brand | **Lockup** | Inline SVG logo + wordmark, settle then breathe |
| A list of facts | **Stacked rows** | Left-aligned rows, staggered slide-in, hairline rules |
| Silence / transition | **Hold card** | Minimal mark, slow drift — earn the pause |

## 2. Typography hierarchy (the single biggest quality lever)

Three roles, three sizes, never two of the same:

- **Eyebrow / label** — 28-36px, weight 600, `letter-spacing: 2-6px`,
  60% opacity. Sets context in one or two words.
- **Headline** — the hero. 76-132px, weight 800, `letter-spacing: -1 to -2px`
  (tight tracking is what makes big type look designed), `line-height: 1.1-1.25`.
- **Caption / support** — 34-44px, weight 400-500, 75% opacity,
  `line-height: 1.5`.

Rules of thumb:
- **Contrast ratio between levels ≥ 2×.** 120px next to 96px reads as a
  mistake; 120px next to 40px reads as intent.
- **Never centre everything.** Left-aligned copy with a generous left margin
  looks more designed than centred copy, except for single-line hero cards.
- **Numbers use `font-variant-numeric: tabular-nums`** or they jitter while
  counting.
- **One accent colour, used once.** Everything else is white at 3 opacities.

### Fonts — a hard host constraint (verified, do not "fix")

The renderer auto-resolves ONLY: `Inter`, `Roboto`, `Open Sans`, `Lato`,
`Montserrat`, `Poppins`, `Source Sans 3`, `Noto Sans`, `Noto Sans JP`.

**Naming any other family does not get you that family** — it silently falls
back to whatever fontconfig has, and `lint` flags it. There is **no auto-resolved
Simplified-Chinese face**, so CJK is served by the host's fontconfig fallback.
Therefore:

- Latin/display: name `Inter` (or another listed family) and stop.
- **CJK: do NOT name `PingFang SC` / `Noto Sans CJK SC` / `Microsoft YaHei`.**
  Let it fall back. Naming them adds a lint error and changes nothing.
- Expect CJK to render in whatever face the host provides — on this host that
  is a **serif**. So do not build a design whose identity depends on a
  geometric sans for Chinese text; carry identity with layout, colour,
  weight and decoration instead.

## 3. Motion — stage it, don't dump it

**Everything arriving at once is the amateur tell.** Give each element its own
entrance offset:

```js
tl.from('#eyebrow',  { opacity: 0, y: 20, duration: 0.5 }, 0.10);
tl.from('#headline', { opacity: 0, y: 56, duration: 0.7, ease: 'power3.out' }, 0.30);
tl.from('#rule',     { scaleX: 0, transformOrigin: 'left center',
                       duration: 0.6, ease: 'power2.out' }, 0.55);
tl.from('.row',      { opacity: 0, x: -32, duration: 0.5,
                       stagger: 0.12 }, 0.70);
```

- **Stagger sibling elements** (`stagger: 0.08-0.15`). This one property does
  more for perceived quality than any other.
- **Eases carry tone.** `power3.out` = confident arrival; `back.out(1.7)` =
  playful pop; `power2.inOut` = calm; linear = broken.
- **Entrances 0.5-0.8s.** Faster reads as a glitch, slower as a drag.
- **Fill the full duration.** After the last element lands, add a slow drift
  (`scale: 1 → 1.03` over the remaining seconds) so the frame is never frozen.
  Compute finite repeats: `repeat: Math.max(0, Math.floor(dur / cycle) - 1)`.
- **Animate transforms + opacity only** — never `width`/`height`/`display`.
  Substitute uniform `scale` for size changes.

### Composable motion recipes (distilled)

- **Count-up** — tween a numeric proxy, `Math.round` in `onUpdate`; grow font
  size with the value for escalating emphasis.
- **Bar grow** — `transform: scaleY(0) → 1` with
  `transform-origin: bottom center` (grow from the baseline, not the middle),
  staggered; accent the final bar.
- **Progress ring** — SVG circle, measure with `getTotalLength()`, tween
  `stroke-dashoffset` → 0, rotate the stroke `-90deg` so it starts at 12 o'clock.
- **Rule / underline draw** — `scaleX: 0 → 1` with
  `transform-origin: left center`.
- **Icon draw** — `stroke-dasharray`/`stroke-dashoffset` from `getTotalLength()`.
- **Beat slam** — one shared beat array, DISTINCT entrance per phrase
  (scale-slam / side-snap / rise-rotate), then a locked finale.
- **Split tilt** — two cards with opposing `rotationY`, entering from their own
  sides, idling in phase opposition.
- **Breathe / idle** — `sine.inOut` yoyo with a finite repeat count.
- **Camera push** — transform ONE wrapper holding all content:
  `translate(x,y) scale(S)`, counter-translate `T = -offset × S`.
- **Morph swap** — outgoing cluster shrinks + fades while incoming pops with
  `back.out(2)` at the same screen centre.

## 4. Layout & colour

- **Compose with padding/flex/grid.** `position: absolute` is for layers and
  decoratives, not main content.
- **Generous margins**: ≥ 80px from frame edges on a 1080px-wide frame. Cramped
  edges are the second amateur tell.
- **Background is a full-bleed child** (`position:absolute; inset:0`), never on
  the composition root — the compositor can drop the root's own background.
- **Depth without clutter**: a large soft radial glow behind the subject, a
  hairline (1-2px, 12% white) divider, a subtle vignette. Not drop shadows on
  everything.
- **Colour**: one dark base + one accent. Deep blue/near-black bases read
  premium; the accent appears on exactly one element (a number, a bar, a word).
- **Contrast is gated** — body text must clear WCAG or `validate` fails. Keep
  support text ≥ 70% opacity on dark backgrounds.
- **Overshooting decoratives** (`yoyo`, `back.out`) need clearance at PEAK
  size, away from `overflow:hidden` edges — `inspect` catches spill.

### Fill the frame vertically (portrait is 4:3 TALLER than you think)

Margins tell you where content must NOT go; they say nothing about how much
of the frame it should occupy. That gap has a measured cost: across the
authored scenes on this host the mean vertical span is **65%** of frame
height with a **19.5%** dead band at the bottom, and the worst two leave
**34%** and **33%** empty below the content. A 1080×1440 frame with
everything clustered in the middle band reads as a 16:9 slide letterboxed
into a phone — the single most common "this looks unfinished" tell in
portrait.

- **Target 80-90% vertical span** between the top of the first element and
  the bottom of the last. Below ~70% the frame reads as under-filled; 100%
  means you are touching the edges and have lost your margins.
- **Never bottom-dead more than ~15%.** Symmetry is the usual culprit: a
  centred flex column on a tall frame parks everything mid-frame and splits
  the leftover space top and bottom. On portrait, prefer
  `justify-content: space-between` on a full-height column so content
  distributes across the frame instead of pooling in the middle.
- **Give the tall axis structure, not stretch.** Do not scale type up to fill
  space — that breaks the hierarchy in §2. Fill it with *another band*: an
  eyebrow row at the top, the hero block in the upper-middle, a supporting
  row (stat, bars, rows, caption) in the lower third, and a footer hairline
  or index mark near the bottom. Three or four horizontal bands is what makes
  portrait look composed.
- **Anchor something near the bottom edge.** A progress bar, a scene index
  (`03 / 08`), a source line, or a hairline rule at ~88% frame height ends
  the composition deliberately instead of letting it trail off.
- **Full-height wrapper.** The content wrapper should be `height: 100%` with
  explicit padding, not a `min-content` block centred by `align-items`. You
  cannot distribute across space the wrapper does not occupy.

### When the copy does not fit, REWRITE it — never truncate

Density pressure from the section above has one specific failure mode, and it
has already shipped once: a headline was cut to
`硫化物固态电解质取代液态` — the author dropped the final noun (`电解液`) to make
two lines fit, leaving a dangling phrase that ends mid-thought. Every gate
passed it: a truncated string is perfectly well-formed, correctly sized, and
fully contrast-compliant. Only a reader notices it says nothing.

- **A headline must be a complete phrase.** If it will not fit at the size the
  hierarchy requires, **write a shorter complete phrase** — condense the idea,
  drop a modifier, split it across headline + caption. Cutting characters off
  the end is not shortening, it is corrupting.
- **Never end on a dangling modifier, measure word, or half of a compound
  noun.** `取代液态` / `提升了` / `相比于` are not endings.
- **Do not reach for `text-overflow: ellipsis` or `line-clamp` on hero copy.**
  They hide the problem at render time instead of fixing the writing. Clamping
  is for user-supplied text of unknown length; your headline is text you chose.
- **Fit by re-writing first, type scale second, layout third.** Shrinking the
  hero below its role size (§2) trades one visible defect for another.

### Overlapping elements: the occlusion flag goes on the COVERED text

`inspect` reports `Text is hidden beneath an opaque element.` whenever an
opaque element lands on top of text. Its fix hint offers three ways out, and
the third — *"mark intentional layering with `data-layout-allow-occlusion`"* —
does not say WHICH element to mark. It is the covered **text**, or one of its
ancestors. Marking the element that does the covering does nothing.

Measured 2026-07-29, and the reason this section exists: a scene placed two
52px circular `+` / `−` badges over a token-alignment diagram and put
`data-layout-allow-occlusion` on the **badges**. The author had read the hint
and acted on it, spent 5 repair rounds and 53k tokens, and the finding never
cleared — because the audit tests `element.closest('[data-layout-allow-occlusion]')`
where `element` is the text being covered. The badges exempted only their own
`+` / `−` glyphs; the two characters they sat on top of (`好`, covered 15.4%,
and `—`, covered 12.4%) were never exempt and kept failing.

```html
<!-- WRONG: the flag is on the coverer; the text underneath still fails -->
<div class="badge" data-layout-allow-occlusion>+</div>
<span>好</span>

<!-- RIGHT: the flag is on (or above) the text that gets covered -->
<div class="badge">+</div>
<span data-layout-allow-occlusion>好</span>
```

Reach for the flag LAST. A single character clipped by a floating badge is
usually a layout mistake, not an intentional effect, so prefer the other two
exits the hint lists:

- **give the text its own zone** — move the badge into a gutter, or into the
  gap between rows, so nothing overlaps in the first place;
- **raise the text's stacking order** above the covering element when the
  overlap really is the design.

One asymmetry worth knowing: a short label with no whitespace (a single glyph,
`03 / 08`, `+`) is treated as an **atomic label** and flags at ANY coverage,
while running prose only flags past ~15%. So a decorative badge grazing one
character of a headline is a finding even though it looks trivial — that is
deliberate, because a partly-covered single glyph is unreadable in a way a
partly-covered paragraph is not.

## 5. Before you finish

Run `composition_check`. It runs the same three gates the renderer runs
(`lint` = contract + fonts, `validate` = headless-Chrome runtime errors +
contrast, `inspect` = text spilling its container across timeline samples),
plus two the CLI cannot see: **text fidelity** (on-frame copy that corrupts
its source beat) and **vertical fill** (the §4 span / dead-band targets,
measured in a real browser at the END of the timeline). The fill numbers above
are enforced, not advisory — a frame under 60% span or over 28% bottom dead
comes back as a finding you must fix here.
Fix every finding — each comes with a hint. A green gate is the floor, not the
goal: re-read §0 and confirm you have hierarchy, stagger and a supporting
graphic before you stop.
