"""Opening a historical conversation must NOT jump the page (repeatedly).

WHY
---
Symptom (owner): "no matter which historical conversation I click on, I want it
to stay stable, not jump around the page, or even jump several times in a row."

Root cause: `loadConversationMessages` (static/js/core/conversations.js) fires
FOUR fire-and-forget re-hydration callbacks on EVERY conversation open —
compaction-markers + artifacts, in BOTH the Phase-1 (IndexedDB cache) and
Phase-2 (server) branches. Each callback resolves after its OWN network
round-trip, at a DIFFERENT moment, and used to call the full-render path
`renderChat(conv, false)`. While the initial switch is in flight
(`conv._initialSwitchLoad` set), that full-render path runs
`_forceScrollToBottom` — so each late-landing callback yanks the reader to the
bottom, producing the "jumps several times" behaviour.

The sibling cost / file-change prefetches were already migrated to the
scroll-preserving in-place repaint `_bgRefreshChat` (which never force-scrolls
and is a no-op when nothing changed). This test pins that the marker + artifact
callbacks are migrated too: their `.then()` bodies must invoke `_bgRefreshChat`
and must NOT call the force-scroll `renderChat`.

Structural test (regex over the real shipped source) with a biting NEUTER:
reverting one callback back to `renderChat(conv, false)` makes the assertion
fail — proving the check is load-bearing.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
CONV_JS = REPO / "static" / "js" / "core" / "conversations.js"

# The four fire-and-forget re-hydration callbacks that run on every conv open.
# Each is matched by the unique `.catch(...)` tail its wiring carries.
_CALLBACK_TAILS = [
    "[compaction] attach (cache) failed:",
    "[artifacts] hydrate (cache) failed:",
    "[compaction] attach (server) failed:",
    "[artifacts] hydrate (server) failed:",
]


def _callback_block(src: str, tail: str) -> str:
    """Return the `.then(() => { ... })` body immediately preceding a callback's
    unique `.catch('<tail>')`. We slice a generous window ending at the tail and
    isolate the last `.then(() => {` ... `})` before it."""
    cidx = src.index(tail)
    window = src[max(0, cidx - 1400):cidx]
    tidx = window.rindex(".then(")
    return window[tidx:]


def test_all_four_load_callbacks_use_scroll_preserving_repaint():
    src = CONV_JS.read_text()
    for tail in _CALLBACK_TAILS:
        block = _callback_block(src, tail)
        assert "_bgRefreshChat(conv)" in block, (
            f"callback for {tail!r} must repaint via scroll-preserving "
            f"_bgRefreshChat; got:\n{block}"
        )
        # The force-scroll full-render path must NOT be reachable from these
        # callbacks — that is what caused the repeated jump-to-bottom on open.
        assert "renderChat(conv, false)" not in block, (
            f"callback for {tail!r} still calls the force-scroll renderChat "
            f"path — this re-introduces the open-conversation jump:\n{block}"
        )


def test_neuter_reverting_one_callback_is_caught():
    """Biting control: revert the Phase-1 compaction callback to the old
    force-scroll `renderChat(conv, false)` and confirm the guard above fails."""
    src = CONV_JS.read_text()
    tail = "[compaction] attach (cache) failed:"
    cidx = src.index(tail)
    # Replace the FIRST `_bgRefreshChat(conv)` at or before this callback.
    before = src[:cidx]
    ridx = before.rindex("_bgRefreshChat(conv)")
    neutered = (
        src[:ridx]
        + "renderChat(conv, false)"
        + src[ridx + len("_bgRefreshChat(conv)"):]
    )
    assert neutered != src, "neuter did not modify the source"

    block = _callback_block(neutered, tail)
    # The neutered callback now trips both halves of the guard.
    caught = ("_bgRefreshChat(conv)" not in block) or ("renderChat(conv, false)" in block)
    assert caught, (
        "neuter should be caught: the reverted callback must fail the "
        f"scroll-preserving guard. block:\n{block}"
    )


if __name__ == "__main__":
    test_all_four_load_callbacks_use_scroll_preserving_repaint()
    print("PASS test_all_four_load_callbacks_use_scroll_preserving_repaint")
    test_neuter_reverting_one_callback_is_caught()
    print("PASS test_neuter_reverting_one_callback_is_caught")
    print("ALL GREEN")
