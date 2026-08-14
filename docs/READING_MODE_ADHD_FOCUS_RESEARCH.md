# Reading Surfaces (Reading Mode + chatinner) — ADHD / Focus Research

> **Status:** Research deliverable; measure fix landed 2026-08-10. Focus/Quick View
> were separately retired by owner decision and must not be restored implicitly.
> Captured 2026-07-08 (extended same day: warm-background lever, BeeLine rejection,
> chatinner analysis). Two reading surfaces:
> - **Reading Mode** = `static/js/paper-reader.js` (DEFERRED bundle) + `.paper-report-*`
>   in `static/styles.css`. Comfort controls: `--reader-measure` (60/68/78ch) +
>   `--reader-font-scale` (0.85–1.3), all-theme inheriting (styles.css:1227-1228).
> - **chatinner** = `.chat-inner` / `.md-content` chat message column
>   (styles.css:317 + :344). Fixed 14.5px body, NO measure/scale control today.
>
> **Scope note:** the two surfaces are kept as SEPARATE sections because they are
> different reading modes — Reading Mode is *sustained* long-form reading; chat is
> *skim/scan* interaction interleaved with UI. The evidence-backed levers transfer
> SELECTIVELY between them (see the transfer table in §5), so do not blur them.

This note exists so the research doesn't evaporate. It records: (1) the evidence
tiers, (2) the "do NOT ship Bionic Reading / BeeLine as flagship" rulings with
independent sources, (3) the verified characters-per-line (CPL) calculation for
Reading Mode, (4) the prioritized reader-mode gaps mapped onto our existing
`--reader-*` / per-theme font system, and (5) the chatinner analysis + its shared,
project-wide measure defect.

---

## 0. TL;DR

- **Do NOT build Bionic Reading as a flagship focus feature.** It is the most-hyped
  ADHD reading technique and the best-debunked — three independent evidence lines
  show no benefit (and a near-significant *negative* speed effect). At most, offer
  it as an opt-in toggle explicitly labeled experimental.
- Our reader is already ~70% aligned with the *strong* evidence (measure cap,
  1.75 line-height, left-aligned, per-theme environments, glossary, WPM bar).
- **Verified finding (pre-fix):** the old "Wide" 860px measure exceeded the
  90-CPL return-sweep threshold at **every** font scale; the old default
  (720 @ scale 1.0) was ~99 CPL. **Fixed 2026-08-10:** presets are now
  font-relative 60/68/78ch, so changing A−/A+ no longer changes CPL.
- Remaining evidence-backed option: an **optional high-legibility font**
  (Atkinson Hyperlegible / Lexend). Focus mode remains research context, not an
  active proposal, because the owner deliberately removed that control.

---

## 1. Evidence tiers

### Tier 1 — Typographic levers (strong evidence, mostly free)
The readability literature converges: the levers that help ADHD-pattern attention
are the SAME ones that help low-vision and dyslexic readers, because all three
depend on making letter identification, word segmentation, and **line tracking**
as cheap as possible.
Source: inclusive-typography primer synthesizing WCAG 2.2 SC 1.4.12 + the
Tinker→Rello→Beier literature —
https://www.disabilityworld.org/articles/inclusive-typography-and-readability/

- **Line length (measure): 60–80 CPL.** Above ~90 CPL the eye loses track on the
  right→left return sweep — a *documented* ADHD/low-vision failure mode.
- **Line height ≥ 1.5** (1.6 preferred). Tight leading → descender/ascender
  interference → line-skipping and re-reading. *(We're at 1.75 — good.)*
- **Left-aligned, ragged-right — never justified.** Justification creates uneven
  word-spaces ("rivers") that disrupt word segmentation. *(Verified: no
  `text-align:justify` anywhere in the reader — baseline correct.)*
- **Paragraph spacing ~2× font size.** A clear break is "a built-in pause" and a
  structural landmark — directly relevant to attention regulation.
- **Body ≥ 16px**; modest letter-spacing (0–0.05em).
- **Warm / reduced-glare background — off-black on off-white, NOT pure #000 on #fff.**
  Warm-tinted reading surfaces measurably speed reading and reduce visual stress:
  - **Rello & Bigham 2017 (ACM ASSETS, n=341, 89 with dyslexia):** warm backgrounds
    (Peach/Orange/Yellow, black text) gave *significantly* faster reading times than
    cool ones (Blue/Green/Blue-Grey); Peach fastest, Blue-Grey ~45% slower. The
    effect is "comparable for both groups" — benefits non-dyslexic readers too.
    https://www.cs.cmu.edu/~jbigham/pubs/pdfs/2017/colors.pdf
  - **Corroboration:** the British Dyslexia Association style guide + an eye-tracking
    study (Rello & Baeza-Yates 2015, n=92) both find **black-on-cream** yields the
    shortest fixation durations (traced to Wilkins' visual-stress work).
    https://dyslexichelp.org/what-colour-paper-is-best-for-dyslexia/
  - **Nuance:** avoid *high* contrast — "high contrast creates so much vibration
    that it diminishes readability" (Perron/Bradford, cited in Rello & Bigham). The
    recommendation is off-black text on an off-white/cream surface, not maximal
    contrast. *(tofu's cream theme is already evidence-aligned — see §4 / §5.)*

**Font choice (strong — but NOT the "dyslexia font" myth):** there is NO "ADHD
font"/"dyslexia font" that beats a well-designed sans-serif in controlled trials.
OpenDyslexic shows *no* objective gain (Rello & Baeza-Yates 2013; Wery &
Diliberto 2017), only subjective preference. What matters: generous x-height,
unambiguous letterforms (I/l/1, 0/O, b/d), open apertures, even stroke weight.
Defensible options: **Atkinson Hyperlegible** (Braille Institute, free/OFL) and
**Lexend** (the one font with real WPM evidence — Shaver-Troup).
Source: https://lexifont.com/blog/best-fonts-for-dyslexia-2026

### Tier 2 — Interaction / pacing levers (plausible mechanism, thinner direct evidence)
Target *attention*, not just legibility; fit our "reading environment" framing.
- **Text chunking / current-paragraph focus** — dim everything but the active
  paragraph/line to kill the "visually overwhelming wall of text" (sound
  mechanism; direct RCT evidence thin).
- **Reading guide / active-line highlight** — addresses line-tracking + regression
  (re-reading), both elevated in ADHD.
- **Progress + reading-time cues** — we already have the EWMA WPM bar; a visible
  remaining-time-per-section supports the "clear finish line" that aids
  task persistence.
- **AVOID RSVP / Spritz** (one-word-at-a-time flashing): eliminating regressions
  HURTS comprehension because re-reading is functional (Schotter et al. 2014,
  trailing-mask paradigm). Same evidence class that sinks Bionic Reading.

### Tier 3 — Content-side (the meta-analysis caveat)
The one ADHD-specific meta-analysis found (Chan, Shero et al. 2023, *J. Attention
Disorders* 27(2):182-200, k=18; ASHA summary
https://apps.asha.org/EvidenceMaps/Articles/ArticleSummary/77429b22-54ce-ed11-8145-005056834e2b )
is about **decoding/phonemic remediation in children** (overall g=0.96), **NOT**
UI presentation for adult readers. Do NOT over-read it — it does not say "reformat
text and adults focus better." But its spirit (structure aids comprehension)
supports content-side levers we can do with the LLM: TL;DR/summary-first, explicit
section scaffolding, key-term glossary (we already have glossary cards), short
paragraphs.

---

## 2. Rejected "focus" gimmicks (do NOT ship as flagship)

### 2a. Bionic Reading — bolding the first half of each word

Three independent lines of evidence agree it does not work:

1. **Peer-reviewed eye-tracking (JEMR).** Bionic font "does not significantly
   change eye movements" — fixation durations, fixation counts, and reading speed
   all statistically unchanged; readers did NOT "auto-complete" words from bolded
   lead letters. https://pmc.ncbi.nlm.nih.gov/articles/PMC12565662/
2. **2025 eye-tracking, *Attention, Perception, & Psychophysics* (Springer).**
   Five bolding conditions; bolding the first half "led to costs relative to
   regular unbolded reading" and "was not found to be beneficial for any specific
   population." https://link.springer.com/article/10.3758/s13414-025-03067-w
3. **Large-N field test (Readwise, n≈1,916 after cleaning).** Readers were 2.6 WPM
   *slower* with Bionic (paired t, two-tail p=0.055 — a near-significant NEGATIVE
   effect), identical comprehension (88% vs 88%).
   https://blog.readwise.io/bionic-reading-results/

**Caveat honestly recorded:** subjective preference is real, and comfort affects
how much someone reads. So Bionic is defensible *only* as an opt-in, clearly
experimental toggle — never the headline attention feature. It also carries a
patent/markup-injection cost (each word wrapped in `<b>` tags).

### 2b. BeeLine Reader — gradient line-coloring (letters fade blue→red per line)

Same fate as Bionic: heavily marketed as a focus aid, but independent controlled
evidence is mixed-to-negative.
- **Koornneef & Kraal 2022 (*Computers in Human Behavior Reports* 6:100197, two
  reading experiments, 2nd/3rd-grade pupils):** BeeLine helped ONLY when reading
  *badly-laid-out* text (long lines, tight leading); on *well-formatted* text
  (short lines, adequate spacing) it made 2nd-graders **slower**, it **hampered
  comprehension** for 3rd-graders, and beginning readers preferred plain black
  font. https://www.sciencedirect.com/science/article/pii/S2451958822000318
- The frequently-cited positive WestEd result is a **vendor-commissioned**
  classroom study, not independent — discount accordingly.
- **Telling interaction:** BeeLine only helps text that is ALREADY typographically
  bad. Fixing the measure/leading (§3, §5) removes the very condition under which
  it showed any benefit. Ship at most as opt-in experimental, never default.

---

## 3. Verified CPL calculation (old pixel presets; defect baseline)

**Method:** `CPL = measure_px / (0.5em × font_px)` with average proportional-sans
glyph advance ≈ 0.5em (Bringhurst/Tinker heuristic; real sans avg ~0.48–0.50em).
Uses the **tofu base 14.5px** as WORST CASE — it is the smallest theme base
(dark/light = 15px), so it yields the HIGHEST CPL / tightest tracking.
`effective_font = 14.5 × scale` ⇒ `CPL = measure_px / (7.25 × scale)`.

| scale | font px | 640 "Narrow" | 720 "Comfortable" | 860 "Wide" |
|------:|--------:|:---:|:---:|:---:|
| 0.85  | 12.3 | 104 !! | 117 !! | 140 !! |
| 0.925 | 13.4 |  95 !! | 107 !! | 128 !! |
| 1.0*  | 14.5 |  88 !  |  99 !! | 119 !! |
| 1.1   | 16.0 |  80 ok |  90 !! | 108 !! |
| 1.2   | 17.4 |  74 OK |  83 !  |  99 !! |
| 1.3   | 18.9 |  68 OK |  76 OK |  91 !! |

`OK` = 60–80 CPL comfort band · `!` = 80–90 (marginal) · `!!` = >90 (documented
return-sweep tracking-loss threshold) · `*` = out-of-the-box default.

**Findings (computed, not guessed):**
- **Wide/860 exceeds 90 CPL at every scale (91–140).** The "likely too wide" guess
  is CONFIRMED and *stronger* than stated: not merely above the 80-CPL band but
  above the 90-CPL return-sweep failure threshold at all six scales — worst at the
  default and smaller sizes that attention-impaired readers are least likely to
  change.
- **The DEFAULT (720 @ scale 1.0) is already ~99 CPL** — above the comfort band
  out of the box. 720 only enters 60–80 at max scale 1.3.
- Only **Narrow/640 at scale ≥1.2** and **Comfortable/720 at scale 1.3** sit
  cleanly in-band.

**Caveats:** container horizontal padding shaves a few CPL off (mildly favorable,
not enough to rescue Wide); dark/light 15px base is marginally lower CPL than this
tofu worst case; **CJK is different** — full-width glyphs advance ~1em, so 720px ≈
49 CJK chars with its own comfort standard. The 60–80 band is Latin-specific.

---

## 4. Prioritized reader-mode gaps (mapped to existing seams)

Ordered by evidence strength × cost. **Implementation status updated 2026-08-10.**

1. ✅ **Re-tune measure presets so the common case is in-band.** Landed as
   60/68/78ch with 68ch default. Font-relative measure preserves CPL across all
   six scale steps; A+ now also disables visibly at its maximum.
2. **Focus mode is retired by product decision.** It was previously prototyped,
   then deliberately removed with Quick View to keep the reader toolbar and
   mental model small. The evidence remains useful, but it is not a request to
   restore those controls; future focus work needs a new explicit owner decision.
3. **Optional high-legibility reader font (Atkinson Hyperlegible or Lexend).**
   Real evidence, free/OFL, slots into the existing per-theme font-var system (the
   `calc(<base>px * var(--reader-font-scale))` chain). Self-hosted static like the
   existing fonts.
4. **Warm reading surface (see Tier 1).** tofu's cream theme is *already*
   evidence-aligned — the action is: (a) verify tofu body text is **off-black**,
   not pure `#000`, on the cream (avoid the high-contrast "vibration"); and
   (b) consider an optional warm/sepia reading surface for the dark and light
   themes' reader containers.
   *Note: for CHAT this is likely already satisfied — the 2026-07-08 "Chatinner*
   *(tofu) typography crispness pass" (JOURNAL) darkened assistant ink to the*
   *`--text-primary:#2E2822` token (13.6:1, WCAG AAA) — the tofu-theme rule tagged*
   *`Ink — warm charcoal` (~styles.css:10688; line drifts, grep the token/comment) —*
   *and set discrete 400/500 weights. Still an explicit check for the paper-reader*
   *containers and the light/dark themes.*
5. **Do NOT build Bionic Reading or BeeLine** as a feature — or if later requested,
   ship either explicitly flagged experimental behind the Focus-mode panel (see §2).

**Conventions for whoever implements:** CSS-var driven like the existing comfort
controls; `paper-reader.js` is in `_DEFERRED_FILES` (verify minified symbols in
newest `static/js/feature-*.js`, not `bundle-*.js`); persist prefs via the
existing `paper_reader_prefs` localStorage key; double-neuter tests
(break/restore) per project convention; thread any new base-size math through ALL
THREE themes or one silently ignores the control.

---

## 5. chatinner — do the levers transfer? (separate surface)

chatinner is `.chat-inner` (`max-width:820px; padding:0 24px`) → 772px column; the
`.message` flexbox reserves a 40px avatar + 12px gap, so `.md-content` renders at
~720px, at a FIXED **14.5px** body font (styles.css:344 — NOT scaled by
`--reader-font-scale`, which exists only on the paper-reader containers).

### chatinner CPL (computed, same method as §3: CPL = px / (0.5em × font_px))

| viewport | column px | font px | CPL | verdict |
|---|---:|---:|---:|---|
| **desktop** (base 820px cap) | ~720 | 14.5 | **~99** | !! over the 60–80 band |
| **tablet** (≤1024px, `max-width:920px`) | ~820 | 14.5 | **~113** | !! well over 90 |
| **phone** (~360px, full-width) | ~320 | 14.5 | **~45** | OK (narrow, not the problem) |

**To-fix defect flagged:** the inline comment at **styles.css:16577** asserts the
tablet 920px measure is "still under the ~90ch upper bound." **This is factually
wrong at 14.5px — it computes to ~113 CPL.** The comment computed `ch` against a
larger assumed font. Fix the comment AND the measure.

### Framing: this is a PROJECT-WIDE typography lever, not reading-mode-only

Both the Reading-Mode default (~99 CPL, §3) and chatinner (~99 desktop / ~113
tablet) run over the comfort band for the SAME reason (measure too wide for the
body font). The measure fix is therefore a shared lever across every prose
surface, not a reading-mode-specific tweak.

### Which levers transfer to chat? (chat = skim/scan, NOT sustained reading)

| Lever | Reading Mode | chatinner | Why |
|---|:---:|:---:|---|
| **Measure ≤ ~80 CPL** | high | **high** | chatinner ~99/~113 CPL — *worse* than reading mode. Real defect. |
| **Warm off-black-on-cream surface** | high | **high** | tofu cream theme already evidence-aligned; verify off-black text. |
| Focus / dim non-active | high | **low–moderate** | chat's unit is a *bubble* (already segments content); a "dim old turns / spotlight latest" variant is the plausible analogue, not per-paragraph dim. |
| Active-line reading ruler | moderate | **low** | rulers suit long uninterrupted prose; chat lines are short + interleaved with UI. |
| High-legibility font option | moderate | **moderate** | a global body-font toggle could apply to BOTH surfaces via the shared `--md-sans` var. |
| Bionic / BeeLine | reject | reject | same evidence (§2). |

**chatinner action items (proposals only, no code):**
1. Reduce the effective chat measure toward ≤~80 CPL — either narrow the column or
   raise the body font; verify against the table above. Note `.chat-inner` width
   is deliberately DECOUPLED from the toolbar width (see the
   `input-toolbar-chat-decoupled-layout` convention) — change the measure, not
   that decoupling.
2. Fix the wrong ~90ch comment at styles.css:16577 (and the tablet measure it
   defends).
3. Verify tofu chat body text is off-black on cream (shared with §4 item 4).
   **Likely already done for chat:** the 2026-07-08 tofu chatinner typography pass
   (JOURNAL) set assistant ink via the `--text-primary:#2E2822` token (13.6:1, AAA)
   — the tofu rule tagged `Ink — warm charcoal` (~styles.css:10688; grep the
   token/comment rather than trusting the line, which drifts) — plus discrete
   400/500 weights. So this item is probably satisfied for chatinner; a future
   implementer should not redo it, only confirm the paper-reader containers and
   the light/dark themes.

---

## 6. Source triangulation summary

Each key claim cross-checked across ≥2 independent origins:
- **Bionic debunk:** PMC peer-review + Springer 2025 eye-tracking + Readwise n≈2k field test.
- **BeeLine debunk:** Koornneef & Kraal 2022 (independent, CHB Reports) + the note that the positive WestEd study is vendor-commissioned.
- **Warm-background lever:** Rello & Bigham 2017 (ACM ASSETS, n=341) + British Dyslexia Association style guide / Rello & Baeza-Yates 2015 eye-tracking (n=92).
- **Typography levers:** disabilityworld/WCAG 2.2 SC 1.4.12 primer + lexifont research guide.
- **Font myth (OpenDyslexic no objective gain):** Rello & Baeza-Yates 2013; Wery & Diliberto 2017 (cited in both typography sources).
- **ADHD intervention meta-analysis:** ASHA evidence-map summary of Chan et al. 2023, *J. Attention Disorders*.
- **chatinner CPL + wrong ~90ch comment:** computed from live CSS (styles.css:317, :344, :16577), owner-verified this session.
