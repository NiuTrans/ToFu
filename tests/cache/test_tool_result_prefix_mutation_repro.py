"""Live-equivalent reproduction for prefix-cache BREAK finding #7.

Drives the SAME ``detect_cache_break`` (via ``replay_rounds``) across a
two-turn sequence that mirrors the production signature: the SAME
``tool_call_id``'s ``tool_result`` content changes between turns
(``tool_result(<id>).tool_result`` — 776/~830 of breaks on 2026-09-24).

This is the byte-level proof that a tool_result content mutation between
turns is a genuine CLIENT-side prefix mutation (``body_change``), not an
upstream / namespace / ttl artifact. It is the acceptance instrument for the
writer fix: once the named writer freezes settled tool_results, a sequence
built from the REAL rebuild path must NOT mutate the field across turns.
"""
from __future__ import annotations

import copy

import pytest

pytestmark = pytest.mark.unit

_CALL_ID = 's_0_a8433036'  # production-shaped tool-name-prefixed id


def _body(tool_result_text: str, *, extra_user: bool):
    """A post-translation body whose prefix ends in an assistant tool_call +
    tool result. ``extra_user`` appends the next user turn so the tool_result
    is INSIDE the immutable prefix on the following round."""
    tool_call = {'id': _CALL_ID, 'type': 'function',
                 'function': {'name': 'search', 'arguments': '{"q":"x"}'}}
    msgs = [
        {'role': 'user', 'content': 'start'},
        {'role': 'assistant', 'content': '', 'tool_calls': [tool_call]},
        {'role': 'tool', 'tool_call_id': _CALL_ID, 'content': tool_result_text},
    ]
    if extra_user:
        msgs.append({'role': 'user', 'content': 'next question'})
    return {'model': 'claude-opus-4', 'messages': msgs,
            'tools': [{'function': {'name': 'search', 'description': 'd',
                                     'parameters': {}}}]}


def test_tool_result_content_mutation_is_a_client_body_change():
    """Round N emits tool_result 'A'; round N+1 emits the SAME id with 'A+more'
    (the production 'content grows between turns' signature). The detector MUST
    classify round N+1 as a client body_change — proving the mutation is real
    and client-side, which is what the writer fix must eliminate."""
    from lib.tasks_pkg.cache_tracking.replay import replay_rounds

    routing = {'key_hash': 'k', 'anthropic_beta': 'pc', 'endpoint': 'e'}
    rounds = [
        {'body': _body('result payload v1', extra_user=False),
         'cache_read': 50000, 'cache_write': 30000, 'routing': routing},
        # Same tool_call_id, but the tool_result content CHANGED (grew).
        {'body': _body('result payload v1 — appended streaming chunk',
                       extra_user=True),
         'cache_read': 5000, 'cache_write': 90000, 'routing': routing},
    ]
    res = replay_rounds(rounds, user_id=7, conv_id='replay-tr-mutate')
    # The second round MUST be seen as a genuine client body mutation — the
    # thing the writer fix has to prevent. It must NOT be laundered upstream.
    assert res['buckets'].get('body_change', 0) >= 1, res['buckets']
    assert res['buckets'].get('upstream_identical', 0) == 0, res['buckets']


def test_frozen_tool_result_is_byte_stable_across_turns():
    """The FIX contract: when a settled tool_result is replayed byte-identically
    across turns (the freeze the writer must guarantee), the detector sees NO
    body_change on the prefix — only the legitimate tail growth (new user msg).
    This test FAILS if any writer reintroduces a mutation."""
    from lib.tasks_pkg.cache_tracking.replay import replay_rounds

    routing = {'key_hash': 'k', 'anthropic_beta': 'pc', 'endpoint': 'e'}
    stable = 'result payload v1 — final, frozen'
    rounds = [
        {'body': _body(stable, extra_user=False),
         'cache_read': 50000, 'cache_write': 30000, 'routing': routing},
        # SAME tool_call_id, byte-identical tool_result; only the tail grows.
        {'body': _body(stable, extra_user=True),
         'cache_read': 70000, 'cache_write': 12000, 'routing': routing},
    ]
    res = replay_rounds(rounds, user_id=7, conv_id='replay-tr-stable')
    # A byte-stable prefix with only tail growth must NOT be a body_change.
    assert res['buckets'].get('body_change', 0) == 0, (
        f'a frozen tool_result was reported as mutated: {res["buckets"]} '
        f'rounds={res["rounds"]}')
