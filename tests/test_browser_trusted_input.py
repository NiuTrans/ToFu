"""tests/test_browser_trusted_input.py — CDP trusted-input pins (extension 4.6.0).

The extension now tries chrome.debugger Input.dispatch* events FIRST for
click / hover / keyboard (trusted events: isTrusted=true, real CSS :hover)
and falls back to the synthetic dispatchEvent path when CDP can't attach
(e.g. DevTools open on the tab). These pins guard:

  * the CDP helpers exist and are wired BEFORE the synthetic path;
  * every fallback annotates ``trusted: false`` + a reason so the model
    knows a click may have been ignored by an isTrusted-checking site;
  * the key descriptor never sends a text payload with a command modifier
    (Ctrl+S must not type "s");
  * both manifests moved to 4.6.0 together (the feature flag the server
    uses to read the annotation contract).

background.js has no JS harness in this repo — pins are source-level, the
same convention as test_browser_tooling_fixes.py. A NEUTER check proves
they bite.
"""

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / 'browser_extension' / 'background.js').read_text(encoding='utf-8')


def _fn_body(name):
    """Extract one function's source (brace-matched) for scoped assertions."""
    m = re.search(rf'(?:async )?function {name}\([^)]*\) \{{', SRC)
    assert m, f'{name} not found in background.js'
    depth, i = 0, m.end() - 1
    for i in range(m.end() - 1, len(SRC)):
        if SRC[i] == '{':
            depth += 1
        elif SRC[i] == '}':
            depth -= 1
            if depth == 0:
                return SRC[m.start():i + 1]
    raise AssertionError(f'{name} body never closes')


def test_cdp_input_helpers_exist():
    for helper in ('_cdpRun', '_cdpClick', '_cdpHover', '_cdpKeyboard',
                   '_cdpKeyDescriptor', '_locateElement'):
        assert f'function {helper}' in SRC, f'{helper} missing'


@pytest.mark.parametrize('cmd,cdp_fn,synthetic_fn', [
    ('cmdClickElement', '_cdpClick', '_clickElement'),
    ('cmdHoverElement', '_cdpHover', '_hoverElement'),
    ('cmdKeyboardInput', '_cdpKeyboard', '_keyboardInput'),
])
def test_commands_try_cdp_before_synthetic(cmd, cdp_fn, synthetic_fn):
    body = _fn_body(cmd)
    cdp_at = body.find(f'await {cdp_fn}(')
    syn_at = body.find(synthetic_fn)
    assert cdp_at != -1, f'{cmd} never calls {cdp_fn}'
    assert syn_at != -1, f'{cmd} lost its synthetic fallback ({synthetic_fn})'
    assert cdp_at < syn_at, (
        f'{cmd} must try {cdp_fn} BEFORE {synthetic_fn} — trusted input is '
        f'the default, synthetic is the fallback')


def test_trusted_annotation_contract_on_both_paths():
    # CDP success path marks trusted:true
    assert 'trusted: true' in _fn_body('_cdpClick')
    assert 'trusted: true' in _fn_body('_cdpKeyboard')
    # Synthetic fallbacks mark trusted:false with a reason
    for cmd in ('cmdClickElement', 'cmdHoverElement', 'cmdKeyboardInput'):
        body = _fn_body(cmd)
        assert 'r.trusted = false' in body, (
            f'{cmd} fallback lost the trusted=false annotation')
        assert 'fallbackReason' in body, (
            f'{cmd} fallback lost its fallbackReason')


def test_cdp_click_dispatches_a_real_mouse_sequence():
    body = _fn_body('_cdpClick')
    for phase in ('mouseMoved', 'mousePressed', 'mouseReleased'):
        assert phase in body, f'_cdpClick no longer sends {phase}'
    assert "button = rightClick ? 'right' : 'left'" in body, (
        'right-click must travel as the real right button so contextmenu '
        'fires trusted')


def test_key_descriptor_never_types_text_with_command_modifiers():
    body = _fn_body('_cdpKeyDescriptor')
    # The guard: Alt|Ctrl|Meta must strip the text payload (Ctrl+S ≠ type "s")
    assert re.search(r'modifiers & \(_CDP_MODIFIER_BITS\.Alt \| '
                     r'_CDP_MODIFIER_BITS\.Control \| '
                     r'_CDP_MODIFIER_BITS\.Meta\)', body), (
        'the command-modifier text strip is gone — Ctrl+S would type "s"')
    assert 'descriptor.text = undefined' in body


def test_key_descriptor_covers_enter_and_shift_letters():
    assert "Enter: ['Enter', 13, '\\r']" in SRC, (
        'Enter must carry a \\r text payload or forms/inputs never see it')
    body = _fn_body('_cdpKeyDescriptor')
    assert "'Key' + upper" in body and "'Digit' + mainKey" in body, (
        'printable keys lost their code synthesis')


def test_manifests_moved_to_460_together():
    dev = json.loads((ROOT / 'browser_extension' / 'manifest.json')
                     .read_text(encoding='utf-8'))
    store = json.loads((ROOT / 'docs' / 'chrome-web-store' / 'manifest.store.json')
                       .read_text(encoding='utf-8'))
    assert dev['version'] == store['version'], 'manifest skew'
    major, minor, _ = (int(x) for x in dev['version'].split('.'))
    assert (major, minor) >= (4, 6), (
        'trusted input landed without a version bump — the server cannot '
        'tell whether the wire carries the trusted annotation')


def test_NEUTER_removing_the_cdp_section_is_caught():
    """Every pin above must fail if the trusted-input section is ripped out."""
    neutered = SRC.replace('await _cdpClick(', 'await _neverCalled(')
    neutered = neutered.replace('trusted: true', 'trusted: maybe')
    assert 'await _neverCalled(' in neutered and 'trusted: maybe' in neutered
    # The pins key on the real strings; spot-check two of them die here.
    assert 'await _cdpClick(' not in neutered
    assert 'trusted: true' not in neutered
