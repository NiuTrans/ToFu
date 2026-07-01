"""Playwright visual E2E — stuck-translation ("翻译中…") self-heal.

Reproduces the reported bug: a server-driven auto-translate whose terminal
``done`` push frame was dropped leaves an assistant message stranded in the
pending state

    _translateDone === false  &&  no _translateTaskId  &&  no translatedContent

— a permanent "正在翻译为中文…" spinner that NO resume path recovered, because
``_resumePendingTranslations`` only scanned the LAST assistant (Phase 0b) and
only collected messages that HAVE a ``_translateTaskId`` (Phase 1). The
screenshot the user reported was a zombie on a NON-LAST assistant (a later
turn was already translated bilingually), so neither phase touched it and it
spun forever across reloads.

These tests build that exact shape (multi-turn conv, zombie on the FIRST of
two assistant turns) directly in the frontend's ``conversations`` memory —
the most faithful expression of the persisted state — then drive the real
resume entry point ``_resumePendingTranslations`` in a real browser and assert
the spinner is healed away.

The fix (``static/js/translation.js`` Phase 0 self-heal) scans EVERY message,
prefers the server-committed translation via ``_tryRecoverFromServer`` and
otherwise drops the stuck markers so the original text shows. Offline-safe:
the seeded conversation does not exist on the live server, so the recovery GET
returns null and the heal deterministically falls through to clearing markers
— no LLM, no WebSocket interception needed.

Run:  pytest tests/test_translate_stuck_e2e.py -m visual
"""
from __future__ import annotations

import time

import pytest

from tests.test_visual_e2e import _screenshot, _wait_for_app_ready

# A multi-turn conversation: msg 1 (FIRST assistant) is the zombie; msg 3
# (LAST assistant) is already translated bilingually — exactly the reported
# screenshot, where the stuck spinner is ABOVE a finished translation.
_SEED_JS = r"""
() => {
  const convId = 'test-conv-zombie-' + Date.now();
  const now = Date.now();
  const conv = {
    id: convId,
    title: 'Zombie translate test',
    _needsLoad: false,
    messages: [
      { role: 'user', content: 'Question one', timestamp: now - 4000 },
      { role: 'assistant',
        content: 'Answer one — English text that should have been translated to Chinese.',
        _translateDone: false,           // pending state set by a push 'running' frame
        _translatePartial: '部分翻译…',   // a partial preview that arrived
        // NOTE: deliberately NO _translateTaskId (server owns the task), and
        // NO translatedContent — this is the stranded zombie.
        timestamp: now - 3000 },
      { role: 'user', content: 'Question two', timestamp: now - 2000 },
      { role: 'assistant', content: 'Answer two.',
        translatedContent: '答案二。', _translateDone: true,
        _showingTranslation: true, timestamp: now - 1000 },
    ],
  };
  conversations.push(conv);
  activeConvId = convId;
  renderChat(conv, true);
  window.__zconv = convId;
  return convId;
}
"""


@pytest.mark.visual
class TestStuckTranslationSelfHeal:
    """The resume-time self-heal must clear a zombie spinner on ANY message."""

    def test_zombie_spinner_on_non_last_assistant_is_healed(self, page, screenshot_dir):
        _wait_for_app_ready(page)

        # ── Seed the zombie conversation directly in frontend memory ──
        conv_id = page.evaluate(_SEED_JS)
        assert conv_id, "seed should return a conversation id"
        time.sleep(0.3)

        # ── Sanity: the bug STATE actually renders a spinner on msg 1 ──
        # (proves the reproduction is real, not a no-op assertion).
        spinner_before = page.locator("#translate-loading-1").count()
        assert spinner_before == 1, (
            f"expected a stuck spinner on the non-last assistant (#translate-loading-1) "
            f"before heal, found {spinner_before}")
        # The last assistant is already translated — no spinner there.
        assert page.locator("#translate-loading-3").count() == 0, (
            "the already-translated last assistant must not show a spinner")

        _screenshot(page, screenshot_dir, "17_translate_stuck_before")

        # ── Drive the REAL resume entry point (async) ──
        page.evaluate(
            "async (cid) => { await _resumePendingTranslations(cid); }", conv_id)
        # The heal re-renders in place / via renderChat; give the DOM a beat.
        page.wait_for_timeout(400)

        _screenshot(page, screenshot_dir, "18_translate_stuck_after")

        # ── Assert: the zombie spinner is GONE ──
        spinner_after = page.locator("#translate-loading-1").count()
        assert spinner_after == 0, (
            f"the zombie spinner (#translate-loading-1) must be healed away, "
            f"still present={spinner_after}")

        # ── Assert: the stuck markers were cleared in memory ──
        done_flag = page.evaluate(
            "(cid) => { const c = conversations.find(x => x.id === cid); "
            "return c && c.messages[1]._translateDone; }", conv_id)
        assert done_flag is None or done_flag is True, (
            f"msg[1]._translateDone should be cleared (undefined) or recovered "
            f"(true), got {done_flag!r}")

        # ── Assert: the original message text is still shown (no data loss) ──
        msg1_text = page.locator("#msg-1").inner_text()
        assert "Answer one" in msg1_text, (
            f"the original assistant content must remain visible after heal; "
            f"got: {msg1_text[:120]!r}")

    def test_translated_sibling_is_untouched_by_heal(self, page, screenshot_dir):
        """The heal must not disturb an already-translated message."""
        _wait_for_app_ready(page)
        conv_id = page.evaluate(_SEED_JS)
        time.sleep(0.3)

        page.evaluate(
            "async (cid) => { await _resumePendingTranslations(cid); }", conv_id)
        page.wait_for_timeout(400)

        # The bilingual sibling keeps its translation + done flag.
        tc = page.evaluate(
            "(cid) => { const c = conversations.find(x => x.id === cid); "
            "return c && c.messages[3].translatedContent; }", conv_id)
        assert tc == "答案二。", f"sibling translation must survive the heal, got {tc!r}"
        done = page.evaluate(
            "(cid) => { const c = conversations.find(x => x.id === cid); "
            "return c && c.messages[3]._translateDone; }", conv_id)
        assert done is True, f"sibling _translateDone must stay true, got {done!r}"
