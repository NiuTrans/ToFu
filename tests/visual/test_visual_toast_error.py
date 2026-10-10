"""Public API failure details survive the rendered Toast path."""

import pytest


pytestmark = pytest.mark.visual


def test_error_toast_shows_detail_as_text(page):
    page.wait_for_function('typeof showToast === "function"')
    page.wait_for_function('typeof errorEnvelopeMessage === "function"')
    detail = 'ContractViolation: <img src=x onerror=alert(1)> turn.status'
    page.evaluate('(value) => showToast("❌", "Conversation failed to load", value, 6000)', detail)

    toast = page.locator('#toastContainer .toast').last
    assert toast.locator('.toast-detail').inner_text() == detail
    assert toast.locator('img').count() == 0
    assert 't-error' in toast.get_attribute('class')

    page.evaluate('''() => showToast(errorEnvelopeMessage({
      kind: 'internal', message: 'Internal error',
      detail: 'ContractViolation: turn.status', source: 'conversation-sync',
    }), 'error')''')
    event_toast = page.locator('#toastContainer .toast').last
    assert 'ContractViolation: turn.status' in event_toast.inner_text()

    page.evaluate('''() => showToast(errorEnvelopeMessage({
      kind: 'internal', message: 'Internal error',
    }), 'error')''')
    generic_event_toast = page.locator('#toastContainer .toast').last
    assert 'logs/error.log' in generic_event_toast.inner_text()


def test_send_failure_toast_keeps_full_selectable_identifiers(page):
    page.wait_for_function('typeof showToast === "function"')
    message = ('Generation executor did not start\n'
               'Request ID: 4c0fe1-71-abcdef0123456789\n'
               'Conversation ID: mufmucgu8c8ljb<unsafe>')
    page.evaluate('(text) => showToast(text, "error")', message)
    title = page.locator('#toastContainer .toast.t-error .toast-title').last
    assert title.inner_text() == message
    assert title.evaluate('(element) => getComputedStyle(element).whiteSpace') == 'pre-wrap'
    assert title.evaluate('(element) => getComputedStyle(element).userSelect') == 'text'
    assert page.locator('#toastContainer unsafe').count() == 0
