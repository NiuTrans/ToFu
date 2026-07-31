"""tests/test_frontend_send_inflight_guard.py — pt_c03fae11 guard.

``conv._sendInFlight`` (main_send_pipeline.js) is set right after the
optimistic user-message push and tells ``syncConversationToServer`` to skip
its PUT (the backend's /chat/send owns the first persist). It is cleared by
the POST's try/finally — but ONLY if control reaches that try. Before the
fix, the region between the flag-set and the try (the "gap": VLM-parse wait,
config build, inject-mode prompt) had two leak shapes:

  1. The conv-switch early return inside the inject-mode prompt block
     returned WITHOUT clearing the flag → every future
     syncConversationToServer PUT for that conversation was silently
     skipped → the user's later messages/title edits vanished on refresh.
  2. A throw from any of the gap awaits (``_waitForVlmParsing`` /
     ``_buildConvConfig`` / ``_promptInjectMode``) leaked the flag the same
     way — there was no catch before the POST's try.

This suite pins the structural contract on the REAL shipped source:

  * No ``return`` inside the gap may execute without a preceding
    ``_sendInFlight = false``.
  * The gap must contain a ``catch`` that clears the flag (throw shield).

Each check carries a byte-reverting NEUTER that re-creates the old shape in
memory and asserts the scanner fires.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_frontend_send_inflight_guard.py -v
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINE = os.path.normpath(
    os.path.join(HERE, '..', 'static', 'js', 'main', 'main_send_pipeline.js'))


def _gap_region(src: str) -> str:
    """The source between `conv._sendInFlight = true;` and the POST's try
    (the try whose body performs _buildConvSettings + Api.chat.send)."""
    set_pos = src.find('conv._sendInFlight = true;')
    assert set_pos != -1, 'could not locate the _sendInFlight flag-set'
    settings_pos = src.find('await _buildConvSettings(', set_pos)
    assert settings_pos != -1, 'could not locate _buildConvSettings'
    post_try = src.rfind('try {', set_pos, settings_pos)
    assert post_try != -1, 'could not locate the POST try block'
    return src[set_pos:post_try]


def _violations(src: str) -> list[str]:
    gap = _gap_region(src)
    out: list[str] = []
    # 1. No `return` in the gap without a preceding flag clear.
    clear_seen = False
    for line in gap.splitlines():
        if '_sendInFlight = false' in line:
            clear_seen = True
        if re.match(r'\s*return;', line) and not clear_seen:
            out.append(f'return without preceding _sendInFlight clear: {line.strip()!r}')
    # 2. The gap must contain a catch that clears the flag (throw shield —
    #    the gap awaits can reject, and without a catch the flag wedges).
    catch_pos = gap.find('} catch (')
    if catch_pos == -1:
        out.append('gap has no catch block — a throw from a gap await leaks _sendInFlight')
    elif '_sendInFlight = false' not in gap[catch_pos:]:
        out.append('gap catch does not clear _sendInFlight')
    return out


def test_no_unguarded_return_in_sendinflight_gap():
    with open(PIPELINE, encoding='utf-8') as f:
        src = f.read()
    v = _violations(src)
    assert not v, (
        '_sendInFlight leak path(s) in main_send_pipeline.js — every pre-POST '
        'exit must clear the flag or the conversation stops syncing forever '
        '(pt_c03fae11):\n  ' + '\n  '.join(v))


def test_NEUTER_return_without_clear_is_flagged():
    """Byte-reverting NEUTER ①: strip the abort branch's flag clear (the exact
    pre-fix shape) — the scanner MUST report the unguarded return."""
    with open(PIPELINE, encoding='utf-8') as f:
        src = f.read()
    anchor = 'conv._sendInFlight = false;\n        const _syncedAbort'
    assert anchor in src, 'NEUTER anchor missing — the abort branch shape changed'
    neutered = src.replace(anchor, 'const _syncedAbort', 1)
    v = _violations(neutered)
    assert any('return without preceding' in x for x in v), (
        f'NEUTER FAILED: removing the clear did not flag the return (got {v})')


def test_NEUTER_missing_catch_clear_is_flagged():
    """Byte-reverting NEUTER ②: strip the gap catch's flag clear — the scanner
    MUST report that the throw shield no longer clears."""
    with open(PIPELINE, encoding='utf-8') as f:
        src = f.read()
    anchor = "debugLog('Failed: ' + ((preSendErr && preSendErr.message) || preSendErr), 'error');\n    conv._sendInFlight = false;"
    assert anchor in src, 'NEUTER anchor missing — the gap catch shape changed'
    neutered = src.replace(
        anchor,
        "debugLog('Failed: ' + ((preSendErr && preSendErr.message) || preSendErr), 'error');",
        1)
    v = _violations(neutered)
    assert any('catch' in x for x in v), (
        f'NEUTER FAILED: removing the catch clear was not flagged (got {v})')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
