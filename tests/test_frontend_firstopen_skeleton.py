"""Frontend first-open skeleton (epic ②).

Opening a not-yet-loaded (`_needsLoad`) conversation must paint an INSTANT
placeholder — title + N shimmer bubbles built from the mirror's `msgCount`
(_serverMsgCount) — instead of blanking / freezing the previous conv under the
reader during the up-to-15s server round-trip on a flaky tunnel. The real
messages then replace it zero-CLS.

Runs the REAL shipped `renderSkeletonChat` body (static/js/ui/chat_render.js)
under a minimal DOM stub. Proves:
  • it paints `min(msgCount,6)` `.message` bubbles as DIRECT children of
    #chatInner, matching the real renderMessage() structure (avatar +
    message-content>message-header>message-role, message-body) so the swap is
    zero-CLS;
  • bubbles are keyed `skeleton-msg-*` (NEVER `msg-*`) so renderChat's surgical
    `[id^="msg-"]` probe treats the first real render as a full wipe;
  • a small cap bounds the DOM even for a huge msgCount;
NEUTER: a body that early-returns paints nothing (the skeleton is load-bearing).
"""
import re
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path

REPO = Path(__file__).resolve().parent.parent
CR_JS = Path(runtime_section_path("ui/chat_render.js"))
pytestmark = pytest.mark.unit


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"function %s\s*\(" % re.escape(name), src)
    assert m, f"{name} not found"
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


# Minimal DOM stub: a single #chatInner element whose innerHTML we capture as a
# string and query with cheap regex (no jsdom dependency — matches the repo's
# no-jsdom node-harness convention for these render helpers).
_HARNESS = r"""
'use strict';
function t(k){ return k; }              // i18n passthrough → _skT falls back to English
function escapeHtml(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

let _innerHTML = '';
const _chatInner = {
  set innerHTML(v){ _innerHTML = v; },
  get innerHTML(){ return _innerHTML; },
  setAttribute(){}, getAttribute(){ return null; },
};
global.document = {
  getElementById: (id) => (id === 'chatInner' ? _chatInner : null),
};

__FN__

const ret = renderSkeletonChat(__CONV__, __COUNT__);
// Count bubbles + probe keying / structure from the captured innerHTML.
const html = _innerHTML;
const bubbleCount = (html.match(/class="message /g) || []).length;
const skeletonKeyed = (html.match(/id="skeleton-msg-\d+"/g) || []).length;
const realMsgKeyed = (html.match(/id="msg-\d+"/g) || []).length;
const hasAvatar = /class="message-avatar"/.test(html);
const hasContent = /class="message-content"/.test(html);
const hasHeaderRole = /class="message-header"[\s\S]*?class="message-role"/.test(html);
const hasBody = /class="message-body"/.test(html);
const hasShimmerLine = /class="chat-sk-line"/.test(html);
const hasUserBubble = /class="message chat-skeleton user-msg"/.test(html);
console.log(JSON.stringify({
  ret, bubbleCount, skeletonKeyed, realMsgKeyed,
  hasAvatar, hasContent, hasHeaderRole, hasBody, hasShimmerLine, hasUserBubble,
  htmlLen: html.length,
}));
"""


def _run(count, conv='{"id":"c1","title":"My Conv"}', neuter=False):
    src = CR_JS.read_text()
    fn = ("function renderSkeletonChat(conv, msgCount) { return false; }"
          if neuter else _extract_fn(src, "renderSkeletonChat"))
    script = (_HARNESS.replace("__FN__", fn)
                      .replace("__CONV__", conv)
                      .replace("__COUNT__", str(count)))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    import json
    line = [l for l in out.stdout.strip().splitlines() if l.strip().startswith("{")][-1]
    return json.loads(line)


def test_paints_n_bubbles_aligned_to_real_dom():
    """5-message conv → 5 skeleton bubbles with the exact real bubble structure
    (avatar + content > header>role, body) as DIRECT #chatInner children."""
    r = _run(5)
    assert r["ret"] is True, r
    assert r["bubbleCount"] == 5, r
    assert r["hasAvatar"] and r["hasContent"] and r["hasHeaderRole"] and r["hasBody"], r
    assert r["hasShimmerLine"], "must render shimmer lines"
    assert r["hasUserBubble"], "must alternate in a user bubble"


def test_bubbles_keyed_skeleton_not_msg():
    """Bubbles use id=skeleton-msg-* and NEVER id=msg-* — so renderChat's
    surgical `[id^='msg-']` probe never mistakes them for real messages (the
    first real render must take the full-wipe path, zero-CLS)."""
    r = _run(4)
    assert r["skeletonKeyed"] == 4, r
    assert r["realMsgKeyed"] == 0, "skeleton must NOT emit msg-* ids"


def test_count_capped_for_huge_conv():
    """A huge msgCount is clamped to a small visual cap (bounds throwaway DOM)."""
    r = _run(500)
    assert r["bubbleCount"] == 6, r


def test_min_one_bubble():
    """msgCount 1 → exactly one bubble (caller only calls this for count>0)."""
    r = _run(1)
    assert r["bubbleCount"] == 1, r


def test_neuter_paints_nothing():
    """NEUTER: early-return body paints zero bubbles. Biting control."""
    r = _run(5, neuter=True)
    assert r["ret"] is False, r
    assert r["bubbleCount"] == 0, r
    assert r["htmlLen"] == 0, r


def test_wiring_gates_on_servermsgcount_positive():
    """loadConversation only paints the skeleton when _serverMsgCount>0 — an
    empty conv (count 0) falls through to the welcome/empty render, never a
    skeleton (source-level guard on the wiring, which no node harness exercises
    end-to-end)."""
    lc = Path(runtime_section_path("main/main_conv_lifecycle.js")).read_text()
    # The gate: `const _skMsgCount = c._serverMsgCount || 0;` then a
    # `_skMsgCount > 0 && ... renderSkeletonChat(` call.
    assert "_serverMsgCount || 0" in lc, "skeleton count must derive from _serverMsgCount"
    assert re.search(r"_skMsgCount\s*>\s*0[\s\S]{0,120}renderSkeletonChat\(", lc), \
        "renderSkeletonChat must be gated on _skMsgCount > 0 (no skeleton for empty conv)"
    # The old always-render 400ms fallback timer must be gone (it caused the flash).
    assert "_skeletonTimer" not in lc, "the deferred 400ms skeletonTimer should be removed"


def test_failure_degrades_to_retry_ui():
    """On fetch timeout / 404 the skeleton must be REPLACED by a Retry/NotFound
    UI (never a permanent placeholder). loadConversationMessages' error branches
    overwrite #chatInner — assert both replacement points exist."""
    cv = Path(runtime_section_path("core/conversations.js")).read_text()
    assert "Failed to load conversation" in cv, "timeout branch must show a Failed/Retry UI"
    assert "Conversation Not Found" in cv, "404 branch must show a Not-Found UI"
    # Both branches write inner.innerHTML (replacing the skeleton DOM).
    assert cv.count("inner.innerHTML") >= 2, "error branches must overwrite #chatInner"


if __name__ == "__main__":
    test_paints_n_bubbles_aligned_to_real_dom(); print("PASS aligned")
    test_bubbles_keyed_skeleton_not_msg(); print("PASS keyed")
    test_count_capped_for_huge_conv(); print("PASS capped")
    test_min_one_bubble(); print("PASS min-one")
    test_neuter_paints_nothing(); print("PASS neuter")
    print("ALL GREEN")
