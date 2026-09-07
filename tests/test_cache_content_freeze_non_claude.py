"""tests/test_cache_content_freeze_non_claude.py — content-freeze coverage for
NON-Claude marker-honoring models (GLM-5 / Qwen / DeepSeek).

The objective is "flawless prefix caching", not "flawless for Claude". Every
content-freeze fix this cycle must be checked against the other models that
honor cache markers (``_CACHE_MARKERS_HELP`` = glm-5 / qwen / deepseek). Those
run the OpenAI protocol — the body is serialized AS-IS (no
``openai_body_to_anthropic``), so the gateway prompt-cache matches on the raw
bytes directly; a str↔block flip is a RAW-byte break it sees immediately.

Coverage matrix PROVEN here:
  * str↔block Phase 0.5 normalization (commit ab161bf) is NOT ``is_claude``-gated
    → it runs for these models too, so a run_command turn's ``content`` stays the
    single-block form whether it is the tail (marker on) or buried (marker off).
    NEUTER: the raw str-form vs list-form bytes DIFFER (the flip WOULD break their
    cache without the normalization) — so the fix is load-bearing, not vacuous.
  * The ``reasoning_content`` these models carry is replayed VERBATIM (no
    Claude ``reasoning_details`` rebuild, which is ``is_claude``-gated and N/A
    here), so it does not drift round-over-round.

The two Claude-only wire-shape fixes (``reasoning_details`` rebuild,
prefill-conversion sentinel) are N/A for these models — they never produce those
wire shapes — which is a structural-immunity claim this suite makes explicit.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_cache_content_freeze_non_claude.py -p no:cacheprovider
"""

import copy
import json

import pytest

pytestmark = pytest.mark.unit

from lib.llm import build_body, add_cache_breakpoints
from lib.llm.body._model_tweaks import _inject_claude_reasoning_details
from lib.model_info import is_claude

# The non-Claude models that honor cache markers (lib/llm/cache.py _CACHE_MARKERS_HELP).
_NON_CLAUDE_MARKER_MODELS = ['glm-5', 'qwen-max', 'deepseek-chat']


def _tools():
    return [{'type': 'function',
             'function': {'name': 'run_command', 'description': 'run',
                          'parameters': {'type': 'object'}}}]


def _turn():
    return {'role': 'assistant', 'content': 'Analysis done.',
            'reasoning_content': 'my thinking',
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'run_command',
                                         'arguments': '{"command": "ls"}'}}]}


def _wire(model, msgs):
    b = build_body(model, copy.deepcopy(msgs), max_tokens=100,
                   thinking_enabled=True, tools=copy.deepcopy(_tools()))
    b['_task_id'] = ''
    add_cache_breakpoints(b, log_prefix='')
    return b['messages']  # OpenAI protocol: serialized as-is (no anthropic xlate)


def _first_tool_asst(msgs):
    for m in msgs:
        if m.get('role') == 'assistant' and m.get('tool_calls'):
            return m
    return None


def _msg_bytes(m):
    cc = m.get('content')
    if isinstance(cc, list):
        cc = [{k: v for k, v in b.items() if k != 'cache_control'}
              if isinstance(b, dict) else b for b in cc]
    d = dict(m)
    d['content'] = cc
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


@pytest.mark.parametrize('model', _NON_CLAUDE_MARKER_MODELS)
def test_non_claude_content_byte_stable_across_rounds(model):
    """A run_command turn (prose + reasoning_content) is byte-identical whether
    near-tail (round N) or buried (round N+1) for the non-Claude marker models —
    the str↔block Phase 0.5 fix covers them (it is not is_claude-gated)."""
    assert not is_claude(model)
    head = [{'role': 'system', 'content': 'S' * 60},
            {'role': 'user', 'content': 'go'}]
    core = [_turn(), {'role': 'tool', 'tool_call_id': 'c1', 'content': 'result'}]
    round_n = _wire(model, head + core + [{'role': 'user', 'content': 'q2'}])
    round_n1 = _wire(model, head + core + [
        {'role': 'user', 'content': 'q2'}, _turn(),
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'r2'},
        {'role': 'user', 'content': 'q3'}])
    a1, a2 = _first_tool_asst(round_n), _first_tool_asst(round_n1)
    assert a1 is not None and a2 is not None
    assert isinstance(a1.get('content'), list) and isinstance(a2.get('content'), list), (
        'content should be normalized to the single-block form for non-Claude too')
    assert _msg_bytes(a1) == _msg_bytes(a2), (
        f'{model}: run_command turn content flips bytes tail↔buried — the '
        'str↔block normalization must not be is_claude-gated')


def test_nc_str_vs_block_would_break_non_claude_raw_bytes():
    """NEUTER: on the OpenAI protocol the body is serialized AS-IS, so a
    str-content vs single-text-block-content turn are DIFFERENT raw bytes — the
    gateway prompt-cache would see the flip directly. This proves the Phase 0.5
    normalization is load-bearing for non-Claude (not that they are immune)."""
    buried = {'role': 'assistant', 'content': 'Checking.',
              'tool_calls': [{'id': 'c1', 'type': 'function',
                              'function': {'name': 'run_command', 'arguments': '{}'}}]}
    tail = {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Checking.'}],
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'run_command', 'arguments': '{}'}}]}
    assert (json.dumps(buried, ensure_ascii=False, sort_keys=False)
            != json.dumps(tail, ensure_ascii=False, sort_keys=False)), (
        'str-form and block-form must differ as raw bytes — that is the flip '
        'the normalization removes; if they are equal the NEUTER premise is gone')


@pytest.mark.parametrize('model', _NON_CLAUDE_MARKER_MODELS)
def test_non_claude_reasoning_details_not_injected(model):
    """The Claude reasoning_details rebuild is is_claude-gated and must NOT fire
    for non-Claude models — they carry reasoning_content verbatim (no synthesized
    reasoning_details field to drift). Structural-immunity claim made explicit."""
    m = [{'role': 'assistant', 'content': 'x', 'reasoning_content': 'think',
          'thinking_signature': 'sig'}]
    _inject_claude_reasoning_details(m, model)
    assert 'reasoning_details' not in m[0], (
        f'{model}: reasoning_details was injected — the Claude-only wire shape '
        'must not leak onto non-Claude models')
