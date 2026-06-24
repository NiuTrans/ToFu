/* ═══════════════════════════════════════════════════════════════════
   toolset-apply.js — "Apply tool change on next conversation" affordance

   Backs the per-conversation tool-SCHEMA latch (the (B) root fix for
   tools-array prompt-cache breaks). The backend freezes the exact tool
   list a conversation first used and serves it byte-identical every
   round, so a mid-conversation tool toggle (Swarm / Scheduler / Browser
   / …) cannot invalidate the cached prefix. When that happens the `done`
   SSE event carries `toolsetDiverged: true` (+ `toolsetDiff`); we surface
   a small banner above the composer offering:
     • Apply now  → POST /api/v1/conversations/{id}/toolset/apply, which
                    clears the latch so the NEXT round rebuilds the tool
                    list from current toggles (one-time cache rebuild).
     • Dismiss    → keep the cache; the change rides to the next NEW
                    conversation automatically (a fresh conv has no latch).

   ── Conversation-scoped (fix 2026-06-22) ──
   The banner is a single global DOM element, but the divergence state is
   PER-CONVERSATION. We stash it on the conv object (`conv._toolsetDiverged`
   / `conv._toolsetDiff`), only ever render the banner for the ACTIVE conv,
   and re-evaluate on every conversation switch via `syncToolsetBanner()`.
   This stops the banner (and its "Apply now" action, which clears a latch
   by conv id) from leaking onto an unrelated conversation when the user
   switches mid-task.

   Bundled by lib/js_bundler.py (_BUNDLE_FILES). Reads core.js globals
   (activeConvId / conversations) only inside function bodies, so load
   order is flexible.
   ═══════════════════════════════════════════════════════════════════ */

/* Find a conversation object by id (best-effort; returns null if unknown). */
function _toolsetConv(convId) {
  if (!convId || typeof conversations === 'undefined') return null;
  return conversations.find((c) => c.id === convId) || null;
}

/* Render the added/removed tool chips into the banner text line. When no
   diff is available we fall back to the static i18n "pending" message. */
function _renderToolsetDiff(diff) {
  const textEl = document.getElementById('toolsetApplyBannerText');
  if (!textEl) return;
  const added = (diff && Array.isArray(diff.added)) ? diff.added : [];
  const removed = (diff && Array.isArray(diff.removed)) ? diff.removed : [];
  if (!added.length && !removed.length) {
    // No per-tool detail — keep the generic deferred-change message.
    textEl.textContent = (typeof t === 'function')
      ? t('toolset.pending')
      : '工具改动将在新会话生效（保持缓存命中）';
    return;
  }
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const chip = (name, kind) =>
    `<span class="toolset-diff-chip ${kind}">${kind === 'added' ? '+' : '−'} ${esc(name)}</span>`;
  const lead = (typeof t === 'function') ? t('toolset.pendingDiff')
    : '以下工具改动将在新会话生效（保持缓存命中）：';
  const chips = [
    ...added.map((n) => chip(n, 'added')),
    ...removed.map((n) => chip(n, 'removed')),
  ].join('');
  textEl.innerHTML =
    `<span class="toolset-diff-lead">${esc(lead)}</span>` +
    `<span class="toolset-diff-chips">${chips}</span>`;
}

/* Low-level show/hide of the (single, global) banner element. */
function showToolsetApplyBanner(diff) {
  _renderToolsetDiff(diff);
  const el = document.getElementById('toolsetApplyBanner');
  if (el) el.style.display = '';
}

function _hideToolsetBanner() {
  const el = document.getElementById('toolsetApplyBanner');
  if (el) el.style.display = 'none';
}

/* Render the banner to reflect the ACTIVE conversation's latch state.
   Called on conversation switch and whenever divergence state changes. */
function syncToolsetBanner() {
  const conv = _toolsetConv(typeof activeConvId !== 'undefined' ? activeConvId : null);
  if (conv && conv._toolsetDiverged) showToolsetApplyBanner(conv._toolsetDiff);
  else _hideToolsetBanner();
}

/* ── Snap the live toolbar toggles back to the cache-safe (frozen) set ──
 *
 * The latch diff is in tool FUNCTION-name space (lib/tools/registry.py):
 *   • added   = toggled ON  mid-conversation but HELD BACK → not in the
 *               frozen set → cache-safe state is OFF.
 *   • removed = toggled OFF mid-conversation but still FROZEN ON → ran
 *               anyway → cache-safe state is ON.
 * Reverting the composer toggles to that frozen set makes the toolbar
 * honestly reflect the tools actually in effect (instead of showing a
 * held-back divergence) WITHOUT a cache rebuild — the next round's fresh
 * assembly then matches the latch, so `diverged` goes back to False.
 *
 * Each family maps a set of function names to the matching `_apply*UI`
 * setter (which flips the global + button state without re-saving/logging).
 * Mirrors `_CTX_FAMILY_RULES` in info-rail.js; kept self-contained here
 * because that table is private to its IIFE and maps to labels, not
 * setters. Unknown function names have no toggle to revert and are
 * skipped. */
const _SAFE_REVERT_FAMILIES = [
  { match: (n) => n === 'web_search',
    apply: (on) => { if (typeof _applySearchModeUI === 'function') _applySearchModeUI(on ? 'multi' : 'off'); } },
  { match: (n) => n === 'fetch_url',
    apply: () => { /* fetch is always on — not toggleable */ } },
  { match: (n) => String(n).indexOf('browser_') === 0,
    apply: (on) => { if (typeof _applyBrowserUI === 'function') _applyBrowserUI(on); } },
  { match: (n) => String(n).indexOf('desktop_') === 0,
    apply: (on) => { if (typeof _applyDesktopUI === 'function') _applyDesktopUI(on); } },
  { match: (n) => n === 'generate_image',
    apply: (on) => { if (typeof _applyImageGenToolUI === 'function') _applyImageGenToolUI(on); } },
  { match: (n) => n === 'ask_human',
    apply: (on) => { if (typeof _applyHumanGuidanceUI === 'function') _applyHumanGuidanceUI(on); } },
  { match: (n) => ['create_memory', 'update_memory', 'delete_memory', 'merge_memories', 'search_memories'].indexOf(n) !== -1,
    apply: (on) => { if (typeof _applyMemoryUI === 'function') _applyMemoryUI(on); } },
  { match: (n) => ['schedule_create', 'schedule_list', 'schedule_manage', 'await_task', 'timer_create', 'timer_manage'].indexOf(n) !== -1,
    apply: (on) => { if (typeof _applySchedulerUI === 'function') _applySchedulerUI(on); } },
  { match: (n) => n === 'run_command',
    apply: (on) => { if (typeof _applyCodeExecUI === 'function') _applyCodeExecUI(on); } },
  { match: (n) => ['spawn_agents', 'await_agents', 'get_agent_result'].indexOf(n) !== -1,
    apply: (on) => { if (typeof _applySwarmUI === 'function') _applySwarmUI(on); } },
];

function _revertToolsToCacheSafe(diff) {
  if (!diff || typeof diff !== 'object') return false;
  const added = Array.isArray(diff.added) ? diff.added : [];      // → turn OFF
  const removed = Array.isArray(diff.removed) ? diff.removed : []; // → turn ON
  if (!added.length && !removed.length) return false;
  let changed = false;
  const _applyFam = (name, on) => {
    for (const fam of _SAFE_REVERT_FAMILIES) {
      if (fam.match(name)) { fam.apply(on); changed = true; return; }
    }
  };
  for (const n of added) _applyFam(n, false);
  for (const n of removed) _applyFam(n, true);
  if (changed) {
    // Persist the reverted toggles onto the active conv and refresh the
    // submenu count pills so the composer reflects the cache-safe set.
    if (typeof _saveConvToolState === 'function') _saveConvToolState();
    if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  }
  return changed;
}

/* Dismiss = keep the cache, but ALSO revert the composer toggles to the
   frozen (cache-safe) set so they no longer show a held-back divergence.
   Clears the flag on the active conv so the banner doesn't reappear. The
   banner only renders for the active conv, so the live toggle globals being
   reverted here are exactly this conversation's state. */
function dismissToolsetBanner() {
  const conv = _toolsetConv(typeof activeConvId !== 'undefined' ? activeConvId : null);
  if (conv) {
    try { _revertToolsToCacheSafe(conv._toolsetDiff); }
    catch (e) { console.warn('[toolset-apply] revert to cache-safe failed:', e); }
    delete conv._toolsetDiverged;
    delete conv._toolsetDiff;
  }
  _hideToolsetBanner();
}

/* Clear the backend latch for the active conversation so the next round
   re-assembles tools from the current toggles. Captures the conv id up front
   so a mid-await conversation switch can't retarget the wrong conv. */
async function applyToolsetNow() {
  const convId = (typeof activeConvId !== 'undefined' && activeConvId) ? activeConvId : '';
  if (!convId) {
    _hideToolsetBanner();
    return;
  }
  const conv = _toolsetConv(convId);
  try {
    const res = await Api.conversations.applyToolset(convId);
    if (conv) {
      delete conv._toolsetDiverged;
      delete conv._toolsetDiff;
    }
    // Only touch the visible banner if we're still on the same conversation.
    if (activeConvId === convId) _hideToolsetBanner();
    if (res && res.ok) {
      if (typeof showToast === 'function') {
        showToast(typeof t === 'function' ? t('toolset.applied') : '工具改动已应用，下一轮重建缓存', 'success');
      }
    } else if (typeof showToast === 'function') {
      showToast(typeof t === 'function' ? t('toolset.applyFailed') : '应用失败', 'error');
    }
  } catch (e) {
    console.error('[toolset-apply] applyToolset failed:', e);
    if (activeConvId === convId) _hideToolsetBanner();
    if (typeof showToast === 'function') {
      showToast(typeof t === 'function' ? t('toolset.applyFailed') : '应用失败', 'error');
    }
  }
}

/* Called by the SSE pipeline when a done event reports toolsetDiverged.
   `convId` is the conversation the event belongs to; `diff` (optional) is
   {added:[...], removed:[...]} naming the held-back tools. We persist the
   state on the conv object and only render the banner when that conv is
   active — so a background-task conv never pops the banner over a different
   conversation the user is currently viewing. */
function onToolsetDiverged(diverged, convId, diff) {
  const conv = _toolsetConv(convId);
  if (conv) {
    if (diverged) {
      conv._toolsetDiverged = true;
      conv._toolsetDiff = diff || null;
    } else {
      delete conv._toolsetDiverged;
      delete conv._toolsetDiff;
    }
  }
  // Reflect the active conversation only.
  if (convId && typeof activeConvId !== 'undefined' && convId !== activeConvId) return;
  if (diverged) showToolsetApplyBanner(diff);
  else _hideToolsetBanner();
}

if (typeof window !== 'undefined') {
  window.showToolsetApplyBanner = showToolsetApplyBanner;
  window.dismissToolsetBanner = dismissToolsetBanner;
  window.syncToolsetBanner = syncToolsetBanner;
  window.applyToolsetNow = applyToolsetNow;
  window.onToolsetDiverged = onToolsetDiverged;
}
