/* ═══════════════════════════════════════════════════════════════════
   core/cache_stats.js — extracted from core.js (split 2026-05-28)

   IDB-cache cleanup + stats.

   This file is concatenated by lib/js_bundler.py AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Clear the IndexedDB conversation cache.
 * Run from console: clearConvCache()
 * Or programmatically for troubleshooting.
 */
async function clearConvCache() {
  const before = await ConvCache.stats();
  await ConvCache.clear();
  console.log(`[clearConvCache] ✅ Cleared ${before.count} cached conversations`);
  return before.count;
}

/**
 * Show IndexedDB cache statistics.
 * Run from console: convCacheStats()
 */
async function convCacheStats() {
  const s = await ConvCache.stats();
  console.log(`[convCacheStats] available=${s.available}, count=${s.count}`);
  return s;
}

// ── Markdown ──
if (typeof marked !== "undefined") {
  marked.setOptions({ breaks: true });
  /* ★ FIX: marked v12's bundled GFM `del` rule is /^(~~?)(?=[^\s~])(...)\1.../
   * — note the `~~?`, which lets a SINGLE tilde act as strikethrough
   * (e.g. `~约 5 分钟~` → struck-through).  Strict GFM (and GitHub itself)
   * require `~~` only.  LLM output frequently emits single-tilde spans
   * around CJK text or hyphenated tokens (`~WMT 2014 EN-FR~`), which
   * then renders as unintended strikethrough.  Override the tokenizer
   * to demand at least two tildes. */
  try {
    marked.use({
      tokenizer: {
        del(src) {
          const m = /^(~~+)(?=[^\s~])([\s\S]*?[^\s~])\1(?=[^~]|$)/.exec(src);
          if (!m) return false;
          return {
            type: 'del',
            raw: m[0],
            text: m[2],
            tokens: this.lexer.inlineTokens(m[2]),
          };
        },
      },
    });
  } catch (e) {
    console.warn('[Markdown] failed to install strict-del tokenizer:', e);
  }
}
