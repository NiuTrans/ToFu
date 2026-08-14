"""tests/test_frontend_p0_small_batch.py — pt_26a427d3.

Three P0-class frontend fixes, one suite each:

  ① Boot-path localStorage bare ``JSON.parse`` (core.js claude_client_config,
    core/cost.js claude_auto_translate / claude_auto_apply). One corrupted
    key threw at module top level and white-screened the app. Now routed
    through ``_safeJsonParse`` (corrupt → fallback). Drives the REAL helper
    under node.

  ② image-gen.js single-mode: user Cancel and the 150 s watchdog shared one
    AbortController, so the catch mislabelled a deliberate cancel as
    "Request timed out (150s)" and pushed a timeout error message. Now
    ``_igUserCancelled`` distinguishes them (cancel → 'cancelled' notice,
    no 150 s text, no timeout toast). Batch mode already handled this.

  ③ scheduler.js was an entirely dead panel (schedulerBadge/schedulerPanel/
    proactiveCount exist in NO template; toggleSchedulerPanel uncalled;
    _applySchedulerUI toggled a nonexistent badge). Removed: the file, its
    _CLASSIC_ASSET_FILES entry and _applySchedulerUI + its call site. The scheduler
    TOOL itself is server-side always-on and unaffected.

Each check carries a byte-reverting NEUTER.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_frontend_p0_small_batch.py -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CORE_JS = os.path.join(ROOT, 'static', 'js', 'core.js')
COST_JS = os.path.join(ROOT, 'static', 'js', 'core', 'cost.js')
IMAGE_GEN_JS = os.path.join(ROOT, 'static', 'js', 'image-gen.js')
MAIN_JS = os.path.join(ROOT, 'static', 'js', 'main.js')
BUNDLER = os.path.join(ROOT, 'lib', 'js_bundler.py')
SCHEDULER_JS = os.path.join(ROOT, 'static', 'js', 'scheduler.js')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


# ── ① _safeJsonParse: behavior + call-site scans ──────────────────────────

_SAFE_DRIVER = r"""
const cases = [
  ['{"a":1}', 'valid'],
  ['{"a":1,', 'corrupt'],
  [null, 'null'],
  ['false', 'false'],
  ['true', 'true'],
];
const out = cases.map(([raw, tag]) => {
  const r = _safeJsonParse(raw, { fb: true });
  return { tag, type: typeof r, a: r && r.a, fb: !!(r && r.fb) };
});
console.log(JSON.stringify(out));
"""


def _extract_safe_parse(src: str) -> str:
    m = re.search(r'function _safeJsonParse\(raw, fallback\) \{.*?\n\}', src, re.DOTALL)
    assert m, '_safeJsonParse not found in core.js'
    return m.group(0)


def _run_safe(src: str) -> list:
    if not shutil.which('node'):
        pytest.skip('node not available')
    proc = subprocess.run(['node', '-e', src + '\n' + _SAFE_DRIVER],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f'node harness failed:\n{proc.stderr[-1500:]}'
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_safe_json_parse_behavior():
    out = _run_safe(_extract_safe_parse(_read(CORE_JS)))
    by = {c['tag']: c for c in out}
    assert by['valid']['a'] == 1 and by['valid']['fb'] is False
    assert by['corrupt']['fb'] is True, 'corrupt JSON must fall back, not throw'
    assert by['null']['fb'] is True
    assert by['false']['type'] == 'boolean' and by['false']['fb'] is False
    assert by['true']['type'] == 'boolean'


def test_NEUTER_safe_parse_gutted_throws():
    """Byte-reverting NEUTER: replace the helper body with a bare JSON.parse —
    the corrupt case MUST throw (proves the guard is load-bearing)."""
    src = _extract_safe_parse(_read(CORE_JS))
    neutered = re.sub(r'try \{ return JSON\.parse\(raw\); \} catch \(_\) \{ return fallback; \}',
                      'return JSON.parse(raw);', src)
    assert neutered != src, 'NEUTER anchor missing'
    if not shutil.which('node'):
        pytest.skip('node not available')
    proc = subprocess.run(['node', '-e', neutered + '\n' + _SAFE_DRIVER],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0, (
        'NEUTER FAILED: gutted helper did not throw on corrupt input')


def test_boot_sites_use_safe_parse():
    core = _read(CORE_JS)
    cost = _read(COST_JS)
    assert '_safeJsonParse(\n  localStorage.getItem("claude_client_config")' in core
    assert 'JSON.parse(\n  localStorage.getItem("claude_client_config")' not in core
    assert cost.count('_safeJsonParse(\n  localStorage.getItem("claude_auto_') == 2, (
        f'expected 2 safe-parse sites in cost.js, found '
        f'{cost.count("_safeJsonParse(chr(10)  localStorage")}')
    assert 'JSON.parse(\n  localStorage.getItem("claude_auto_' not in cost


# ── ② image-gen cancel vs timeout ─────────────────────────────────────────
#
# ★ REWRITTEN (pt_e3809ce36b544975). This pair asserted an implementation that
# no longer exists and was RED on a byte-clean tree.
#
# The original mechanism was an explicit ``_igUserCancelled`` flag whose ONLY
# job was to tell a user Cancel apart from a 150s client-side watchdog firing —
# both surface as ``err.name === 'AbortError'``, so without the flag a timeout
# would have been mislabelled "Cancelled by user".
#
# Commit eb1ddee5 ("Remove the browser-side timeout ceilings too") deleted the
# watchdog and the flag TOGETHER, which is correct: with no watchdog there is
# no second AbortError producer to disambiguate from, so the flag became dead
# state. Verified on every leg rather than trusting the code comment:
#
#   * exactly ONE ``.abort()`` reaches the single-image request — inside
#     ``_igCancelGeneration``, i.e. the Cancel button;
#   * the request passes only ``_igAbortController.signal`` — no
#     ``AbortSignal.timeout`` and no ``setTimeout(... abort ...)`` anywhere;
#   * ``Api.images.generate`` pins ``timeout: 0``, so the shared request layer
#     contributes no abort of its own either.
#
# So ``isAbort ⇒ user cancel`` is sound TODAY. The guard is rewritten to pin
# the USER-VISIBLE property that still matters — a cancel must never be
# labelled a network error, and a non-abort failure must never be labelled a
# cancel — plus the structural precondition the inference rests on. The moment
# someone reintroduces a watchdog, the precondition test fails and points here,
# which is exactly when the explicit flag has to come back.


def test_image_gen_cancel_is_labelled_as_cancel_not_network_error():
    """The user-visible outcome, independent of HOW cancel is detected.

    Anchored on the real catch block via ``brace_block`` rather than a fixed
    byte window: measured, the old ``src[catch_start:catch_start + 1800]``
    slice overshot the true 820-byte block by ~1 KB and read into neighbouring
    code, which is how it kept "passing" while the token it asserted on had
    already been deleted from the file.
    """
    from tests._source_scan import brace_block

    src = _read(IMAGE_GEN_JS)
    catch = brace_block(src, "const isAbort = err.name === 'AbortError';")

    # An abort must be classified as a cancel …
    assert 'isUserCancel' in catch, (
        'the catch block no longer derives a user-cancel verdict at all')
    assert "'Cancelled by user.'" in catch, (
        'a cancelled generation must say so, not report a network failure')
    # … and drive BOTH the title and the machine-readable errorType.
    assert "'Cancelled'" in catch, 'the error card title must read Cancelled'
    assert "'cancelled'" in catch, (
        "errorType must be 'cancelled' so the renderer picks the cancel style, "
        "not the network-error style")
    # A NON-abort failure must still be labelled a network error — the
    # complement, without which "always say cancelled" would pass.
    assert "'Network error'" in catch, (
        'a genuine transport failure must still be labelled a network error')
    assert "'network'" in catch, (
        "errorType must stay 'network' for a non-abort failure")
    # A cancel is NOT a timeout: isTimeout must not be set on this path.
    assert 'isTimeout: false' in catch, (
        'the cancel/network path must not claim isTimeout — there is no '
        'client-side timeout on this path any more')


def test_abort_implies_cancel_precondition_still_holds():
    """The STRUCTURAL premise the inference above rests on.

    ``isUserCancel = isAbort`` is only sound while the Cancel button is the
    sole producer of an ``AbortError`` on this path. If anyone reintroduces a
    client-side watchdog (``AbortSignal.timeout``, or a ``setTimeout`` that
    calls ``abort()``), a timeout starts arriving as an AbortError and gets
    mislabelled "Cancelled by user" — a user-visible lie. At that point the
    explicit ``_igUserCancelled`` flag eb1ddee5 removed has to come back.

    This test is the tripwire for that regression. Comments are stripped first
    (charter #24) so the explanatory prose above — which necessarily mentions
    the very constructs being forbidden — can neither satisfy nor violate it.
    """
    from tests._source_scan import js_function_body, strip_comments

    live = strip_comments(_read(IMAGE_GEN_JS), lang='js', inline=True)

    assert 'AbortSignal.timeout' not in live, (
        'a client-side timeout signal reintroduces a SECOND AbortError '
        'producer, so isAbort no longer implies user-cancel — restore an '
        'explicit cancel flag (see eb1ddee5) before adding this')

    # Any setTimeout whose body aborts is the same regression in another shape.
    for m in re.finditer(r'setTimeout\s*\([^;]{0,200}', live, re.DOTALL):
        assert 'abort' not in m.group(0), (
            'a setTimeout that calls abort() is a watchdog by another name; it '
            'makes a timeout indistinguishable from a user cancel. Offending '
            'snippet: %r' % (m.group(0)[:120],))

    # And the abort must still originate from the Cancel handler.
    #
    # Pinned on the SINGLE-image controller specifically. A bare
    # ``'.abort()' in cancel_fn`` is too loose: the batch loop right below it
    # calls ``ac.abort()``, so deleting the single-path abort entirely left
    # that check satisfied — measured, the NEUTER did not bite. The
    # single-image path is the one whose catch block this file's other test
    # asserts on, so it is the one that has to keep producing the AbortError.
    cancel_fn = js_function_body(_read(IMAGE_GEN_JS), '_igCancelGeneration')
    assert re.search(r'_igAbortController\s*(?:\?)?\.abort\s*\(', cancel_fn), (
        '_igCancelGeneration no longer aborts the SINGLE-image request '
        '(_igAbortController) — the Cancel button is the thing that makes '
        'isAbort mean "user cancel" on the path the catch-block test covers. '
        'A batch-only abort does not substitute for it.')
    assert re.search(r'_igAbortControllers\b[^\n]*abort', cancel_fn), (
        'the batch controllers are no longer aborted — Cancel would leave '
        'batch requests running')



# ── ③ dead scheduler UI removed ───────────────────────────────────────────

def test_scheduler_dead_ui_fully_removed():
    assert not os.path.exists(SCHEDULER_JS), 'static/js/scheduler.js still exists'
    bundler = _read(BUNDLER)
    assert "'scheduler.js'," not in bundler, 'scheduler.js still in _CLASSIC_ASSET_FILES'
    main = _read(MAIN_JS)
    assert '_applySchedulerUI' not in main, 'main.js still defines/calls _applySchedulerUI'


def test_NEUTER_scheduler_scans_fire_on_old_shape():
    """NEUTER: reintroduce the markers — every absence-scan must fire."""
    bundler = _read(BUNDLER) + "    'scheduler.js',\n"
    assert "'scheduler.js'," in bundler
    main = _read(MAIN_JS) + '\nfunction _applySchedulerUI() {}\n'
    assert '_applySchedulerUI' in main


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
