"""Frontend boot first-open seamlessness (ChatGPT-parity).

A returning user must land DIRECTLY in a conversation the moment the page opens
— not stare at the "New Chat" welcome screen for the whole server round-trip on
a slow / flaky tunnel, then watch it snap to their conversation.

Guarded here as SOURCE INVARIANTS (no jsdom — matches the repo's node-free
source-assertion convention for boot wiring the e2e harness can't exercise):

  1. main.js — right after `hydrateSidebarFromCache()` resolves (cache-first
     sidebar paint, BEFORE the server round-trip), boot opens a conversation
     via `loadConversation(...)`:
       • the SPECIFIC last-active conv when its id is known AND in the cache
         (in-tab reload, or a cold open whose localStorage mirror resolves);
       • else, on a COLD open with NO known id, the MOST-RECENT cached conv
         (`conversations[0]` — hydrate sorts recency-first via _convSorter);
       • gated on `!activeConvId` (never yank a user who already navigated).

  2. Durable restore id — sessionStorage is per-tab and dies on a browser
     close, so `_restoredConvId` also falls back to a localStorage mirror
     (`tofu_lastActiveConvId`) written on every leave (visibilitychange→hidden /
     pagehide). This is what makes a genuine COLD open restore the SPECIFIC
     last conversation, matching ChatGPT.

  3. main_init_tasks.js — `_ensureNewest()` must NOT repaint over an in-flight
     first-open skeleton (`_initialSwitchLoad` + `_needsLoad` + empty messages),
     which would flash the generic "Loading conversation…" welcome over it.
"""
import re
from pathlib import Path

from tests._runtime_sections import runtime_section_path

REPO = Path(__file__).resolve().parent.parent
MAIN_JS = Path(runtime_section_path('main.js'))
INIT_JS = Path(runtime_section_path('main/main_init_tasks.js'))


def _strip_js_comments(src: str) -> str:
    """Remove /* block */ and // line comments so a substring assertion bites on
    EXECUTABLE code, not comment prose. (Round-1/round-4 lesson: a bare
    substring like "conversations[0]" also matches the explanatory comment next
    to the statement, so the guard passes even when the statement is deleted.)

    Delegates to the SINGLE shared implementation (charter #24). MEASURED on the
    two files this guard scans: the set of surviving code identifiers is
    IDENTICAL, so every substring assertion here keeps its verdict. The swap
    also fixes a latent defect — the local regexes DELETED comment lines rather
    than blanking them (main.js 1519 -> 1162, main_init_tasks.js 584 -> 431), so
    any future line-number reporting would have been badly off; the shared pass
    returns the exact source line count.
    """
    from tests._source_scan import strip_comments
    return strip_comments(src, lang='js', inline=True)


def _hydrate_then_block(src: str, *, strip_comments: bool = False) -> str:
    m = re.search(r"hydrateSidebarFromCache\(\)\s*\.then\(\s*\(\)\s*=>\s*\{([\s\S]*?)\}\)\s*\.catch", src)
    assert m, "hydrateSidebarFromCache().then(...).catch() block not found"
    block = m.group(1)
    return _strip_js_comments(block) if strip_comments else block


def test_boot_opens_specific_last_conv_when_known_and_cached():
    """The specific last-active conv is opened instantly when its id is known
    AND present in the cache-hydrated list — inside the hydrate .then, gated on
    !activeConvId, by identity match (not a positional guess)."""
    block = _hydrate_then_block(MAIN_JS.read_text())
    assert "!activeConvId" in block, "early-restore must be gated on !activeConvId"
    assert "_restoredConvId" in block and "conversations.find(c => c.id === _restoredConvId)" in block, \
        "must verify the specific restored id exists in the cache-painted list by identity"
    assert "loadConversation(" in block, "must open a conversation from the cache path"


def test_cold_open_falls_back_to_most_recent_cached_conv():
    """COLD open with NO known id (mirror empty / cleared) must open the
    most-recent cached conversation (conversations[0]) so the user never sees
    the welcome-flash → snap. Gated on !_restoredConvId so it never overrides a
    known specific id, and on a non-empty cache."""
    # Comment-stripped so the assertion bites on the EXECUTABLE assignment, not
    # the explanatory comment (which also mentions conversations[0]).
    code = _hydrate_then_block(MAIN_JS.read_text(), strip_comments=True)
    assert re.search(r"_target\s*=\s*conversations\[0\]\.id", code), \
        "cold open must ASSIGN the most-recent cached conv (_target = conversations[0].id) — " \
        "the executable statement, not just a comment mention"
    assert "!_restoredConvId" in code and "conversations.length > 0" in code, \
        "cold-open fallback must be gated on no-known-id AND a non-empty cache"
    # And the resolved target is actually opened.
    assert re.search(r"if\s*\(\s*_target\s*\)\s*loadConversation\(\s*_target\s*\)", code), \
        "the resolved cold-open _target must be passed to loadConversation"


def test_restore_id_has_durable_localstorage_fallback():
    """_restoredConvId must fall back to a localStorage mirror so a genuine
    browser-restart cold open restores the SPECIFIC last conversation (not just
    the newest). sessionStorage alone is per-tab and dies on close."""
    src = MAIN_JS.read_text()
    m = re.search(r"const _restoredConvId\s*=([\s\S]*?);", src)
    assert m, "_restoredConvId declaration not found"
    decl = m.group(1)
    assert "sessionStorage.getItem('tofu_activeConvId')" in decl, "must still prefer sessionStorage"
    assert "localStorage.getItem('tofu_lastActiveConvId')" in decl, \
        "must fall back to the localStorage mirror for a browser-restart cold open"


def test_last_active_conv_mirrored_to_localstorage_on_leave():
    """The active conv id is mirrored into localStorage on a leave signal
    (visibilitychange→hidden + pagehide), guarded on a non-null activeConvId so
    exiting on a blank new chat never clobbers the last real conversation."""
    src = MAIN_JS.read_text()
    assert "localStorage.setItem('tofu_lastActiveConvId', activeConvId)" in src, \
        "must mirror activeConvId into localStorage for browser-restart restore"
    # Guarded on a truthy activeConvId (don't persist a null / blank new chat).
    assert re.search(r"if \(activeConvId\)\s*localStorage\.setItem\('tofu_lastActiveConvId'", src), \
        "the mirror write must be guarded on a non-null activeConvId"
    # Registered on both a hidden-visibility signal and pagehide.
    assert "visibilitychange" in src and "'hidden'" in src, "must persist on visibilitychange→hidden"
    assert "'pagehide'" in src, "must persist on pagehide (desktop tab close)"


def test_ensure_newest_preserves_inflight_skeleton():
    """_ensureNewest must skip its full repaint when the active conv is still
    mid first-open load (skeleton painted): _initialSwitchLoad + _needsLoad +
    empty messages. Otherwise it repaints the generic loading welcome over the
    skeleton (downgrade flash). The repaint call itself moved
    renderChat → ConvView.replaceAll (the ConvView fold) — the GUARD is what's
    pinned, not the retired callee name."""
    src = INIT_JS.read_text()
    m = re.search(r"function _ensureNewest\s*\(\)\s*\{([\s\S]*?)\n\}", src)
    assert m, "_ensureNewest not found"
    body = m.group(1)
    assert re.search(
        r"_initialSwitchLoad[\s\S]{0,80}_needsLoad[\s\S]{0,80}messages\.length\s*===\s*0[\s\S]{0,80}ConvView\.replaceAll\(c\.id\)",
        body,
    ), "_ensureNewest must guard the full repaint (ConvView.replaceAll) against an in-flight first-open skeleton"


if __name__ == "__main__":
    test_boot_opens_specific_last_conv_when_known_and_cached(); print("PASS specific")
    test_cold_open_falls_back_to_most_recent_cached_conv(); print("PASS cold-open")
    test_restore_id_has_durable_localstorage_fallback(); print("PASS durable-id")
    test_last_active_conv_mirrored_to_localstorage_on_leave(); print("PASS mirror")
    test_ensure_newest_preserves_inflight_skeleton(); print("PASS skeleton-guard")
    print("ALL GREEN")
