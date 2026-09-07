"""tests/test_qr_login_gate.py — QR-tab hard gate for the SSO login capture.

``tests/_qr_login_capture.py`` screenshots the SSO login page so a human can
scan it. The original implementation switched to the QR tab with a JS click and
trusted its own return value:

    clicked = page.evaluate(...)   # True == "I dispatched a click"

``True`` there proves only that the switch element was found and a click event
was dispatched — NOT that the tab actually changed. On a miss the script
screenshotted the PASSWORD form and reported success, handing the operator an
unscannable image. So a hard gate now stands between the click and the
screenshot: :func:`_wait_for_qr` must positively identify a visible QR.

The gate's own first version then repeated the very mistake it was written to
prevent. Its judgement was purely GEOMETRIC ("visible, >=120px, ratio
0.8–1.25"), and the login page's brand illustration — measured at 540x468,
ratio 1.15 — sails straight through it. Measured on the live page: the gate
returned ``img 540x468`` while the PASSWORD form was on screen.

That measurement is the regression sample below. The rule it encodes:
**a gate must itself judge on the real condition, not on a looser approximation
of it.** The fix anchors on CONTEXT — square-ish AND inside a container that
announces QR login AND that container holds no password input.

These tests run offline against synthetic DOMs (no network, no SSO), so they
guard the judgement rather than the site.
"""

import importlib.util
import os

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       '_qr_login_capture.py')


@pytest.fixture(scope='module')
def qr():
    """Load the capture script when its optional browser runtime is present."""
    pytest.importorskip(
        'playwright.sync_api',
        reason='QR login capture requires the optional Playwright capability',
    )
    spec = importlib.util.spec_from_file_location('_qr_cap', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakePage:
    """Minimal page double: ``evaluate`` returns a scripted verdict."""

    def __init__(self, verdicts):
        # verdicts: list of values _wait_for_qr's JS would return, consumed in order.
        self._verdicts = list(verdicts)
        self.waits = 0

    def evaluate(self, _js):
        return self._verdicts.pop(0) if self._verdicts else None

    def wait_for_timeout(self, _ms):
        self.waits += 1


# ── The gate exists and is consulted before the screenshot ──

def test_gate_function_exists(qr):
    assert hasattr(qr, '_wait_for_qr'), '_wait_for_qr is the hard gate — do not remove it'


def _gate_call_offset(src):
    """Offset of the gate's CALL site, not its ``def`` line.

    ``src.find('_wait_for_qr(page')`` matches the definition first (it appears
    earlier in the file), which made an earlier version of these assertions
    compare the wrong offsets. Anchor on ``qr = _wait_for_qr(`` — the call.
    """
    for pat in ('qr = _wait_for_qr(', '= _wait_for_qr('):
        at = src.find(pat)
        if at > 0:
            return at
    return -1


def test_screenshot_is_gated_on_the_qr_check(qr):
    """Source-level: the QR check must PRECEDE the success screenshot.

    If the screenshot could happen first, a password form would again be handed
    to the operator as if it were a QR.
    """
    src = open(_SCRIPT, encoding='utf-8').read()
    gate = _gate_call_offset(src)
    assert gate > 0, 'the gate is never called'
    shot = src.find('page.screenshot', gate)
    assert shot > gate, 'screenshot must come AFTER the gate'
    # And a failed gate must exit non-zero rather than continue.
    tail = src[gate:gate + 1200]
    assert 'return 2' in tail, 'a failed gate must exit non-zero'


def test_gate_does_not_trust_the_click_return(qr):
    """`clicked: True` must not be the thing that authorizes the screenshot."""
    src = open(_SCRIPT, encoding='utf-8').read()
    click_at = src.find('qrcode-change JS-click')
    gate_at = _gate_call_offset(src)
    assert click_at > 0 and gate_at > click_at, (
        'the gate must run after the click, as an independent check')


# ── Gate semantics (driven through the real function) ──

def test_returns_none_when_no_qr_is_found(qr):
    page = _FakePage([None, None])
    assert qr._wait_for_qr(page, timeout_s=1) is None


def test_returns_the_description_when_found(qr):
    page = _FakePage(['img 150x150'])
    assert qr._wait_for_qr(page, timeout_s=5) == 'img 150x150'


def test_polls_until_the_qr_appears(qr):
    """The QR renders async — the gate must keep looking, not fail instantly."""
    page = _FakePage([None, None, 'canvas 160x160'])
    assert qr._wait_for_qr(page, timeout_s=10) == 'canvas 160x160'
    assert page.waits >= 2, 'gate should have waited between polls'


def test_evaluate_errors_do_not_crash_the_gate(qr):
    class _Boom(_FakePage):
        def evaluate(self, _js):
            raise RuntimeError('execution context destroyed')
    assert qr._wait_for_qr(_Boom([]), timeout_s=1) is None


# ── The JS judgement itself: the brand-illustration regression ──

def _js_of_gate():
    """Extract the gate's JS predicate so its thresholds can be asserted."""
    src = open(_SCRIPT, encoding='utf-8').read()
    start = src.find('def _wait_for_qr')
    end = src.find('\ndef ', start + 10)
    return src[start:end if end > 0 else len(src)]


def test_rejects_the_brand_illustration_dimensions():
    """REGRESSION: 540x468 (ratio 1.15) must be outside the accepted band.

    Measured on the live login page — this is the element that fooled the
    gate's first version while the password form was displayed.
    """
    js = _js_of_gate()
    assert 'r.width > 400' in js or 'width > 400' in js, (
        'an upper size bound is required — the 540px brand art must be excluded')
    # The ratio band must be tight enough that 1.15 is NOT a QR.
    assert 'ratio < 0.9' in js and 'ratio > 1.12' in js, (
        'ratio band must be tight (a QR is square); 1.15 must fall outside')


def test_requires_qr_context_not_just_geometry():
    """Geometry alone was proven insufficient — context anchoring is mandatory.

    The gate's JS lives in an r-string, so CJK appears as ``\\uXXXX`` escapes in
    the source. Assert on either form rather than on the literal only — an
    earlier version of this test failed for that reason alone, not because the
    words were missing.
    """
    js = _js_of_gate()
    assert 'QR_WORDS' in js, 'the gate must anchor on QR-login wording'
    for word in ('大象扫描', '扫码登录', '二维码'):
        escaped = ''.join('\\u%04x' % ord(ch) for ch in word)
        assert word in js or escaped in js, f'missing QR context word: {word}'


def test_rejects_a_container_holding_a_password_input():
    """The password tab's card must never satisfy the gate."""
    js = _js_of_gate()
    assert 'input[type="password"]' in js, (
        'a container with a password input must be rejected')


def test_visibility_and_viewport_are_checked():
    js = _js_of_gate()
    assert 'display' in js and 'visibility' in js and 'opacity' in js
    assert 'innerHeight' in js and 'innerWidth' in js, (
        'an off-screen element (hidden tab) must not count as visible')


# ── Credential hygiene: names only, never values ──

def test_capture_never_prints_cookie_values(qr):
    """The script may log cookie NAMES / domains / flags — never values."""
    src = open(_SCRIPT, encoding='utf-8').read()
    assert "c.get('value')" not in src and 'c["value"]' not in src, (
        'cookie values must never be read for logging')
    assert 'names only' in src or 'never printed' in src.lower() or \
           'NEVER printed' in src, 'the no-values rule should be stated'
