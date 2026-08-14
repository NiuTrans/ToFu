"""Frontend push-dead POLL path — open-conversation equal-count grow adopt.

The objective: in the target environment (Tofu Android WebView behind a VS Code
port-forward tunnel) the ``wss://`` push upgrade is unreliable, so the periodic
POLL (`loadConversationsFromServer`) is the ONLY route that refreshes the OPEN
conversation body without a manual refresh.

## The bug this proves + guards
When the open conv's trailing assistant turn grows IN PLACE (same message count,
content extended — a fill-in / regenerate / continue / autopilot follow-up on
another device), the poll path used to route through
``loadConversationMessages``, whose settled-conv OVERWRITE branch decides
staleness at equal count purely by ``serverUpdatedAt > _cachedUpdatedAt`` (a
wall-clock compare against the CACHE timestamp) with NO content-length
keep-longer fallback. When those timestamps tie (cache written from the same
``updatedAt`` the sidebar just merged, or clock skew) the grown content is
DROPPED → the partial bubble stays stale for the whole push-dead session (the
reported "fills only after several manual refreshes").

The fix flags the equal-count content-grow on the open conv and routes it
through the SAME keep-longer, non-destructive ``_verifyActiveConvFromServer``
the notify path already uses — which adopts a longer trailing turn at equal
count. This test drives the REAL shipped ``loadConversationsFromServer`` under
node with the push tier down (``pushIsConnected`` false, simulating a legacy
server that sent no ``rev`` in the ``?meta=1`` payload) and asserts the grow is
adopted. The biting NEUTER strips
the routing flag so the poll falls back to the count-plus-clock path and the
grow is DROPPED — proving the flag is load-bearing, not incidental.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONV_JS = REPO / "static" / "js" / "core" / "conversations.js"


def _extract_fn(src: str, name: str) -> str:
    """Extract a top-level `[async] function <name>(...) { ... }` by brace match."""
    m = re.search(r"(async\s+)?function %s\s*\(" % re.escape(name), src)
    assert m, f"{name} not found in source"
    i = src.index("{", m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


# The harness drives the REAL loadConversationsFromServer. It stubs every
# collaborator the function body touches, and — critically — provides the REAL
# _verifyActiveConvFromServer keep-longer adopt (so the assertion exercises the
# actual routing target, not a mock). The scenario: push is DOWN, the ?meta=1
# payload carries an advanced updatedAt but NO rev (legacy-server shape), and
# the open conv's single assistant turn grew from "short" → "short + MORE" at
# the SAME message count with the cache timestamp already TIED to the server's.
_HARNESS = r"""
'use strict';
function debugLog(){}
const console2 = console;  // keep real console for the JSON emit
// Module-level vars the real loadConversationsFromServer references (it lives
// in a file with these at module scope; the extracted body needs them present).
let _convMetaEtag = null;
let _lastServerLoadOk = false;
function serverLoadOk(){ return _lastServerLoadOk; }
// ── Server truth: the open conv's trailing assistant turn GREW in place ──
const SERVER_LONG = 'short reply that then grew MORE on another device';
const SERVER_SHORT = 'short reply';   // what the local (stale) copy holds
const CONV_ID = 'c-open';
const NOW = 5000000;

// Legacy ?meta=1 list row: advanced updatedAt, NO rev, equal count.
const META_ROW = { id: CONV_ID, title: 'T', msgCount: 2, msg_count: 2,
  createdAt: 1000, updatedAt: NOW /* advanced */, settings: null };
// The full GET the keep-longer verify fetches: the grown trailing turn.
const FULL_CONV = { id: CONV_ID, title: 'T', updatedAt: NOW, settings: null,
  messages: [ {role:'user', content:'hi'},
              {role:'assistant', content: SERVER_LONG, thinking:'', toolRounds:[]} ] };

// ── Minimal app state ──
let activeConvId = CONV_ID;
const activeStreams = new Map();          // push dead, nothing streaming
let _editingMsgIdx = null;
let conversations = [{
  id: CONV_ID, title: 'T',
  messages: [ {role:'user', content:'hi'},
              {role:'assistant', content: SERVER_SHORT, thinking:'', toolRounds:[]} ],
  _serverMsgCount: 2,
  _needsLoad: false,
  updatedAt: 1000,          // local is behind
  _cachedUpdatedAt: NOW,    // ★ cache TS already TIED to server → count+clock OVERWRITE would NOT fire
  activeTaskId: null,
}];
function getActiveConv(){ return conversations.find(c => c.id === activeConvId); }
function _convSorter(a,b){ return (b.updatedAt||0)-(a.updatedAt||0); }
let _renderChatCalls = 0;
function renderChat(){ _renderChatCalls++; }
/* RENDER_CONTRACT migration: an adopted change now renders via
 * window.ConvView.replaceAll (the bare renderChat seam was removed from
 * _verifyActiveConvFromServer). The counter below is wired to the ConvView
 * seam in the driver — the assertion is unchanged ("must re-render"). */
function renderConversationList(){}
function _restoreConvToolState(){}
function _applySettingsToConv(){}
function _hydrateImageBase64(){}
function saveConversations(){}
function _serverConvCount(sc){ if(!sc) return 0; const v = sc.messageCount!=null?sc.messageCount:(sc.msgCount!=null?sc.msgCount:sc.msg_count); return v||0; }
const ConvCache = { isAvailable(){return false;}, get(){return null;}, put(){}, remove(){}, getAllMeta(){return [];} };
// loadConversationsFromServer now fetches the sidebar list via
// Api.conversations.listMeta({prefetch, headers, signal}) (core split, 2026-07)
// rather than a raw fetch(). Return a Response-shaped envelope wrapping the
// ?meta=1 list ([META_ROW]); the keep-longer verify still uses Api.conversations.get.
const Api = { conversations: {
  listMeta: async () => ({ ok:true, status:200, headers:{ get(){ return null; } },
    json: async () => [ META_ROW ] }),
  get: async () => FULL_CONV,
} };
function apiUrl(u){ return u; }
const AbortSignal = { timeout: () => ({}) };

// Which adopt path did the poll route to? (the crux of concern #2)
let _loadConvMessagesCalled = false;
async function loadConversationMessages(){
  _loadConvMessagesCalled = true;
  /* Faithful stand-in for the OVERWRITE branch's count-plus-clock decision:
   *   equal count + cache TS tied to server → cacheIsStale=false → NO adopt.
   *   (This is exactly the branch the fix bypasses; modelling it lets the
   *    neuter prove the grow is dropped when routing falls back here.) */
  const c = getActiveConv();
  const serverLen = FULL_CONV.messages.length;
  const cacheIsStale = (serverLen !== c.messages.length) || (FULL_CONV.updatedAt > (c._cachedUpdatedAt||0));
  if (cacheIsStale) { c.messages = FULL_CONV.messages.map(m=>({...m})); }
  c._needsLoad = false;
}

__VERIFY_FN__
__LOAD_FN__

(async () => {
  Date.now = () => NOW;
  window = (typeof window==='undefined') ? {} : window;
  window.ConvView = { replaceAll: function(){ _renderChatCalls++; } };
  // Drive the REAL loadConversationsFromServer with the ?meta=1 list.
  await loadConversationsFromServer();
  const ac = getActiveConv();
  const trailing = ac.messages[ac.messages.length-1];
  console2.log(JSON.stringify({
    loadConvMessagesCalled: _loadConvMessagesCalled,
    trailingContent: trailing.content,
    adoptedGrow: trailing.content === SERVER_LONG,
    msgCount: ac.messages.length,
    renderChatCalls: _renderChatCalls,
  }));
})();
"""


def _fetch_stub():
    # loadConversationsFromServer calls fetch(url, {...}); return the meta list.
    return (
        "let window = {};\n"
        "async function fetch(){ return { ok:true, status:200, "
        "headers:{ get(){ return null; } }, "
        "json: async () => [ META_ROW ] }; }\n"
    )


def _run(neuter=False):
    src = CONV_JS.read_text()
    load_fn = _extract_fn(src, "loadConversationsFromServer")
    verify_fn = _extract_fn((REPO / "static" / "js" / "core" / "cross_tab_sync.js").read_text(),
                            "_verifyActiveConvFromServer")
    # The Case-2 terminal-field fill was centralised into ONE shared reducer
    # (core/conv_reducers.js::_mergeTerminalTurnFields, 2026-07-25) which the
    # real _verifyActiveConvFromServer now CALLS — splice it too, else the
    # standalone extraction crashes with ReferenceError before the growth gate.
    helper_fn = _extract_fn((REPO / "static" / "js" / "core" / "conv_reducers.js").read_text(),
                            "_mergeTerminalTurnFields")
    # Same reason, second reducer: _verifyActiveConvFromServer also calls
    # core/conv_reducers.js::_mergeTranslationFields (the server-committed
    # translation adopt, 2026-07-27) BEFORE the growth gate. In the real bundle
    # conv_reducers.js loads first; this standalone extraction must splice it
    # too or the function throws ReferenceError before ever reaching the grow.
    xlate_fn = _extract_fn((REPO / "static" / "js" / "core" / "conv_reducers.js").read_text(),
                           "_mergeTranslationFields")
    # d3f9078e (windowed tail reads) extracted the verify body into
    # _adoptVerifiedServerConv (legacy full-array path — the one this scenario
    # drives, since convWindowParam is absent here so no ?window= is requested)
    # and _adoptTailGrowthFromServer (the shared trailing-turn adoption).
    # Splice BOTH: the standalone extraction references them by bare name and
    # the poll path swallows the ReferenceError inside its resilience
    # try/catch, so a missing splice degrades to a silent no-adopt instead of
    # a loud crash (the exact drift this comment now guards).
    cts_src = (REPO / "static" / "js" / "core" / "cross_tab_sync.js").read_text()
    adopt_fn = _extract_fn(cts_src, "_adoptVerifiedServerConv")
    tail_fn = _extract_fn(cts_src, "_adoptTailGrowthFromServer")
    if neuter:
        # NEUTER: strip the routing flag the fix sets, so the tail can never
        # take the keep-longer verify branch and falls back to the
        # count-plus-clock loadConversationMessages path (which drops the grow).
        load_fn = load_fn.replace("local._contentGrewNeedsVerify = true;",
                                  "local._contentGrewNeedsVerify = false;")
    script = (_fetch_stub() + helper_fn + "\n" + xlate_fn + "\n"
              + adopt_fn + "\n" + tail_fn + "\n" + _HARNESS
              .replace("__VERIFY_FN__", verify_fn)
              .replace("__LOAD_FN__", load_fn))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    import json
    last = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(last)


def test_poll_adopts_equal_count_grow_via_keep_longer_verify():
    """REAL source: push dead + open conv + equal-count trailing-turn grow →
    the poll routes through _verifyActiveConvFromServer and ADOPTS the longer
    content, WITHOUT falling back to the count-plus-clock loadConversationMessages."""
    r = _run()
    assert r["adoptedGrow"] is True, f"the grown trailing turn must be adopted: {r}"
    assert r["trailingContent"].endswith("MORE on another device"), r
    assert r["loadConvMessagesCalled"] is False, \
        "the equal-count grow must NOT route to the count-plus-clock loadConversationMessages"
    assert r["renderChatCalls"] >= 1, "an adopted change must re-render the open conv"


def test_neuter_without_flag_drops_the_grow():
    """NEUTER (biting control): strip the routing flag → the poll falls back to
    loadConversationMessages, whose count-plus-clock OVERWRITE ties (equal count,
    cache TS == server TS) → the grow is DROPPED (bubble stays stale). This is
    the exact "fills only after manual refresh" regression the fix removes."""
    r = _run(neuter=True)
    assert r["loadConvMessagesCalled"] is True, \
        "without the flag the poll must fall back to loadConversationMessages"
    assert r["adoptedGrow"] is False, \
        "the count-plus-clock fallback must DROP the equal-count grow (proves the bug)"
    assert r["trailingContent"] == "short reply", r


if __name__ == "__main__":
    test_poll_adopts_equal_count_grow_via_keep_longer_verify()
    print("PASS adopt-via-keep-longer")
    test_neuter_without_flag_drops_the_grow()
    print("PASS neuter-drops-grow")
    print("ALL GREEN")
