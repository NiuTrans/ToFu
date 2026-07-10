# JS Bundling Rationale — why tofu uses a no-build-step Python concatenator

> **Status:** reference note (uncommitted, pending owner sign-off).
> **Purpose:** stop the next session from re-litigating "should we just drop
> the bundler / migrate to Vite/webpack." The mechanism is deliberate and, on
> the fundamentals, correct for tofu's constraints. This note records the
> verified evidence so the decision does not have to be rediscovered.

Anchors: `lib/js_bundler.py` (the bundler), `routes/common.py::index_page`
(the strip+inject seam), `server.py:1199-1216` (static cache headers),
`server.py:1751-1771` (TLS / HTTP-2 negotiation).

---

## 1. What tofu actually does (verified, not assumed)

Tofu bundles its frontend, like every serious LLM chat project — but through
an **unusual implementation**: a pure-Python, runtime concatenator with **no
npm / webpack / Vite / Node build step**.

- `lib/js_bundler.py` reads an explicit ordered manifest — **`_BUNDLE_FILES`
  (101 core files) + `_DEFERRED_FILES` (4)** = 105 source modules — and, at
  **server startup** (and on any source-mtime change), does:
  `scan-for-corruption → _minify_js → concatenate → sha256 → write
  bundle-<hash>.js → node --check gate`.
- `_minify_js` is a dependency-free, **line-preserving** comment/whitespace
  strip (mirrors `lib/css_bundler._minify_css`). It never rewrites tokens, so
  there is zero ASI hazard; it can only delete comment/whitespace bytes.
- Two-tier split: a **core** bundle (`bundle-<hash>.js`) for first paint + chat,
  and a **deferred** bundle (`feature-<hash>.js`) lazy-loaded on first use of a
  heavy feature (orchestration, task-mode, paper-reader, image-gen) via
  `static/js/feature-loader.js`. This is a hand-rolled equivalent of
  route-based code-splitting.
- `routes/common.py::index_page` rewrites the served `index.html` on `GET /`,
  stripping the ~100 individual `<script>` tags and injecting the single
  bundle tag (+ the deferred-bundle URL as `window.__FEATURE_BUNDLE_SRC__`).

**Robustness that most CI bundlers do not bother with:** a git-merge-conflict /
NUL-byte corruption scan per source file; a `node --check` syntax gate on the
concatenated result (no-op when node absent); `_CRITICAL_FILES` fail-to-fallback
(a missing i18n/core/api/push/main aborts the bundle → `routes/common.py` serves
individual `<script>` tags so `index.html`'s LoadGuard surfaces the failure);
per-file fail-open minify (a tricky file degrades to raw content, never a blank
app).

### How this compares to the competition (all verified)

| Project | Framework | Bundler | Strategy |
|---|---|---|---|
| LibreChat | React | **Vite** (migrating to Rolldown/Vite 8, PR #13450) | build-time, code-split |
| Open WebUI | SvelteKit | **Vite** | build-time, route chunks, SPA |
| Lobe Chat | Next.js | **webpack** (+ Turbopack) | build-time, route chunking + lazy load |

The universal truth: **everyone bundles** — "ship few files, not many" is still
correct in 2026. Tofu is the odd one out only in *refusing a Node build step*,
which is a stated project value (CLAUDE.md §3.2: "vanilla JS only — no
frameworks, no build step"). A user runs `python server.py`; there is no
`npm install` in the install path. The competitors trade that simplicity for
tree-shaking, scope-hoisting, transpilation, and automatic dependency
resolution — capabilities tofu deliberately forgoes.

---

## 2. Why the bundle is still load-bearing — the "Mac quick fix" is a LIVE path

The bundler was originally created to fix a Mac symptom: on a local install
with no reverse proxy, HTTP/1.1's 6-connections-per-host limit forced ~100 JS
files to download in serial waves (the "App initialization failed" banner — see
`.tofu/skills/macos-mime-loadguard-compat-fixes.md`). The obvious 2026
objection is "HTTP/2 multiplexing removed that limit, so the bundle is
obsolete." **It is not**, for two independent reasons.

### 2a. Tofu is frequently NOT on HTTP/2 (verified in `server.py:1751-1771`)

Tofu only speaks HTTP/2 when it terminates TLS itself
(`_ensure_tls_certs` succeeds → banner `HTTP/2 + HTTP/1.1`). It drops to
**HTTP/1.1** in three real code paths:

1. **Behind a reverse proxy / cloud IDE** — `_detect_reverse_proxy()` true →
   TLS auto-disabled (the proxy may or may not give the browser H2; tofu's own
   socket is H1). Banner: `HTTP/1.1 (proxy provides HTTP/2)`.
2. **`cryptography` not installed** — no self-signed cert → `_ensure_tls_certs`
   returns `(None, None)`. Banner: `HTTP/1.1 (TLS unavailable)`.
3. **`--no-tls` / `TOFU_TLS=0`** — explicit opt-out.

A bare local Mac install with no VS Code forwarding — exactly the origin
scenario — has no proxy H2 and can hit path (2). So the bundle is not dead
justification; it is the active fast-path there.

### 2b. Even WITH HTTP/2, bundling still wins for 105 small files

The literature is consistent that "multiplexing killed bundling" is an
over-read:

- **Rolldown (Vite team), "Why do we still need bundlers?"** — *"HTTP/2 does not
  mean you can stop caring about number of HTTP requests."* Every request still
  carries fixed overhead (headers, TLS, framing); browsers/servers cap ~100
  concurrent streams; and **gzip/brotli is "considerably more efficient when
  performed on bundled code compared to individual modules."**
  (<https://rolldown.rs/in-depth/why-bundlers>)
- **USWDS HTTP/2 Performance Guide** (US-gov design system) — recommends smaller
  files under H2, **but** cites Khan Academy serving 300 individual JS files
  over H2 and seeing *degraded* performance "due to less efficient compression
  over multiple files, and server delays related to reading each file from
  disk." Rule of thumb: **keep files below ~50 per URL.**
  (<https://designsystem.digital.gov/performance/http2/>)
- **SitePoint** and **konnorrogers, "Why we still bundle with HTTP/2 in 2022"**
  — multiplexing makes requests *cheaper*, not *free*; combining still yields
  better compression and fewer per-file server reads.

Tofu has **105 source modules — well over the ~50 threshold** — squarely in the
range where even the "prefer small files under H2" guidance flips back to
"concatenate." The **compression argument is decisive**: one gzip stream over
the whole core compresses far better than 100 independently-gzipped files, and
one HTTP response is one disk read instead of ~100.

---

## 3. Honest cons of this specific mechanism

These are the real costs — the reasons the approach can *feel* wrong even though
it is correct on the fundamentals:

1. **No tree-shaking / scope-hoisting / transpile.** Every byte of every
   function ships whether used or not. A real bundler drops dead code. For
   vanilla JS with few deps this is a small tax, but it is real.
2. **Manual dependency ordering** — the biggest maintenance cost.
   `_BUNDLE_FILES` must be hand-maintained in load order, and forgetting to
   register a new top-level file makes it a **silent no-op** (the trap
   documented in CLAUDE.md §3.2.1; guarded by
   `tests/test_bundle_manifest_parity.py` /
   `tests/test_bundle_corruption_guard.py`). A real bundler resolves order from
   `import` statements automatically.
3. **Coarse cache invalidation** — one content hash for the entire core bundle,
   so any one-line change re-downloads the whole core (see §4 for the measured
   verdict on whether this matters).
4. **Startup-time, on-box work** — bundling runs per deployment at server
   start; a build step would have done it once in CI. (Trivial cost — logged as
   `[Bundle] Built ... in N ms`.)

**Do NOT migrate to Vite/Rollup** unless also willing to adopt a Node toolchain
in the install path — that trades one scoped Python file for a whole build/CI
dependency, against the project's explicit "no build step" value. For a
105-file vanilla-JS app that must run from a bare `python server.py` (including
on a Mac), the concatenator is a defensible, correct design.

---

## 4. Cache invalidation — the measured verdict (not a hand-wave)

**Question:** is whole-core-bundle cache invalidation a problem worth a
volatile-vs-stable core split *now*?

### The cache seam (verified)

- `server.py:1207-1208`: paths containing `/bundle-` (or `/vendor/`) →
  `Cache-Control: public, max-age=31536000, immutable` (1 year). The core
  `bundle-<hash>.js` matches. Because the sha256 hash is in the filename, a
  changed source ⇒ new filename ⇒ guaranteed fresh fetch. **Caching is already
  maximal and correct for the core bundle.**
- `routes/common.py:494`: the injected `index.html` is served
  `private, no-cache`, so a new bundle hash is picked up on the very next page
  load (no stale-HTML lag).
- **The deferred split is the only current mitigation** for churn: it pulls the
  4 heaviest, independently-changing feature modules out of the core so a change
  to them re-downloads 77.6 KB gzip, not the core.

### Current on-disk sizes (measured this session — supersedes the remembered 493 KB)

| Bundle | Raw | Gzip |
|---|---|---|
| core `bundle-3d9e4964.js` (101 files) | 1,866,346 B (1.78 MB) | **465,481 B (454.6 KB)** |
| deferred `feature-1595e5c5.js` (4 files) | 339,479 B (331.5 KB) | **79,505 B (77.6 KB)** |

(Core is *below* the memory's 493 KB because image-gen / paper-reader /
orchestration / task-mode were since moved into the deferred bundle.)

### Verdict: a volatile/stable core split is PREMATURE

- The cost of whole-core invalidation is **one 454.6 KB gzip re-download per
  client, per update**. A finer stable/volatile split only pays off under
  *frequent redeploys to many already-cached clients* — a CDN/SaaS model.
  **Tofu's deployment model is self-hosted: pull an update, restart.** The
  re-download is one-time per client per update, not per-visit.
- A stable/volatile split adds exactly the con we most want to avoid — **more
  manifest ordering to hand-maintain** (con #2) — for a benefit that only
  appears in a deployment shape tofu does not have.
- Prior art in this repo already rejected splitting-for-cache on the same logic:
  `.tofu/skills/i18n-locale-split-rejected-caching-regression.md` (inlining the
  active locale traded a first-visit-only immutable-cached cost for a per-reload
  no-cache cost). And `.tofu/skills/js-bundle-minify-load-perf.md` established
  the bundle dominates **first paint**, not re-download.
- **Cheap lever if churn ever becomes a measured problem:** add *more deferred
  sub-bundles* (group stable-vs-volatile core modules into 2-3 feature bundles),
  reusing the existing `_assemble_bundle` machinery — not a webpack migration.

**Recommendation:** do nothing to the core-split now. Revisit only if update
cadence × already-cached client count makes the per-update re-download a
*measured* cost.

### One real gap found (candidate one-line fix, flagged not applied)

The deferred `feature-<hash>.js` is content-hashed (immutable by construction)
but its path contains `/feature-`, **not** `/bundle-`, so it **misses** the
`server.py:1207` immutable branch and falls through to
`max-age=300, must-revalidate` (it carries no `?v=`). Safe (revalidation is
cheap and the hash guarantees correctness) but suboptimal — it should get the
same 1-year immutable header as the core bundle. Suggested fix: broaden the
substring test at `server.py:1207` to also match `/feature-` (or the shared
`-<8hex>.js` output pattern). Left for owner sign-off.


---

## 5. Should we add a `node` dependency? — the measured decision

**Question (owner, 2026-07-08):** node isn't a big dependency operationally (the
`bootstrap.py` conda-PG auto-install at line 722 is a clean template; `npm ci`
is a one-time step). So — if there's a real benefit — add it. Which benefits are
actually reachable?

### 5a. The ESM-gating fact (decisive)

`static/js` has **zero `export` / `import ... from` statements** (verified by
grep). The frontend is entirely `window.*` globals / IIFEs, with load order
encoded by the hand-ordered `_BUNDLE_FILES` manifest. This determines *which*
bundler benefits are available without a rewrite:

| Bundler benefit | Needs ES modules? | Reachable on tofu today? |
|---|---|---|
| Tree-shaking (drop dead code) | **Yes** (static import graph) | **No — $0** |
| Scope-hoisting | **Yes** | **No — $0** |
| Retire manual `_BUNDLE_FILES` ordering | **Yes** (import graph) | **No — $0** |
| Code-splitting | Yes | Already hand-rolled (core + deferred) |
| **Real minification** (identifier mangling) | **No** — works on globals/IIFE | **Yes — the ONLY reachable win** |

The headline reasons to adopt Vite/webpack (tree-shaking, scope-hoisting,
killing the manifest) **all require converting ~105 files to ES modules** — an
expensive, risky *frontend* refactor, not a dependency install. Adding node buys
none of those on the current code. The one benefit available on global-style
code is stronger minification (esbuild/terser mangle local identifiers; the
current `_minify_js` only strips comments/whitespace line-by-line).

### 5b. Measured minification delta (2026-07-08, node v24 / esbuild 0.28.1 / terser 5.48.0)

Baseline = current on-disk `bundle-502911e9.js` (already `_minify_js`'d).
esbuild/terser applied on top. All three passed `node --check`.

| Variant | Raw | gzip-6 (served) | brotli-11 |
|---|---|---|---|
| baseline (`_minify_js`) | 1,868,722 B | 466,074 B (455.2 KB) | 350,482 B |
| esbuild `--minify` | 1,511,175 B | 411,158 B (401.5 KB) | 315,419 B |
| terser `-c -m` | 1,438,303 B | 398,481 B (389.1 KB) | 301,809 B |

**Reduction over baseline:** esbuild −11.8% gzip-6 (−53.6 KB) / −19.1% raw /
−10.0% brotli; terser −14.5% gzip-6 (−66.0 KB) / −23.0% raw / −13.9% brotli.

### 5c. Verdict — QUALIFIED GO (optional enhancer, esbuild), decision recorded

Pre-stated threshold: ≥15% gzip → go; <8% → no; 8–15% → argued judgment call.
Both land in the **8–15% band** (esbuild ~11.8%, terser ~14.5%, just shy of 15%)
→ neither clears the clean bar; it's a judgment call. **Recommended: GO, but
strictly as an OPTIONAL fail-open enhancer using esbuild.** Rationale:

1. **Near-zero incremental dependency cost** — node is *already* a soft
   dependency: `lib/js_bundler.py::_node_syntax_ok` already shells `node --check`
   and returns OK when node is absent. We reuse that exact fail-open seam; we do
   not *add* node, we *use it more when present*.
2. **A second benefit beyond the wire: parse time** — raw drops ~19–23% (~357–430
   KB less JS to parse/compile pre-boot), which matters most on low-end devices
   incl. the project's Kotlin **Android WebView** client.
3. **esbuild over terser** — terser squeezes ~2.7% more gzip but is slow (JS);
   esbuild is Go, ~10–50× faster — decisive for a *per-startup* bundler.

**Honest caveat:** it does NOT clear a strict ≥15% bar, so this is a go for the
*optional* enhancer only (absent node → unchanged `_minify_js`, byte-identical
to today), **never** a mandate to make node required. Holding a strict ≥15% and
declining is also defensible — the whitespace strip already captures most of the
wire win, and the big prizes stay ESM-gated.

### 5d. Integration — IMPLEMENTED (2026-07-08, uncommitted pending sign-off)

Mirrors the existing optional `node --check` gate exactly:

- **`lib/js_bundler.py`** — `_resolve_esbuild()` finds `node_modules/.bin/esbuild`
  first, then `esbuild` on PATH (deliberately NOT `npx` — that would try to
  download at startup). `_esbuild_minify(src)` pipes the concatenated bundle
  through `esbuild --minify --loader=js`, validates the OUTPUT with the existing
  `_node_syntax_ok` gate, and returns the minified string only if it passes —
  else `None`. `_assemble_bundle` calls it after concat, before hashing (so the
  content-hash always matches the served bytes), and keeps the `_minify_js`
  output when it returns `None`. **Fail-open:** absent/broken esbuild → the
  dependency-free bundle ships, byte-identical to before (a bare
  `python server.py` on a Mac with no node is unaffected).
- **Safety proven empirically:** because the bundle has no `import`/`export`,
  esbuild runs in SCRIPT mode and does NOT rename top-level globals — verified
  that `loadConversation` / `closeSettings` / `renderChat` / `pushSubscribe`
  (names index.html's inline `onclick=` handlers depend on) all survive as
  definitions in the real esbuild output, and `node --check` passes. Only
  function-local identifiers are mangled; no top-level definition is
  tree-shaken. Trade-off: esbuild collapses the per-file `// ═══ name ═══`
  debug headers (the `_minify_js` fallback keeps them).
- **Tests** — `tests/test_js_minify.py`: `_esbuild_minify` fail-open when
  absent; build ignores esbuild when absent; and a skipif-gated engaged-path
  test asserting the enhanced bundle is strictly smaller AND still defines every
  top-level global. The three `_minify_js`-output-shape tests (here + in
  `tests/test_bundle_corruption_guard.py`'s shared `_reset_state`) now pin
  `_resolve_esbuild → None` so they stay deterministic regardless of whether
  esbuild is installed.
- **Dependency plumbing** — `esbuild ^0.28.1` added to `package.json`
  devDependencies + `package-lock.json` regenerated (so `npm ci` stays in sync).
  `install.sh` gained an optional, fail-open Node.js step (after ripgrep/tmux):
  `conda install -c conda-forge --override-channels -y nodejs` then a one-time
  `npm ci` (falls back to `npm install`). Every failure path only WARNs — node
  is never required. `node_modules/` is already gitignored. Not added to
  `bootstrap.py`: its LLM-repair loop only fires on a crash, and missing node
  produces no error, so there is nothing for it to repair.
- **Measured on this box after install:** core bundle raw 1,866,346 → 1,511,306 B,
  gzip-6 466,074 → 411,280 B (matches §5b). One-time `npm ci` persists across
  restarts.
