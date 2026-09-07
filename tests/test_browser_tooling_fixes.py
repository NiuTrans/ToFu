"""Drift-guard tests for the 2026-07-15 browser-tooling fixes.

Three fixes, each asserted present AND (where meaningful) with a neuter check
that the guard bites on regression:

  1. Screenshot decoupled from window size: _screenshotFullPageCDP forces a
     desktop viewport via Emulation.setDeviceMetricsOverride before capture and
     ALWAYS clears it in finally (before detach) so the user's real page layout
     is never corrupted — on success, capture-error, and empty-shot paths.
  2. browser_fill_form uses type_text (clears the field first) not
     keyboard_input (appends), so changing origin A→B replaces cleanly; plus
     description hard-steers to it for 2+ fields, with reverse pointers on
     browser_click / browser_press_key.
  3. browser_navigate tells the model to web_search for the
     real URL when unsure instead of guessing a domain.

JS cannot run under pytest here, so the screenshot fix is verified by
structural analysis of the extracted function body (call ordering), following
the repo's existing verify-then-write discipline (see test_tool_audit_tranche1).
"""

import os
import re

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as f:
        return f.read()


def _extract_fn_body(src, fn_signature):
    """Return the brace-balanced body of a JS function starting at fn_signature."""
    start = src.index(fn_signature)
    brace = src.index('{', start)
    depth = 0
    i = brace
    while i < len(src):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
        i += 1
    raise AssertionError('unbalanced braces for ' + fn_signature)


# ── 1. Screenshot viewport override + guaranteed clear ──────────────

def test_screenshot_forces_viewport_override_before_capture():
    src = _src('browser_extension/background.js')
    body = _extract_fn_body(src, 'async function _screenshotFullPageCDP(')

    i_override = body.index('setDeviceMetricsOverride')
    i_capture = body.index('Page.captureScreenshot')
    assert i_override < i_capture, (
        'the device-metrics override must be applied BEFORE the capture so the '
        'page reflows to the forced desktop viewport first')

    # The override must be a plain desktop render: no scaling, not mobile.
    assert 'deviceScaleFactor: 1' in body
    assert 'mobile: false' in body


def test_screenshot_always_clears_override_before_releasing_cdp_lease():
    src = _src('browser_extension/background.js')
    body = _extract_fn_body(src, 'async function _screenshotFullPageCDP(')

    # The clear must live in the finally block, guarded by `overridden`, and
    # must finish before _cdpRun releases the shared lease (clear needs the
    # live debugger session). Key on the actual sendCommand CALL — the string
    # also appears in a comment and a catch-log, which must not satisfy the guard.
    i_finally = body.rindex('} finally {')
    m_clear = re.search(
        r"sendCommand\(\s*target\s*,\s*'Emulation\.clearDeviceMetricsOverride'\s*\)",
        body)
    assert m_clear is not None, 'must actually call clearDeviceMetricsOverride'
    i_clear = m_clear.start()
    assert i_finally < i_clear, (
        'the clearDeviceMetricsOverride CALL must be in the callback finally')
    assert '_cdpRun' in body and 'chrome.debugger.detach' not in body, (
        'screenshots must use the shared lease broker, not detach another holder')
    assert 'if (overridden)' in body, (
        'clear must be guarded so we only clear when we actually overrode')

    # `overridden` is only set true AFTER the override call resolves, so a
    # failed override never triggers a bogus clear.
    assert re.search(r'overridden\s*=\s*true', body)
    assert re.search(r'overridden\s*=\s*false', body), (
        'a rejected override must reset the flag so finally skips the clear')


def test_screenshot_override_clear_neuter_bites():
    # Neuter: a body without the clear call in finally is exactly the pre-fix
    # regression that would leave the user's page shrunk/emulated. The guard
    # (test above) keys on clearDeviceMetricsOverride being present in finally.
    src = _src('browser_extension/background.js')
    body = _extract_fn_body(src, 'async function _screenshotFullPageCDP(')
    # Remove ONLY the sendCommand clear call (leave the comment) — this is the
    # exact pre-fix regression. The load-bearing test keys on this call, so it
    # would then raise (bites).
    neutered = re.sub(
        r"await chrome\.debugger\.sendCommand\(target, 'Emulation\.clearDeviceMetricsOverride'\);",
        'void 0;', body)
    assert re.search(
        r"sendCommand\(\s*target\s*,\s*'Emulation\.clearDeviceMetricsOverride'\s*\)",
        neutered) is None, 'sanity: neuter removed the clear CALL'


# ── 2. fill_form clears + reverse pointers ──────────────────────────

def test_fill_form_type_uses_type_text_not_append():
    from tests._source_scan import python_block
    src = _src('lib/browser/advanced.py')
    # Brace/indent-matched body, not a fixed 2000-byte window: the real function
    # is far longer than any constant we could pick, so a window TRUNCATES it
    # and these POSITIVE assertions would pass only while the tokens happen to
    # sit early. Move the dispatch down and the guard goes quietly green.
    body = python_block(src, 'def fill_form_sequential')
    # The 'type' branch must send type_text with clearFirst, NOT keyboard_input.
    assert "'type_text'" in body
    assert "'clearFirst': True" in body


def test_fill_form_type_branch_no_longer_appends_via_keyboard():
    # Neuter/regression sentinel: the old code path used keyboard_input to type
    # the value (which appends). That specific pattern must be gone from the
    # 'type' branch so a pre-filled field is replaced, not concatenated.
    from tests._source_scan import python_block
    src = _src('lib/browser/advanced.py')
    # Indent-matched branch (charter #24 / pt_b95c6d39): the old
    # ``src[start:start + 800]`` window stopped 5.6 KB short of this branch's
    # real end, so a keyboard_input dispatch added past the cutoff would never
    # be seen -- a NEGATIVE assertion that silently stops looking.
    branch = python_block(src, "if field_type == 'type':")
    # The regression is CALLING keyboard_input to send the value. Match the
    # actual command dispatch (the word may still appear in an explanatory
    # comment, which is fine -- python_block strips comments first).
    assert "send_browser_command('keyboard_input'" not in branch, (
        "the 'type' branch must not send keyboard_input for the value (that "
        "appends onto the existing field value)")


def test_fill_form_description_steers_multi_field():
    from lib.browser.advanced import ADVANCED_BROWSER_TOOL_FILL_FORM
    desc = ADVANCED_BROWSER_TOOL_FILL_FORM['function']['description']
    assert '2+' in desc or 'MULTIPLE' in desc
    assert 'CLEARED' in desc or 'cleared' in desc


def test_click_and_press_key_point_back_to_fill_form():
    # Both action descriptions direct multi-field work to one form call.
    from lib.tools.browser import BROWSER_TOOL_CLICK, BROWSER_TOOL_PRESS_KEY
    click_desc = BROWSER_TOOL_CLICK['function']['description']
    key_desc = BROWSER_TOOL_PRESS_KEY['function']['description']
    assert 'browser_fill_form' in click_desc, (
        'click must reverse-point to fill_form for multi-field forms')
    assert 'browser_fill_form' in key_desc, (
        'press_key must reverse-point to fill_form (keystrokes append)')


# ── 2b. screenshot waits for layout stability (no fixed sleep) ──────

def test_screenshot_waits_for_content_stability_not_fixed_sleep():
    src = _src('browser_extension/background.js')
    body = _extract_fn_body(src, 'async function _screenshotFullPageCDP(')
    # After the override we must converge on layout stability, not sleep a
    # fixed 350ms (which truncates async result lists like flights/tickets).
    assert '_waitForContentStable(target)' in body, (
        'must wait for content stability after forcing the viewport')
    assert 'setTimeout(r, 350)' not in body, (
        'the brittle fixed 350ms sleep must be gone from the capture path')


def test_content_stability_polls_size_and_readystate():
    src = _src('browser_extension/background.js')
    body = _extract_fn_body(src, 'async function _waitForContentStable(')
    # Convergence = content size unchanged for N consecutive polls AND
    # readyState complete, capped by a max-wait budget.
    assert 'getLayoutMetrics' in body, 'must poll layout metrics'
    assert 'readyState' in body, 'must gate on document.readyState complete'
    assert 'STABILITY_MAX_WAIT_MS' in body, 'must have a bounded wait budget'
    assert re.search(r'stableCount\s*>=\s*STABILITY_STABLE_READS', body), (
        'must require consecutive stable reads before declaring stable')


# ── 2c. fill_form select failure surfaces (not silent) ──────────────

def _fake_sender(script):
    """Build an injected browser sender driven by a per-command script.

    `script` maps command name -> (result, error). get_interactive_elements
    may map to a callable returning (result, error) so tests control options.
    Records every call for assertions.
    """
    calls = []

    def fake(cmd, params, timeout=None):
        calls.append((cmd, params))
        entry = script.get(cmd, ({'clicked': True}, None))
        if callable(entry):
            return entry(params)
        return entry

    return fake, calls


def test_fill_form_select_no_match_reports_failure_with_candidates(monkeypatch):
    from lib.browser.advanced import fill_form_sequential
    options = {'elements': [
        {'text': 'Economy', 'selector': '#opt1'},
        {'text': 'Business', 'selector': '#opt2'},
    ]}
    send, calls = _fake_sender({
        'get_interactive_elements': (options, None),
        'click_element': ({'clicked': True}, None),
        'type_text': ({'typed': True}, None),
    })
    out = fill_form_sequential(1, [
        {'selector': '#from', 'value': 'PEK', 'type': 'type'},
        {'selector': '#cabin', 'value': 'First Class', 'type': 'select'},  # no match
    ], field_delay=0, send=send)

    assert out['success'] is False, 'a missing select option must fail the whole call'
    assert out['fields_filled'] == 1
    assert out['fields_failed'] == 1
    sel = [r for r in out['field_results'] if r.get('type') == 'select'][0]
    assert sel['ok'] is False
    assert 'available_options' in sel and 'Economy' in sel['available_options']
    # The unmatched option must NOT have been clicked.
    clicked_selectors = [p.get('selector') for c, p in calls if c == 'click_element']
    assert '#opt1' not in clicked_selectors and '#opt2' not in clicked_selectors


def test_fill_form_select_match_succeeds(monkeypatch):
    from lib.browser.advanced import fill_form_sequential
    options = {'elements': [{'text': 'Business', 'selector': '#opt2'}]}
    send, _calls = _fake_sender({
        'get_interactive_elements': (options, None),
        'click_element': ({'clicked': True}, None),
        'type_text': ({'typed': True}, None),
    })
    out = fill_form_sequential(1, [
        {'selector': '#cabin', 'value': 'business', 'type': 'select'},
    ], field_delay=0, send=send)
    assert out['success'] is True
    assert out['fields_failed'] == 0
    sel = out['field_results'][0]
    assert sel['ok'] is True and sel['matched'] == 'Business'


def test_fill_form_skips_submit_when_a_field_failed(monkeypatch):
    from lib.browser.advanced import fill_form_sequential
    send, calls = _fake_sender({
        'get_interactive_elements': ({'elements': []}, None),  # select finds nothing
        'click_element': ({'clicked': True}, None),
        'type_text': ({'typed': True}, None),
    })
    out = fill_form_sequential(1, [
        {'selector': '#cabin', 'value': 'X', 'type': 'select'},  # will fail
    ], submit_selector='#go', field_delay=0, send=send)
    assert out['success'] is False
    assert out['submitted'] is False, 'must not submit a half-filled form'
    submit_clicks = [p for c, p in calls if c == 'click_element' and p.get('selector') == '#go']
    assert submit_clicks == [], 'submit button must not be clicked when a field failed'


def test_fill_form_select_silent_success_neuter_bites(monkeypatch):
    # Sanity that the assertion is load-bearing: if fill_form regressed to the
    # old silent behavior (no match → still success), the failure test above
    # would flip. Here we assert the CURRENT code makes success depend on
    # fields_failed == 0 by checking a mixed batch.
    from lib.browser.advanced import fill_form_sequential
    send, _calls = _fake_sender({
        'get_interactive_elements': ({'elements': [{'text': 'Y', 'selector': '#y'}]}, None),
        'click_element': ({'clicked': True}, None),
        'type_text': ({'typed': True}, None),
    })
    out = fill_form_sequential(1, [
        {'selector': '#a', 'value': 'ok', 'type': 'type'},
        {'selector': '#b', 'value': 'ZZZ', 'type': 'select'},  # no match
    ], field_delay=0, send=send)
    assert out['success'] is False and out['fields_filled'] == 1, (
        'overall success must be gated on every field succeeding')


# ── 3. search-first URL guidance ────────────────────────────────────

def test_navigate_tells_model_to_search_first():
    # New-tab navigation follows the same search-first URL contract.
    from lib.tools.browser import BROWSER_TOOL_NAVIGATE
    tool = BROWSER_TOOL_NAVIGATE
    desc = tool['function']['description']
    assert 'web_search' in desc, (
        f"{tool['function']['name']} must instruct web_search when unsure")
    assert 'guess' in desc.lower() or 'memory' in desc.lower(), (
        'must explicitly forbid guessing the URL from memory')
    assert 'new_tab' in tool['function']['parameters']['properties'], (
        'navigate must carry new_tab (the create_tab merge point)')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:napari']))
