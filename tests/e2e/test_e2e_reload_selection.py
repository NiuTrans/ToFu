"""Reload must preserve the exact selected conversation, not a catalog neighbor."""
from __future__ import annotations

import pytest

from tests.e2e.test_e2e_journeys import (
    _fresh_chat,
    _send_and_wait_done,
    _wait_conversation_persisted,
)
from tests.e2e.test_e2e_smoke import (  # noqa: F401 -- load-bearing fixtures
    _disable_open_mode_rate_limit,
    _install_llm_stubs,
    _wait_app_ready,
)

pytestmark = pytest.mark.visual


def test_reload_preserves_exact_selected_conversation(page, assert_no_js_errors):
    _fresh_chat(page)
    _send_and_wait_done(page, 'Older reload selection E2E conversation')
    older_id = page.locator('.conv-item.active').get_attribute('data-conv-id')
    _wait_conversation_persisted(page, older_id, 'Older reload selection E2E conversation')
    _fresh_chat(page)
    selected_text = 'Selected reload selection E2E conversation'
    _send_and_wait_done(page, selected_text)
    selected_id = page.locator('.conv-item.active').get_attribute('data-conv-id')
    assert selected_id and selected_id != older_id
    _wait_conversation_persisted(page, selected_id, selected_text)
    before = page.evaluate("""() => ({active: window.activeConvId,
      session: sessionStorage.getItem('tofu_activeConvId')})""")
    assert before['active'] == before['session'] == selected_id, before
    page.reload()
    _wait_app_ready(page)
    page.wait_for_function("""(text) =>
      document.querySelector('#chatInner')?.innerText.includes(text)""",
      arg=selected_text, timeout=15000)
    after = page.evaluate("""() => ({active: window.activeConvId,
      session: sessionStorage.getItem('tofu_activeConvId'),
      mirror: localStorage.getItem('tofu_lastActiveConvId'),
      row: document.querySelector('.conv-item.active')?.dataset.convId})""")
    assert after['active'] == after['session'] == after['row'] == selected_id, after
    assert after['mirror'] == selected_id, after
