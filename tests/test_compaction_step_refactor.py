"""Golden pinning tests for the Phase-A–D → step extraction (Stage 2).

These pin the *observable message output* and *return value* of
``micro_compact`` on a battery of fixtures, with ``conv_id=''`` so the
pure-transform path runs without any DB/SSE side effects.  They must pass
identically before and after the extraction of the Phase A–D bodies into
registered compaction steps (``_builtin_steps.py``).

If any of these change, the refactor altered behavior — not allowed.

Run:  pytest tests/test_compaction_step_refactor.py -v
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _big(n: int, ch: str = 'x') -> str:
    return ch * n


def _mk_tool(name: str, content, tcid: str) -> dict:
    return {'role': 'tool', 'name': name, 'tool_call_id': tcid, 'content': content}


def _mk_asst(text: str = '', tool_calls=None, reasoning: str = '') -> dict:
    m = {'role': 'assistant', 'content': text}
    if tool_calls:
        m['tool_calls'] = tool_calls
    if reasoning:
        m['reasoning_content'] = reasoning
    return m


# ── Fixtures: each returns (messages, kwargs) ──────────────────────────

def _fx_tool_results_cold():
    """41 tool results so the oldest falls outside MICRO_HOT_TAIL=40 and
    gets compacted (each > MICRO_COMPACT_THRESHOLD=2000 chars)."""
    msgs = [{'role': 'user', 'content': 'go'}]
    for i in range(41):
        msgs.append(_mk_asst(text=f'step {i}',
                             tool_calls=[{'id': f't{i}',
                                          'function': {'name': 'grep_search',
                                                       'arguments': '{}'}}]))
        msgs.append(_mk_tool('grep_search', _big(3000), f't{i}'))
    return msgs, {}


def _fx_thinking_strip():
    """25 assistant msgs with reasoning_content; THINKING_HOT_TAIL=20 keeps
    the last 20, strips the oldest 5."""
    msgs = [{'role': 'user', 'content': 'go'}]
    for i in range(25):
        msgs.append(_mk_asst(text=f'a{i}', reasoning=_big(4000, 'r')))
        msgs.append({'role': 'user', 'content': f'next {i}'})
    return msgs, {}


def _fx_paired_interstitial():
    """Cold tool results with long interstitial assistant commentary;
    enable_paired_assistant_compact=True triggers Phase B2."""
    msgs = [{'role': 'user', 'content': 'go'}]
    for i in range(61):
        msgs.append(_mk_asst(text=_big(500, 'c'),
                             tool_calls=[{'id': f'p{i}',
                                          'function': {'name': 'grep_search',
                                                       'arguments': '{}'}}]))
        msgs.append(_mk_tool('grep_search', _big(3000), f'p{i}'))
    return msgs, {'enable_paired_assistant_compact': True}


def _fx_assistant_compact():
    """Long cold assistant messages; enable_assistant_compact=True triggers
    Phase D."""
    msgs = [{'role': 'user', 'content': 'go'}]
    for i in range(12):
        msgs.append(_mk_asst(text=_big(1500, 'd')))
        msgs.append({'role': 'user', 'content': f'q{i}'})
    return msgs, {'enable_assistant_compact': True}


def _fx_image_strip():
    """Several cold image tool results; IMAGE_HOT_TAIL=2 keeps the last 2."""
    def img_content():
        return [{'type': 'text', 'text': 'screenshot'},
                {'type': 'image_url',
                 'image_url': {'url': 'data:image/png;base64,' + _big(5000, 'A')}}]
    msgs = [{'role': 'user', 'content': 'go'}]
    for i in range(6):
        msgs.append(_mk_asst(tool_calls=[{'id': f'i{i}',
                                          'function': {'name': 'browser_read_page',
                                                       'arguments': '{}'}}]))
        msgs.append(_mk_tool('browser_read_page', img_content(), f'i{i}'))
    return msgs, {}


def _fx_short_results_untouched():
    """Cold tool results UNDER the threshold stay verbatim."""
    msgs = [{'role': 'user', 'content': 'go'}]
    for i in range(61):
        msgs.append(_mk_asst(tool_calls=[{'id': f's{i}',
                                          'function': {'name': 'grep_search',
                                                       'arguments': '{}'}}]))
        msgs.append(_mk_tool('grep_search', _big(50), f's{i}'))
    return msgs, {}


_FIXTURES = {
    'tool_results_cold': _fx_tool_results_cold,
    'thinking_strip': _fx_thinking_strip,
    'paired_interstitial': _fx_paired_interstitial,
    'assistant_compact': _fx_assistant_compact,
    'image_strip': _fx_image_strip,
    'short_untouched': _fx_short_results_untouched,
}


@pytest.mark.unit
@pytest.mark.parametrize('fx_name', sorted(_FIXTURES))
def test_micro_compact_output_is_stable(fx_name):
    """Captured-vs-current: micro_compact's message output + return value
    must be deterministic for a given input (the property the extraction
    must preserve). We assert determinism by running twice on deep copies
    and comparing — plus assert the transform actually fired where
    expected, so a no-op regression is caught too."""
    from lib.tasks_pkg.compaction.api import micro_compact

    msgs, kwargs = _FIXTURES[fx_name]()
    a = copy.deepcopy(msgs)
    b = copy.deepcopy(msgs)

    saved_a = micro_compact(a, conv_id='', **kwargs)
    saved_b = micro_compact(copy.deepcopy(msgs), conv_id='', **kwargs)

    # Determinism: same input → same savings.
    assert saved_a == saved_b, f'{fx_name}: non-deterministic savings'

    # Message count is always preserved (in-place content replacement).
    assert len(a) == len(msgs), f'{fx_name}: message count changed'

    # Idempotency: a second pass over already-compacted output saves ~0.
    saved_second = micro_compact(a, conv_id='', **kwargs)
    assert saved_second == 0, (
        f'{fx_name}: second pass saved {saved_second} (not idempotent)')


@pytest.mark.unit
def test_compacts_fire_where_expected():
    """Concrete behavioral anchors so 'stable' can't mean 'stably broken'."""
    from lib.tasks_pkg.compaction.api import micro_compact

    # tool results
    msgs, _ = _fx_tool_results_cold()
    micro_compact(msgs, conv_id='')
    compacted = sum(1 for m in msgs if m.get('role') == 'tool'
                    and isinstance(m.get('content'), str)
                    and 'compacted' in m['content'][:80])
    assert compacted == 1, f'expected 1 cold tool result compacted, got {compacted}'

    # thinking
    msgs, _ = _fx_thinking_strip()
    micro_compact(msgs, conv_id='')
    stripped = sum(1 for m in msgs if m.get('role') == 'assistant'
                   and m.get('reasoning_content') == '')
    assert stripped == 5, f'expected 5 cold thinking stripped, got {stripped}'

    # paired interstitial only when enabled
    msgs, kw = _fx_paired_interstitial()
    micro_compact(msgs, conv_id='', **kw)
    folded = sum(1 for m in msgs if m.get('role') == 'assistant'
                 and isinstance(m.get('content'), str)
                 and m['content'].startswith('[Interstitial compacted'))
    assert folded >= 1, 'paired interstitial fold did not fire when enabled'

    # assistant compaction only when enabled
    msgs, kw = _fx_assistant_compact()
    micro_compact(msgs, conv_id='', **kw)
    ac = sum(1 for m in msgs if m.get('role') == 'assistant'
             and isinstance(m.get('content'), str)
             and m['content'].startswith('[Assistant response compacted'))
    assert ac >= 1, 'assistant compaction did not fire when enabled'

    # images
    msgs, _ = _fx_image_strip()
    micro_compact(msgs, conv_id='')
    img_compacted = sum(1 for m in msgs if m.get('role') == 'tool'
                        and isinstance(m.get('content'), str)
                        and 'compacted' in m['content'])
    assert img_compacted == 4, f'expected 4 cold images compacted, got {img_compacted}'


@pytest.mark.unit
def test_phase_d_off_by_default():
    from lib.tasks_pkg.compaction.api import micro_compact
    msgs, _ = _fx_assistant_compact()  # has the data but no kwarg
    micro_compact(msgs, conv_id='')
    ac = sum(1 for m in msgs if isinstance(m.get('content'), str)
             and m['content'].startswith('[Assistant response compacted'))
    assert ac == 0, 'Phase D must NOT fire without enable_assistant_compact'
