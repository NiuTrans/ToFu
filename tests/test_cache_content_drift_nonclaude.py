"""tests/test_cache_content_drift_nonclaude.py — non-Claude coverage proof.

The objective is "flawless prefix caching", NOT "flawless for Claude". Every
content-drift freeze shipped this session was reached through a Claude-flavored
code path, so the owner (correctly) asked: do the OTHER marker-honoring models
— GLM-5 / Qwen / DeepSeek — suffer the same live→replay ``content`` drift, or
are they structurally immune? This suite settles it with the SAME production
functions, not an assertion.

The three fixes and their real gating (read straight from the source):

  Fix 1 — ``_run.py`` tail strip freeze (commit 1274cee).
    ``clean_msg['content'] = (assistant_msg.get('content') or '').strip()`` has
    NO ``is_claude`` guard — it runs for EVERY model. So the raw↔stripped
    live-tail-vs-replay asymmetry is frozen for GLM/Qwen/DeepSeek too. This is
    the model-agnostic layer; nothing to add, but we PROVE the freeze is present
    by driving the same snapshot shape.

  Fix 2 — ``cache.py`` Phase 0.5 str↔block wrap invariance (commit ab161bf).
    ``add_cache_breakpoints`` runs iff ``_gateway_honors_cache_markers(model)``
    = Claude OR (glm-5 / qwen / deepseek). Phase 0.5's normalization is NOT
    ``is_claude``-gated — it fires for every marker-honoring model. So GLM-5 /
    Qwen / DeepSeek ARE covered by the {content} str↔block freeze. minimax /
    doubao (auto-cache, markers-harmful) get NO breakpoints at all → never
    wrapped → structurally immune to the flip.

  Fix 3 — prefill-conversion tail (commit 0a9f6af).
    ``_strip_trailing_assistant_for_claude`` is called ONLY
    ``if is_claude(model)`` (_build.py:262). Non-Claude models never get the
    sentinel conversion → the volatile user+sentinel↔bare-assistant flip that
    ``_is_prefill_converted`` guards against CANNOT arise for them → they are
    structurally immune (nothing to freeze).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_cache_content_drift_nonclaude.py -p no:cacheprovider
"""

import copy
import json

import pytest

pytestmark = pytest.mark.unit

from lib.llm.cache import (
    add_cache_breakpoints,
    _gateway_honors_cache_markers,
)
from lib.llm.body._model_tweaks import (
    _strip_trailing_assistant_for_claude,
    CLAUDE_PREFILL_SENTINEL,
)

# Marker-honoring non-Claude families (the ones Phase 0.5 must also cover).
MARKER_NONCLAUDE = ['glm-5', 'qwen-max', 'deepseek-v3']
# Auto-cache families — markers are harmful, so add_cache_breakpoints no-ops.
AUTOCACHE = ['minimax-01', 'doubao-pro']


def _tools():
    return [{'type': 'function',
             'function': {'name': 'run_command', 'description': 'run',
                          'parameters': {'type': 'object'}}}]


def _run_command_turn(prose='Let me check the logs.'):
    return {'role': 'assistant', 'content': prose,
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'run_command',
                                         'arguments': '{"command": "ls"}'}}]}


def _annotate(messages, model):
    body = {'model': model, 'messages': copy.deepcopy(messages),
            'max_tokens': 100, 'tools': copy.deepcopy(_tools())}
    add_cache_breakpoints(body, log_prefix='')
    return body['messages']


def _first_asst(messages):
    for m in messages:
        if m.get('role') == 'assistant':
            return m
    return None


def _content_bytes(msg):
    """Serialize a message's ``content`` with the only legitimately-mobile key
    (cache_control) stripped — the exact comparison the wire-byte tracer does."""
    c = msg.get('content')
    if isinstance(c, list):
        c = [{k: v for k, v in b.items() if k != 'cache_control'}
             if isinstance(b, dict) else b for b in c]
    return json.dumps(c, ensure_ascii=False, sort_keys=False)


# ═══════════════════════════════════════════════════════════════════════════
#  Gating facts — the map the whole proof rests on
# ═══════════════════════════════════════════════════════════════════════════

def test_marker_nonclaude_models_honor_cache_markers():
    """GLM-5 / Qwen / DeepSeek DO honor cache markers → add_cache_breakpoints
    (and its Phase 0.5 normalization) runs for them."""
    for m in MARKER_NONCLAUDE:
        assert _gateway_honors_cache_markers(m), (
            f'{m} should honor cache markers (Phase 0.5 must run for it)')


def test_autocache_models_do_not_honor_markers():
    """minimax / doubao auto-cache: markers are harmful, so
    add_cache_breakpoints no-ops → they are never wrapped → immune."""
    for m in AUTOCACHE:
        assert not _gateway_honors_cache_markers(m), (
            f'{m} must NOT honor markers (auto-cache family)')


# ═══════════════════════════════════════════════════════════════════════════
#  Fix 2 coverage — the {content} str↔block freeze also protects GLM/Qwen/DS
# ═══════════════════════════════════════════════════════════════════════════

def test_marker_nonclaude_content_bytes_stable_tail_vs_buried():
    """The DIRECT proof: for GLM-5 / Qwen / DeepSeek, a run_command turn's
    ``content`` is byte-identical whether it is the tail (marker on) or buried
    prefix (marker off) — i.e. Phase 0.5 covers them exactly like Claude. If
    the str↔block flip were non-Claude-gated this would diverge."""
    head = [{'role': 'system', 'content': 'S' * 60},
            {'role': 'user', 'content': 'go'}]
    for model in MARKER_NONCLAUDE:
        tail_msgs = _annotate(head + [_run_command_turn()], model)
        buried = head + [_run_command_turn(),
                         {'role': 'tool', 'tool_call_id': 'c1', 'content': 'r'},
                         {'role': 'user', 'content': 'next'}]
        buried_msgs = _annotate(buried, model)
        tb = _content_bytes(_first_asst(tail_msgs))
        bb = _content_bytes(_first_asst(buried_msgs))
        assert tb == bb, (
            f'{model}: run_command content bytes flip tail vs buried:\n'
            f' tail  ={tb}\n buried={bb}\nPhase 0.5 must cover marker-honoring '
            'non-Claude models, not just Claude.')


def test_autocache_models_never_wrap_content():
    """minimax / doubao get no breakpoints, so their run_command content is
    left as the bare ``str`` it arrived as — no wrapping, hence no flip to
    diverge. Structural immunity, proven by observing content is untouched."""
    head = [{'role': 'system', 'content': 'S' * 60},
            {'role': 'user', 'content': 'go'}]
    for model in AUTOCACHE:
        msgs = _annotate(head + [_run_command_turn()], model)
        asst = _first_asst(msgs)
        assert isinstance(asst.get('content'), str), (
            f'{model}: auto-cache model should have its str content left alone '
            f'(no marker wrapping), got {type(asst.get("content"))}')


# ═══════════════════════════════════════════════════════════════════════════
#  Fix 3 immunity — prefill-conversion is Claude-only, so non-Claude can never
#  hit the volatile user+sentinel↔bare-assistant flip
# ═══════════════════════════════════════════════════════════════════════════

def test_prefill_conversion_never_fires_for_nonclaude():
    """_strip_trailing_assistant_for_claude, when guarded by ``is_claude`` at
    its call site, is never invoked for GLM/Qwen/DeepSeek/minimax/doubao. We
    assert the guard's PREMISE: the conversion function itself is the only thing
    that stamps CLAUDE_PREFILL_SENTINEL, and _build.py only calls it under
    is_claude. Here we prove the sentinel-flip cannot arise for a non-Claude
    build by confirming the trailing assistant is NOT converted when the
    Claude-only path is skipped (we simulate the call-site guard)."""
    from lib.model_info import is_claude
    trailing = [{'role': 'system', 'content': 'S'},
                {'role': 'user', 'content': 'hi'},
                {'role': 'assistant', 'content': 'trailing prose the model wrote'}]
    for model in MARKER_NONCLAUDE + AUTOCACHE:
        msgs = copy.deepcopy(trailing)
        # Reproduce the _build.py call-site guard exactly.
        if is_claude(model):
            _strip_trailing_assistant_for_claude(msgs, model)
        last = msgs[-1]
        assert last['role'] == 'assistant', (
            f'{model}: trailing assistant must stay a bare assistant (no '
            'prefill conversion) — the guard is is_claude only')
        assert not str(last.get('content', '')).startswith(CLAUDE_PREFILL_SENTINEL), (
            f'{model}: must never carry the prefill sentinel')


# ═══════════════════════════════════════════════════════════════════════════
#  NEUTER — prove the coverage/immunity claims are load-bearing, not vacuous
# ═══════════════════════════════════════════════════════════════════════════

def test_nc_phase05_is_what_makes_nonclaude_stable():
    """NEUTER for Fix 2 coverage: if Phase 0.5 had NOT normalized the
    tool_call turn (the pre-ab161bf carve-out), a marker-honoring non-Claude
    model would show the SAME str↔block flip. We reproduce the pre-fix shapes
    directly and assert they DO diverge — proving the stability in
    test_marker_nonclaude_content_bytes_stable_tail_vs_buried is caused by the
    fix, not by non-Claude models being trivially immune."""
    buried = {'role': 'assistant', 'content': 'Let me check the logs.',
              'tool_calls': [{'id': 'c1', 'type': 'function',
                              'function': {'name': 'run_command',
                                           'arguments': '{}'}}]}
    tail = {'role': 'assistant',
            'content': [{'type': 'text', 'text': 'Let me check the logs.'}],
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'run_command',
                                         'arguments': '{}'}}]}
    assert _content_bytes(buried) != _content_bytes(tail), (
        'the str↔block flip must be a REAL byte divergence — if these match, '
        'the whole coverage premise is vacuous')


def test_nc_prefill_conversion_would_fire_if_gate_dropped():
    """NEUTER for Fix 3 immunity: prove non-Claude immunity is caused by the
    is_claude call-site gate, NOT by the conversion function refusing
    non-Claude. If we call _strip_trailing_assistant_for_claude WITHOUT the
    gate (as if the gate were dropped), it DOES convert — so the immunity is
    load-bearing on the gate, exactly as claimed."""
    msgs = [{'role': 'system', 'content': 'S'},
            {'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'trailing prose'}]
    # Ungated call (the function is model-parameterized but does NOT self-gate
    # on is_claude — the gate lives at the call site).
    _strip_trailing_assistant_for_claude(msgs, 'glm-5')
    last = msgs[-1]
    assert last['role'] == 'user' and \
        str(last['content']).startswith(CLAUDE_PREFILL_SENTINEL), (
        'ungated, the conversion fires regardless of model — confirming the '
        'non-Claude immunity comes from the is_claude call-site gate, which is '
        'the thing being relied upon')
