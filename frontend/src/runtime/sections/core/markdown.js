/* ===== migrated source: core/markdown.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/markdown.js — extracted from core.js (split 2026-05-28)

   Markdown rendering pipeline: single-pass DOM transforms, KaTeX, CJK-friendly emphasis, table fence repair, code-block apply buttons, copy helpers.

   This file is concatenated by Vite's module graph AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* Perf: reuse a single temp div for all DOM-based HTML transforms in renderMarkdown.
 * Previously, highlightCodeInHtml + _addApplyButtons + processLongCodeBlocks each created
 * their own temp div and did innerHTML parse → serialize, meaning 3 full DOM round-trips
 * per renderMarkdown call.  Now we parse once, apply all transforms, serialize once. */
let _mdTempDiv = null;
function _getMdTemp() {
  if (!_mdTempDiv) _mdTempDiv = document.createElement('div');
  return _mdTempDiv;
}
/* Perf: skip lang set created once (was re-created every call) */
const _applySkipLangs = new Set(["bash","shell","sh","console","terminal","cmd","powershell","zsh"]);
/* Perf: _singlePassDomTransform — one DOM parse, all transforms, one serialize.
 * Replaces the old 3-pass approach (highlightCodeInHtml → _addApplyButtons → processLongCodeBlocks)
 * which each did innerHTML=html, transform, html=temp.innerHTML — 3 full DOM round-trips.
 * Now: 1 parse + 1 serialize = saves ~3-5ms per renderMarkdown call. */
function _singlePassDomTransform(html) {
  /* Perf: skip DOM parse entirely when no <pre> blocks — saves ~1-3ms for plain text */
  if (!html.includes('<pre')) return html;
  const temp = _getMdTemp();
  temp.innerHTML = html;
  const pres = temp.querySelectorAll('pre');
  const hasHljs = typeof hljs !== 'undefined';
  /* Perf: skip hljs while streaming — highlightAuto on a growing code block
   * re-runs every frame and is the dominant per-token allocator (GC storm).
   * The block is highlighted once at finalizeStreaming (renderMessage path). */
  const _skipHl = typeof window !== 'undefined' && runtimeScope._streamRenderNoHighlight;
  const hasProject = projectState && projectState.active;
  for (let pi = 0; pi < pres.length; pi++) {
    const pre = pres[pi];
    const code = pre.querySelector('code');
    if (!code) continue;
    /* --- Phase 1: Syntax highlighting (was highlightCodeInHtml) --- */
    if (hasHljs && !_skipHl) {
      const lm = code.className.match(/language-(\w+)/);
      const lang = lm ? lm[1] : null;
      const text = code.textContent;
      try {
        if (lang && hljs.getLanguage(lang))
          code.innerHTML = hljs.highlight(text, { language: lang }).value;
        else {
          code.innerHTML = hljs.highlightAuto(text).value;
          // Grammar outside the eager subset: upgrade this block in place once
          // the lazy grammar chunk lands (typeof-guarded for section harnesses).
          if (lang && typeof ensureHljsLanguage !== 'undefined') {
            const pending = code;
            void ensureHljsLanguage(lang).then((ok) => {
              if (!ok || !pending.isConnected) return;
              try {
                pending.innerHTML = hljs.highlight(pending.textContent || '', { language: lang }).value;
              } catch (_) {}
            });
          }
        }
      } catch (_) {}
      code.classList.add('hljs');
    }
    /* --- Phase 2: Apply buttons for project mode (was _addApplyButtons) --- */
    if (hasProject) {
      const hdr = pre.querySelector('.code-header');
      if (hdr) {
        const langSpan = hdr.querySelector('span');
        const lang = langSpan ? langSpan.textContent.trim().toLowerCase() : '';
        if (!_applySkipLangs.has(lang)) {
          const lines = code.textContent.trim().split('\n');
          if (lines.length > 3) {
            const btn = document.createElement('button');
            btn.className = 'apply-btn';
            btn.textContent = 'Apply';
            btn.setAttribute('data-tofu-action', 'openApplyModal(this)');
            hdr.appendChild(btn);
          }
        }
      }
    }
    /* --- Phase 3: Collapse long code blocks (was processLongCodeBlocks) --- */
    const lc = code.textContent.split('\n').length;
    if (lc > 15) {
      pre.classList.add('code-long');
      pre.setAttribute('data-collapsed', 'true');
      const hdr = pre.querySelector('.code-header');
      if (hdr) {
        const sp = hdr.querySelector('span');
        if (sp) sp.textContent += ` \u00b7 ${lc} lines`;
        const btn = document.createElement('button');
        btn.className = 'code-collapse-btn';
        btn.textContent = 'Expand';
        btn.setAttribute('data-tofu-action', 'toggleCodeBlock(this)');
        hdr.insertBefore(btn, hdr.querySelector('.copy-btn'));
      }
    }
  }
  return temp.innerHTML;
}
/* Legacy wrappers (kept for any external callers) */
function highlightCodeInHtml(html) {
  if (typeof hljs === 'undefined') return html;
  const temp = _getMdTemp();
  temp.innerHTML = html;
  temp.querySelectorAll('pre code').forEach((el) => {
    const lm = el.className.match(/language-(\w+)/);
    const lang = lm ? lm[1] : null;
    const text = el.textContent;
    try {
      if (lang && hljs.getLanguage(lang))
        el.innerHTML = hljs.highlight(text, { language: lang }).value;
      else {
        el.innerHTML = hljs.highlightAuto(text).value;
        if (lang && typeof ensureHljsLanguage !== 'undefined') {
          const pending = el;
          void ensureHljsLanguage(lang).then((ok) => {
            if (!ok || !pending.isConnected) return;
            try {
              pending.innerHTML = hljs.highlight(pending.textContent || '', { language: lang }).value;
            } catch (_) {}
          });
        }
      }
    } catch (e) {}
    el.classList.add('hljs');
  });
  return temp.innerHTML;
}
function _addApplyButtons(html) {
  if (!projectState || !projectState.active) return html;
  const temp = _getMdTemp();
  temp.innerHTML = html;
  temp.querySelectorAll("pre").forEach((pre) => {
    const hdr = pre.querySelector(".code-header");
    if (!hdr) return;
    const langSpan = hdr.querySelector("span");
    const lang = langSpan ? langSpan.textContent.trim().toLowerCase() : "";
    if (_applySkipLangs.has(lang)) return;
    const code = pre.querySelector("code");
    if (code) {
      const lines = code.textContent.trim().split("\n");
      if (lines.length <= 3) return;
    }
    const btn = document.createElement("button");
    btn.className = "apply-btn";
    btn.textContent = "Apply";
    btn.setAttribute("data-tofu-action", "openApplyModal(this)");
    hdr.appendChild(btn);
  });
  return temp.innerHTML;
}
function extractFencedBlocks(text, codeStore) {
  const lines = text.split("\n");
  const result = [];
  let i = 0;
  while (i < lines.length) {
    const open = lines[i].match(/^(`{3,}|~{3,})(.*)/);
    if (!open) {
      result.push(lines[i]);
      i++;
      continue;
    }
    const fChar = open[1][0],
      fLen = open[1].length,
      esc = fChar === "`" ? "`" : "~";
    const closeRe = new RegExp("^" + esc + "{" + fLen + ",}\\s*$");
    const innerOpenRe = new RegExp("^" + esc + "{3,}\\S");
    let closeIdx = -1,
      depth = 1;
    for (let j = i + 1; j < lines.length; j++) {
      if (closeRe.test(lines[j])) {
        depth--;
        if (depth === 0) {
          closeIdx = j;
          break;
        }
      } else if (innerOpenRe.test(lines[j])) depth++;
    }
    if (closeIdx === -1) {
      let lastClose = -1;
      for (let j = i + 1; j < lines.length; j++) {
        if (closeRe.test(lines[j])) lastClose = j;
        else if (lastClose !== -1 && innerOpenRe.test(lines[j])) {
          if (j > lastClose + 1) break;
        }
      }
      closeIdx = lastClose;
    }
    if (closeIdx === -1) {
      codeStore.push(lines.slice(i).join("\n"));
      result.push("\x02CODE" + (codeStore.length - 1) + "\x03");
      i = lines.length;
    } else {
      codeStore.push(lines.slice(i, closeIdx + 1).join("\n"));
      result.push("\x02CODE" + (codeStore.length - 1) + "\x03");
      i = closeIdx + 1;
    }
  }
  return result.join("\n");
}
function upgradeFenceIfNeeded(block) {
  const lines = block.split("\n");
  if (lines.length < 2) return block;
  const open = lines[0].match(/^(`{3,}|~{3,})(.*)/);
  if (!open) return block;
  const fChar = open[1][0],
    fLen = open[1].length,
    lang = open[2],
    esc = fChar === "`" ? "`" : "~";
  let maxInner = 0;
  for (let k = 1; k < lines.length - 1; k++) {
    const m = lines[k].match(new RegExp("^(" + esc + "{3,})"));
    if (m) maxInner = Math.max(maxInner, m[1].length);
  }
  if (maxInner >= fLen) {
    const nf = fChar.repeat(maxInner + 1);
    lines[0] = nf + lang;
    lines[lines.length - 1] = nf;
    return lines.join("\n");
  }
  return block;
}
/* ── Markdown render cache ── */
const _mdCache = new Map();
const _MD_CACHE_MAX = 300;
/* Perf: O(1) cache key — length + first/last 64 chars instead of O(n) full-text hash.
 * For 10k+ char responses this avoids hashing every char on each streaming frame.
 * Collision risk is negligible: same length + same head + same tail is near-impossible
 * for different markdown content. */
function _mdCacheKey(text) {
  const n = text.length;
  if (n <= 128) {
    /* Short text: hash all chars — still fast */
    let h = 0;
    for (let i = 0; i < n; i++) {
      h = ((h << 5) - h + text.charCodeAt(i)) | 0;
    }
    return h + ":" + n;
  }
  /* Long text: sample first 64 + last 64 chars */
  let h = 0;
  for (let i = 0; i < 64; i++) {
    h = ((h << 5) - h + text.charCodeAt(i)) | 0;
  }
  for (let i = n - 64; i < n; i++) {
    h = ((h << 5) - h + text.charCodeAt(i)) | 0;
  }
  return h + ":" + n;
}
/* _fixTableExtraPipes — repair Markdown table rows where unescaped pipes
 * inside cell content cause extra columns.
 *
 * LLMs sometimes generate cell content like "82|181" or "82\|181" where the
 * pipe is literal, not a column separator. marked.js treats it as an extra
 * column, causing misaligned ("串列") rendering.
 *
 * Two-phase fix:
 * Phase 1: Replace backslash-escaped pipes (\|) with &#124; in data rows.
 *          This handles the common case where the LLM tried to escape but
 *          marked.js doesn't honor \| inside table cells.
 * Phase 2: If a row still has too many columns, try every contiguous merge
 *          position and pick the best one by scoring against other correct
 *          rows in the same table.
 *
 * Example: 3-column table, LLM wrote "$82|$181" in a cell:
 *   | 目前成本 | $82|$181 |  |     ← 4 cells, expected 3
 *   → | 目前成本 | $82&#124;$181 |  |
 */
function _fixTableExtraPipes(text) {
  const lines = text.split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    if (!/^\s*\|/.test(lines[i])) { out.push(lines[i]); i++; continue; }
    // Collect consecutive table lines
    const tStart = i;
    while (i < lines.length && /^\s*\|/.test(lines[i])) i++;
    const tLines = lines.slice(tStart, i);
    if (tLines.length < 2) { out.push(...tLines); continue; }
    // Find separator row (within first 3 lines)
    let sepIdx = -1, expectedCols = -1;
    for (let s = 0; s < Math.min(tLines.length, 3); s++) {
      const cells = tLines[s].trim().replace(/^\|/, '').replace(/\|$/, '').split('|');
      if (cells.length > 0 && cells.every(c => /^\s*:?-+:?\s*$/.test(c))) {
        sepIdx = s; expectedCols = cells.length; break;
      }
    }
    if (sepIdx === -1 || expectedCols < 1) { out.push(...tLines); continue; }

    // Phase 1: Replace \| with &#124; in non-separator rows.
    // The LLM often writes \| intending a literal pipe, but marked.js
    // doesn't treat \| as an escape inside table rows.
    for (let t = 0; t < tLines.length; t++) {
      if (t !== sepIdx) {
        tLines[t] = tLines[t].replace(/\\\|/g, '&#124;');
      }
    }

    // Build a reference pattern from rows with correct column count.
    // For each column, record how often it's empty across correct rows.
    const emptyRate = new Array(expectedCols).fill(0);
    let correctRowCount = 0;
    for (let t = 0; t < tLines.length; t++) {
      if (t === sepIdx) continue;
      const inner = tLines[t].trim().replace(/^\|/, '').replace(/\|$/, '');
      const cells = inner.split('|');
      if (cells.length === expectedCols) {
        correctRowCount++;
        for (let c = 0; c < expectedCols; c++) {
          if (!cells[c] || !cells[c].trim()) emptyRate[c]++;
        }
      }
    }

    // Process each row
    for (let t = 0; t < tLines.length; t++) {
      if (t === sepIdx) { out.push(tLines[t]); continue; }
      const trimmed = tLines[t].trim();
      const hasLeading = trimmed.startsWith('|');
      const hasTrailing = trimmed.endsWith('|');
      let inner = trimmed;
      if (hasLeading) inner = inner.slice(1);
      if (hasTrailing) inner = inner.slice(0, -1);
      const cells = inner.split('|');
      if (cells.length <= expectedCols) { out.push(tLines[t]); continue; }

      // Phase 2: Too many columns — try each merge position, pick best.
      const excess = cells.length - expectedCols;
      let bestMerge = null, bestScore = -Infinity;
      for (let pos = 0; pos <= cells.length - excess - 1; pos++) {
        const merged = cells.slice(pos, pos + excess + 1);
        const trial = [
          ...cells.slice(0, pos),
          merged.join('&#124;'),
          ...cells.slice(pos + excess + 1)
        ];
        let score = 0;
        // Strong penalty: if the merge window absorbs a genuinely empty cell,
        // it's almost certainly merging across a real column boundary.
        const mergedHasEmpty = merged.some(c => !c || !c.trim());
        if (mergedHasEmpty) score -= 10;
        // Pattern matching against correct rows' empty/non-empty distribution
        for (let c = 0; c < expectedCols; c++) {
          const tEmpty = !trial[c] || !trial[c].trim();
          if (correctRowCount > 0) {
            const colUsuallyEmpty = emptyRate[c] > correctRowCount * 0.5;
            if (colUsuallyEmpty === tEmpty) score += 3;
          }
          // Penalize merged cells that end with backslash (broken \| escape)
          if (!tEmpty && trial[c].trimEnd().endsWith('\\')) score -= 2;
        }
        // Tiebreaker: prefer interior merges — the first and last cells
        // in LLM tables are rarely the ones with accidental pipes.
        if (pos > 0 && pos + excess < cells.length - 1) score += 0.5;
        if (score > bestScore) { bestScore = score; bestMerge = trial; }
      }
      if (bestMerge && bestMerge.length === expectedCols) {
        out.push((hasLeading ? '|' : '') + bestMerge.join('|') + (hasTrailing ? '|' : ''));
      } else {
        out.push(tLines[t]); // fallback: unchanged
      }
    }
  }
  return out.join('\n');
}

/* ── CJK-friendly emphasis preprocessor ──
 * CommonMark 0.31.2's emphasis flanking rules treat CJK punctuation (。，！？（）「」etc.)
 * the same as ASCII punctuation, which breaks emphasis adjacent to CJK punct:
 *
 *   ❌  **这是中文。**接下来   → closing ** is preceded by CJK punct (。) and followed
 *                               by a non-ws/non-punct CJK char, so it is NOT right-flanking.
 *   ❌  从**「重点」**开始     → opening ** is followed by CJK punct 「, so it is NOT left-flanking.
 *
 * The upstream markdown-cjk-friendly spec (tats-u/markdown-cjk-friendly) solves this by
 * redefining flanking in terms of "non-CJK punctuation". Since marked.js has no extension
 * API for the inline emphasis tokenizer, we achieve the equivalent effect by inserting
 * U+200B (ZERO WIDTH SPACE) between delimiter runs and adjacent CJK punctuation.
 *
 * U+200B is Unicode category Cf (Format), so CommonMark treats it as neither whitespace
 * nor punctuation — making it a "non-ws/non-punct character" in the flanking rule. That
 * is exactly what the CJK-friendly spec requires treat CJK punctuation as.
 *
 * This runs while code/math are still placeholders (\x02CODE…\x03, \x02MATH…\x03) so
 * emphasis characters inside code blocks/spans/math are untouched.
 *
 * FIX (2026-05-20): Only insert ZWS when the OPPOSITE side of the delimiter
 * is a letter/number. Without this guard, patterns like `**(Table 3)**：` get
 * a ZWS between `**` and `：`, which paradoxically breaks marked.js's
 * detection because the closing `**` ends up adjacent to ZWS instead of `：`
 * (whose CM-2b rule was actually closing it correctly). The original failing
 * cases (`**这是中文。**接下来`, `从**「重点」**开始`) are characterised by
 * a CJK letter on the opposite side — that's the only situation where the
 * upstream spec demands a flanking fix. */
const _CJK_PUNCT_CLASS =
  '[\u3000-\u303F\uFE30-\uFE4F\uFE50-\uFE6B\uFF01-\uFF0F\uFF1A-\uFF20\uFF3B-\uFF40\uFF5B-\uFF65]';
/* A homogeneous delimiter run (spec terminology). We keep runs homogeneous
 * (*+ | _+ | ~+) — do NOT mix, per CommonMark. */
const _EMPH_RUN_CLASS = '(?:\\*+|_+|~+)';
/* "Letter or number" using Unicode property classes — covers Latin, CJK,
 * Greek, digits, etc. but NOT punctuation/whitespace. */
const _LETTER_OR_NUM = '[\\p{L}\\p{N}]';
/* CJK_PUNCT before EMPH_RUN, with a letter immediately AFTER the run.
 * Matches `」**开` but NOT `)**：`. */
const _CJK_FRIENDLY_BEFORE_RE = new RegExp(
  '(' + _CJK_PUNCT_CLASS + ')(' + _EMPH_RUN_CLASS + ')(?=' + _LETTER_OR_NUM + ')', 'gu');
/* Letter before EMPH_RUN, then CJK_PUNCT.  Avoids JS lookbehind (Safari
 * <16.4) by capturing the letter and re-emitting it.  Matches `点**：` but
 * NOT `)**：`. */
const _CJK_FRIENDLY_AFTER_RE = new RegExp(
  '(' + _LETTER_OR_NUM + ')(' + _EMPH_RUN_CLASS + ')(' + _CJK_PUNCT_CLASS + ')', 'gu');

function _cjkFriendlyPreprocess(text) {
  /* Fast reject: skip unless text contains both a CJK-range char AND a delimiter. */
  if (!/[\u3000-\uFFEF]/.test(text)) return text;
  if (!/[*_~]/.test(text)) return text;
  return text
    .replace(_CJK_FRIENDLY_BEFORE_RE, '$1\u200B$2')
    .replace(_CJK_FRIENDLY_AFTER_RE, '$1$2\u200B$3');
}

function renderMarkdown(text) {
  if (!text) return "";
  if (typeof marked === "undefined" || typeof marked.parse !== "function") {
    return '<pre style="white-space:pre-wrap">' + escapeHtml(text) + "</pre>";
  }
  try {
  /* Perf: streaming renders skip syntax highlighting, so they must NOT be
   * cached — a later non-streaming caller could otherwise get an unhighlighted
   * hit. The full message is re-rendered (with highlight) at finalizeStreaming. */
  const _noHl = typeof window !== 'undefined' && runtimeScope._streamRenderNoHighlight;
  const _ck = _mdCacheKey(text);
  if (!_noHl && _mdCache.has(_ck)) {
    return _mdCache.get(_ck);
  }
  const codeStore = [];
  let p = extractFencedBlocks(text, codeStore);
  const mathStore = [];
  /* NOTE: Backtick spans are intentionally NOT inspected for "LaTeX-looking"
   * content.  In Markdown a backtick span is always code — full stop.  Code
   * is extracted to \x02CODE\x03 placeholders HERE, before any $…$ / \(…\) /
   * $$…$$ / \[…\] math detection runs below, so a `$` (or `\d`, `_foo`, `^{`)
   * inside backticks can never be mis-parsed as math.  An earlier override
   * reached into backticks to re-route "math-shaped" spans to KaTeX; it
   * corrupted ordinary code/regex (e.g. `r'\d+ : \d+'`, `_RG_MATCH_LINE`)
   * into garbled subscripts and was removed.  Models that want typeset math
   * must emit $…$ / \(…\), not backticks.
   * See tests/test_frontend_markdown_backtick_code.py. */
  p = p.replace(/(`[^`\n]+`)/g, (m) => {
    codeStore.push(m);
    return "\x02CODE" + (codeStore.length - 1) + "\x03";
  });
  p = p.replace(/\$\$([\s\S]*?)\$\$/g, (_, t) => {
    mathStore.push({ tex: t.trim(), display: true });
    return "\x02MATH" + (mathStore.length - 1) + "\x03";
  });
  p = p.replace(/\\\[([\s\S]*?)\\\]/g, (_, t) => {
    mathStore.push({ tex: t.trim(), display: true });
    return "\x02MATH" + (mathStore.length - 1) + "\x03";
  });
  // No lookbehind (Safari <16.4 compat) — safe because $$ blocks
  //   are already extracted above, so no $$ sequences remain.
  // FIX: [^$\\\n|] excludes newlines AND pipes — prevents $ in table cells
  //   (e.g. $2.28) from matching across rows/paragraphs/columns and
  //   destroying table structure.  For | in math, use \vert or \mid.
  p = p.replace(/\$(?!\$)((?:[^$\\\n|]|\\.)+?)\$(?!\$)/g, (_, t) => {
    mathStore.push({ tex: t.trim(), display: false });
    return "\x02MATH" + (mathStore.length - 1) + "\x03";
  });
  p = p.replace(/\\\((.*?)\\\)/g, (_, t) => {
    mathStore.push({ tex: t.trim(), display: false });
    return "\x02MATH" + (mathStore.length - 1) + "\x03";
  });
  /* FIX: CJK-friendly emphasis — insert U+200B between emphasis delimiters and
   * adjacent CJK punctuation so marked's stock flanking rules detect emphasis.
   * Runs while code/math are still placeholders → code blocks are untouched. */
  p = _cjkFriendlyPreprocess(p);
  for (let i = 0; i < codeStore.length; i++) {
    p = p
      .split("\x02CODE" + i + "\x03")
      .join(upgradeFenceIfNeeded(codeStore[i]));
  }

  /* FIX: Normalize missing spaces in Markdown structural markers.
   * CommonMark requires a space after #/##/### and after list markers (- * +).
   * LLMs (especially with CJK output) often omit the space:
   *   "###标题"  → should be "### 标题"
   *   "-项目一"  → should be "- 项目一"
   * Without the space, marked.js treats them as plain text.
   * Only applied to line-start patterns to avoid false positives in mid-line text. */
  p = p.replace(/^(\s{0,3}#{1,6})([^\s#])/gm, '$1 $2');
  p = p.replace(/^(\s*[-*+])([^\s\-*+\d])/gm, '$1 $2');
  p = p.replace(/^(\s*\d+\.)([^\s])/gm, '$1 $2');

  /* FIX: Repair Markdown table rows with extra unescaped pipes.
   * LLMs sometimes generate cell content like "82|181" where the pipe is
   * literal, not a column separator.  marked.js interprets it as an extra
   * column, causing misaligned ("串列") rendering.
   *
   * Strategy: detect the separator row (| --- | --- |) to learn the expected
   * column count, then for data rows with too many pipes, merge the excess
   * cells back together using the escaped pipe "&#124;".  */
  p = _fixTableExtraPipes(p);
  let html =
    typeof DOMPurify !== "undefined"
      ? DOMPurify.sanitize(marked.parse(p))
      : marked.parse(p);
  /* Sub-path deploys: rewrite root-anchored API/upload URLs in <img>/<a>
   * tags to include BASE_PATH, so reports / chat embeds load when Tofu is
   * served behind a reverse proxy or cloud-IDE prefix (e.g. /proxy/15000/).
   * apiUrl() handles this for AJAX, but markdown-rendered tags bypass it. */
  if (BASE_PATH) {
    html = html.replace(
      /(<(?:img|a)\b[^>]*?\s(?:src|href)=["'])(\/(?:api|static|uploads)\/)/g,
      '$1' + BASE_PATH + '$2'
    );
  }
  /* Perf: consolidated single-pass DOM transform.
   * Previously this did 3 separate innerHTML parse→serialize round-trips:
   *   highlightCodeInHtml (parse→serialize) → regex → _addApplyButtons (parse→serialize) → processLongCodeBlocks (parse→serialize)
   * Now: one parse, all transforms in-memory, one serialize.  Saves ~3-5ms per renderMarkdown call. */
  html = html.replace(
    /<pre><code class="language-(\w+)[^"]*">/g,
    '<pre><div class="code-header"><span>$1</span><button class="copy-btn" data-tofu-action="copyCode(this)">Copy</button></div><code class="language-$1">',
  );
  html = html.replace(
    /<pre><code class="hljs">/g,
    '<pre><div class="code-header"><span>code</span><button class="copy-btn" data-tofu-action="copyCode(this)">Copy</button></div><code>',
  );
  html = html.replace(
    /<pre><code>/g,
    '<pre><div class="code-header"><span>code</span><button class="copy-btn" data-tofu-action="copyCode(this)">Copy</button></div><code>',
  );
  html = _singlePassDomTransform(html);
  // Wrap <table> elements in a scrollable container with copy button
  html = html.replace(/<table>/g, '<div class="md-table-wrapper"><div class="table-header"><span>table</span><button class="copy-btn" data-tofu-action="copyTableMarkdown(this)">Copy</button></div><table>');
  html = html.replace(/<\/table>/g, '</table></div>');
  if (mathStore.length > 0 && typeof katex !== 'undefined') {
    for (let i = 0; i < mathStore.length; i++) {
      const { tex, display } = mathStore[i];
      let r;
      try {
        r = katex.renderToString(tex, {
          displayMode: display,
          throwOnError: false,
          trust: true,
          strict: false,
        });
      } catch (e) {
        r = `<code class="math-error">${escapeHtml(tex)}</code>`;
      }
      const ph = `\x02MATH${i}\x03`;
      if (display) html = html.split(`<p>${ph}</p>`).join(r);
      html = html.split(ph).join(r);
    }
  } else if (mathStore.length > 0) {
    /* KaTeX not loaded yet — lazy-load and re-render */
    _ensureKatex();
    /* Meanwhile, show raw TeX as fallback */
    for (let i = 0; i < mathStore.length; i++) {
      const { tex, display } = mathStore[i];
      const ph = `\x02MATH${i}\x03`;
      const fallback = `<code class="math-pending">${escapeHtml(tex)}</code>`;
      if (display) html = html.split(`<p>${ph}</p>`).join(fallback);
      html = html.split(ph).join(fallback);
    }
  }
  if (!_noHl) {
    if (_mdCache.size >= _MD_CACHE_MAX) {
      const first = _mdCache.keys().next().value;
      _mdCache.delete(first);
    }
    _mdCache.set(_ck, html);
  }
  return html;
  } catch (e) {
    console.warn('renderMarkdown: marked.parse() failed, using fallback', e);
    return '<pre style="white-space:pre-wrap">' + escapeHtml(text) + "</pre>";
  }
}
function processLongCodeBlocks(html) {
  const temp = _getMdTemp();
  temp.innerHTML = html;
  temp.querySelectorAll("pre").forEach((pre) => {
    const code = pre.querySelector("code");
    if (!code) return;
    const lc = code.textContent.split("\n").length;
    if (lc > 15) {
      pre.classList.add("code-long");
      pre.setAttribute("data-collapsed", "true");
      const hdr = pre.querySelector(".code-header");
      if (hdr) {
        const sp = hdr.querySelector("span");
        if (sp) sp.textContent += ` · ${lc} lines`;
        const btn = document.createElement("button");
        btn.className = "code-collapse-btn";
        btn.textContent = "Expand";
        btn.setAttribute("data-tofu-action", "toggleCodeBlock(this)");
        hdr.insertBefore(btn, hdr.querySelector(".copy-btn"));
      }
    }
  });
  return temp.innerHTML;
}
function toggleCodeBlock(btn) {
  const pre = btn.closest("pre");
  const c = pre.getAttribute("data-collapsed") === "true";
  pre.setAttribute("data-collapsed", c ? "false" : "true");
  btn.textContent = c ? "Collapse" : "Expand";
}
function copyCode(btn) {
  const code = btn.closest("pre").querySelector("code").textContent;
  _safeClipboardWrite(code);
  btn.textContent = "Copied!";
  setTimeout(() => (btn.textContent = "Copy"), 1500);
}
function _cellToMarkdown(cell) {
  /* Reconstruct approximate markdown from a table cell's DOM,
   * preserving bold, italic, and inline code formatting. */
  let md = '';
  cell.childNodes.forEach(node => {
    if (node.nodeType === 3) { // text node
      md += node.textContent;
    } else if (node.nodeType === 1) { // element
      const tag = node.tagName.toLowerCase();
      const inner = node.textContent;
      if (tag === 'strong' || tag === 'b') {
        md += '**' + inner + '**';
      } else if (tag === 'em' || tag === 'i') {
        md += '*' + inner + '*';
      } else if (tag === 'code') {
        md += '`' + inner + '`';
      } else {
        md += inner;
      }
    }
  });
  return md.replace(/\|/g, '\\|').trim();
}
function copyTableMarkdown(btn) {
  const wrapper = btn.closest(".md-table-wrapper");
  const table = wrapper.querySelector("table");
  if (!table) return;
  const rows = table.querySelectorAll("tr");
  if (!rows.length) return;
  const lines = [];
  rows.forEach((tr, i) => {
    const cells = tr.querySelectorAll("th, td");
    const vals = Array.from(cells).map(c => _cellToMarkdown(c));
    lines.push("| " + vals.join(" | ") + " |");
    if (i === 0) {
      lines.push("| " + vals.map(() => "---").join(" | ") + " |");
    }
  });
  _safeClipboardWrite(lines.join("\n"));
  btn.textContent = "Copied!";
  setTimeout(() => (btn.textContent = "Copy"), 1500);
}

