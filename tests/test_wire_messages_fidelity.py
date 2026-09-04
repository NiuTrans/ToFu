#!/usr/bin/env python3
"""Negative-control test for the debug-panel single-source-of-truth wire form.

Proves the debug panel is FAITHFUL: the COLD path (the ``/debug-messages``
endpoint, ``build_wire_messages(mode='snapshot')``) and the HOT path (the live
orchestrator snapshot: ``_transform_messages`` → ``compose_task_context`` →
``apply_wire_sanitize`` AFTER ``sort_tool_results``) produce a BYTE-IDENTICAL
OpenAI-form message array given the same provider context.

The equality assertion alone is worthless unless it has teeth, so this file is
driven by ``TOFU_WIRE_REVERT`` to run in three states:

  * unset  → both paths correct → byte-identical → PASS
  * 'inject' → REVERT (i): the cold path skips ``compose_task_context``
      (what the old endpoint did) → cold system text drops to 0 chars →
      cold != hot → the equality test FAILS.
  * 'sort'   → REVERT (ii): the hot path emits its snapshot BEFORE
      ``sort_tool_results`` (the old orchestrator emission point) → tool
      results stay in author order [call_zzz, call_aaa] while cold has them
      sorted [call_aaa, call_zzz] → cold != hot → the equality test FAILS.

Run all three (see this module's ``__main__`` or the CHANGE.md commands):
    TOFU_WIRE_REVERT=inject python -m pytest tests/test_wire_messages_fidelity.py
    TOFU_WIRE_REVERT=sort   python -m pytest tests/test_wire_messages_fidelity.py
    python -m pytest tests/test_wire_messages_fidelity.py
"""

import json
import os
import sys

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


_REVERT = os.environ.get('TOFU_WIRE_REVERT', '')


# ── Adversarial fixture (inline; mirrors debug/_scratch/adversarial.json) ──
# Designed to exercise the wire transforms: a full toolRounds batch whose
# toolCallIds are in REVERSE lexical order (so sort_tool_results visibly
# reorders), and a whitespace-only user turn (so _fix_empty_user_messages
# visibly rewrites). The trailing consecutive assistants exercise the
# builder's own merge (a finding: redundant at the wire layer).
# NOTE: the whitespace turn MUST NOT be the tail — compose_task_context
# splices the Current-date reminder into the TRUE tail (turning its string
# content into a non-empty block array), which legitimately suppresses the
# empty-user rewrite there (the 2026-07-25 drift). The trailing
# assistant+user turns keep the whitespace turn mid-list so the fix stays
# observable in the real pipeline.
def _fixture():
    return [
        {'role': 'user', 'content': 'Tell me about the policy report.', 'timestamp': 1000},
        {
            'role': 'assistant', 'content': '',
            'toolRounds': [
                {'llmRound': 1, 'roundNum': 1, 'toolCallId': 'call_zzz',
                 'toolName': 'web_search', 'toolArgs': {'query': 'zeta'},
                 'toolContent': 'ZETA search result body', 'status': 'done',
                 'assistantContent': 'Let me search and read.'},
                {'llmRound': 1, 'roundNum': 2, 'toolCallId': 'call_aaa',
                 'toolName': 'read_files', 'toolArgs': {'path': 'alpha.py'},
                 'toolContent': 'ALPHA file content', 'status': 'done'},
            ],
        },
        {'role': 'user', 'content': '   ', 'timestamp': 2000},
        {'role': 'assistant', 'content': 'Noted — continuing.'},
        {'role': 'user', 'content': 'And the pricing section?', 'timestamp': 3000},
    ]


# Config: disable project/memory (no FUSE), enable multi-agent guidance so a block
# is present and the cold/hot system text is non-empty + deterministic.
def _config():
    return {
        'systemPrompt': '',
        'projectEnabled': False,
        'memoryEnabled': False,
        'orchestration': {'multiAgent': 'read_only'},
        'searchEnabled': True,
        'model': 'claude-sonnet-4',
        'systemPromptMode': 'append',
    }


def _inject_params(config):
    project_path = config.get('projectPath') or ''
    return dict(
        user_id=0,
        project_path=project_path,
        project_enabled=bool(config.get('projectEnabled', bool(project_path))),
        memory_enabled=bool(config.get('memoryEnabled', False)),
        search_enabled=bool(config.get('searchEnabled', True)),
        has_real_tools=True,
        conv_id='',
        task={'config': config},
        model=config.get('model', ''),
        system_prompt_mode=config.get('systemPromptMode', 'append'),
    )


def _build_cold(raw, config):
    """Mirror the /debug-messages endpoint (build_wire_messages snapshot)."""
    if _REVERT == 'inject':
        # REVERT (i): the OLD endpoint — transform + sanitize, NO inject.
        from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
        from lib.tasks_pkg.wire_messages import apply_wire_sanitize
        msgs = _transform_messages([dict(m) for m in raw], config)
        return apply_wire_sanitize(msgs, conv_id='')
    from lib.tasks_pkg.wire_messages import build_wire_messages
    return build_wire_messages(
        raw, config, user_id=0, mode='snapshot', conv_id='')


def _build_hot(raw, config):
    """Mirror the live orchestrator: transform → inject → (sort) → sanitize."""
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
    from lib.tasks_pkg.context_composer import compose_task_context
    from lib.tasks_pkg.wire_messages import apply_wire_sanitize

    msgs = _transform_messages([dict(m) for m in raw], config)
    compose_task_context(msgs, **_inject_params(config))

    if _REVERT == 'sort':
        # REVERT (ii): emit BEFORE sort_tool_results — run the sanitize tail
        # WITHOUT the cache-aware reorder (the old emission point at
        # orchestrator.py:1513, which was before sort at :1531).
        from lib.llm_sanitize import (
            _fix_empty_user_messages,
            _fix_orphaned_tool_calls,
            _merge_consecutive_same_role,
            _sanitize_messages,
            _strip_non_api_fields,
        )
        from lib.tasks_pkg.wire_messages import _gateway_sanitize_enabled
        work = [dict(m) for m in msgs]
        clean = _strip_non_api_fields(work)
        if _gateway_sanitize_enabled(''):
            _sanitize_messages(clean)
        clean = _fix_orphaned_tool_calls(clean)
        clean = _merge_consecutive_same_role(clean)
        _fix_empty_user_messages(clean)
        return clean

    return apply_wire_sanitize(msgs, conv_id='')


def _canon(msgs):
    return json.dumps(msgs, sort_keys=True, ensure_ascii=False)


def _tool_order(msgs):
    return [m.get('tool_call_id') for m in msgs if m.get('role') == 'tool']


def _assistant_tool_order(msgs):
    return [tc.get('id')
            for message in msgs if message.get('role') == 'assistant'
            for tc in (message.get('tool_calls') or [])]


def _system_text(msgs):
    out = []
    for m in msgs:
        if m.get('role') != 'system':
            continue
        c = m.get('content')
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(b.get('text', '') for b in c
                       if isinstance(b, dict) and b.get('type') == 'text')
    return '\n'.join(out)


# ── Tests ────────────────────────────────────────────────────────────────

def test_cold_hot_wire_form_byte_identical():
    """THE acceptance criterion: cold == hot, byte-for-byte (provider_id='')."""
    raw, config = _fixture(), _config()
    cold = _build_cold(raw, config)
    hot = _build_hot(raw, config)
    assert _canon(cold) == _canon(hot), (
        f'COLD vs HOT wire form DIVERGED (revert={_REVERT or "none"}).\n'
        f'  cold tool_order={_tool_order(cold)} system_chars={len(_system_text(cold))}\n'
        f'  hot  tool_order={_tool_order(hot)} system_chars={len(_system_text(hot))}'
    )
    _ok(f'cold == hot byte-identical (revert={_REVERT or "none"})')


def test_carrier_transforms_fire():
    """Byte-identity must rest on REAL transforms, not an empty no-op run."""
    raw, config = _fixture(), _config()
    wire = _build_hot(raw, config)
    # (1) The assistant's tool_calls array remains in the model's slot order,
    # while the following tool-result run is canonicalized by tool_call_id.
    # Pairing is by ID, not by the two arrays sharing a positional order.
    assert _assistant_tool_order(wire) == ['call_zzz', 'call_aaa']
    assert _tool_order(wire) == ['call_aaa', 'call_zzz'], (
        f'sort_tool_results did not reorder: {_tool_order(wire)}')
    # (2) empty user rewritten
    user_texts = [m.get('content') for m in wire if m.get('role') == 'user']
    assert '[empty message]' in user_texts, f'empty-user fix did not fire: {user_texts}'
    # (3) system context injected (swarm block present, non-empty)
    sys_txt = _system_text(wire)
    assert len(sys_txt) > 0 and '<parallel_execution>' in sys_txt, (
        'system context not injected into hot path')
    _ok('carrier transforms fire: tool reorder + empty-user fix + system inject')


def test_provider_symmetry_no_gateway_divergence():
    """provider_id='' (auto-detect) and explicit non-sankuai agree on the
    carrier transforms — proving only _sanitize_messages is provider-gated."""
    from lib.tasks_pkg.wire_messages import apply_wire_sanitize
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
    raw, config = _fixture(), _config()
    base = _transform_messages([dict(m) for m in raw], config)
    a = apply_wire_sanitize(base, conv_id='', provider_id='')
    b = apply_wire_sanitize(base, conv_id='', provider_id='openai')
    assert _tool_order(a) == _tool_order(b) == ['call_aaa', 'call_zzz']
    assert [m.get('role') for m in a] == [m.get('role') for m in b]
    _ok('provider symmetry: carrier transforms provider-independent')


def test_wire_preview_uses_gateway_family_and_wire_shape_rules():
    """Preview and real build share family gating plus call/result repair."""
    from lib.tasks_pkg.wire_messages import (
        _gateway_sanitize_enabled,
        apply_wire_sanitize,
    )

    assert _gateway_sanitize_enabled('sankuai_anthropic') is True
    source = [
        {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': '', 'function': {'name': '', 'arguments': {}},
        }]},
        {'role': 'tool', 'tool_call_id': '', 'content': 'typed rejection'},
    ]
    wire = apply_wire_sanitize(source, provider_id='openai')
    call = wire[0]['tool_calls'][0]
    assert call['id']
    assert call['function']['name']
    assert isinstance(call['function']['arguments'], str)
    assert wire[1]['tool_call_id'] == call['id']


def main():
    print()
    print(_color(f'═══ wire_messages fidelity (revert={_REVERT or "none"}) ═══', '36'))
    print()
    tests = [
        test_cold_hot_wire_form_byte_identical,
        test_carrier_transforms_fire,
        test_provider_symmetry_no_gateway_divergence,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
