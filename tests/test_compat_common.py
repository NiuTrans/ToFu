#!/usr/bin/env python3
"""Unit tests for lib.compat._common — the shared compat request helpers.

Both ``translate_openai_request`` and ``translate_anthropic_request`` now route
their common cfg mapping (model/preset, temperature, max_tokens→maxTokens,
top_p→topP, tools, tool_choice) and their identical tools-disable +
headless-personal-defaults policy through this module. This suite pins the
helper behavior AND asserts the two translators produce the same shared cfg
subset for an equivalent request (the parity the extraction guarantees).

Run directly (``python tests/test_compat_common.py``) or via pytest.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def test_short_id_shape():
    from lib.compat._common import short_id
    a = short_id('chatcmpl-')
    assert a.startswith('chatcmpl-') and len(a) == len('chatcmpl-') + 24
    b = short_id('msg_', 16)
    assert b.startswith('msg_') and len(b) == len('msg_') + 16
    c = short_id(n=16)
    assert len(c) == 16 and short_id() != short_id()  # unique, prefixless
    _ok('short_id: prefix + n-hex shape, unique')


def test_apply_common_cfg_maps_shared_fields():
    from lib.compat._common import apply_common_cfg
    cfg = {}
    apply_common_cfg(cfg, {
        'model': 'gpt-x', 'temperature': 0.5, 'max_tokens': 100,
        'top_p': 0.9, 'tools': [{'x': 1}], 'tool_choice': 'auto',
    })
    assert cfg == {
        'model': 'gpt-x', 'preset': 'gpt-x', 'temperature': 0.5,
        'maxTokens': 100, 'topP': 0.9, 'tools': [{'x': 1}],
        'toolChoice': 'auto',
    }
    _ok('apply_common_cfg: maps model/temp/max_tokens/top_p/tools/tool_choice')


def test_apply_common_cfg_omits_absent():
    from lib.compat._common import apply_common_cfg
    cfg = {}
    apply_common_cfg(cfg, {'model': 'm'})
    assert cfg == {'model': 'm', 'preset': 'm'}  # nothing else set
    _ok('apply_common_cfg: absent fields not injected')


def test_tools_disable_only_with_tools():
    from lib.compat._common import apply_tools_and_personal_defaults
    with_tools = {}
    apply_tools_and_personal_defaults(with_tools, {'tools': [{'x': 1}]})
    assert with_tools['searchMode'] == 'off'
    assert with_tools['fetchEnabled'] is False
    assert with_tools['mcpEnabled'] is False

    no_tools = {}
    apply_tools_and_personal_defaults(no_tools, {})
    # No explicit tools → the disable block does NOT fire.
    assert 'searchMode' not in no_tools
    _ok('apply_tools_and_personal_defaults: disables auto-tools only with tools[]')


def test_tools_disable_respects_explicit_caller_cfg():
    from lib.compat._common import apply_tools_and_personal_defaults
    cfg = {'searchMode': 'web'}  # caller already chose
    apply_tools_and_personal_defaults(cfg, {'tools': [{'x': 1}]})
    assert cfg['searchMode'] == 'web'  # setdefault must not clobber
    _ok('apply_tools_and_personal_defaults: setdefault preserves caller cfg')


def test_translators_agree_on_shared_cfg_subset():
    """The extraction's whole point: both translators map the shared fields
    identically for an equivalent request."""
    from lib.compat.openai import translate_openai_request
    from lib.compat.anthropic import translate_anthropic_request

    common = {'model': 'claude-x', 'temperature': 0.3, 'max_tokens': 512,
              'top_p': 0.8, 'tools': [{'name': 't'}], 'tool_choice': 'auto',
              'messages': [{'role': 'user', 'content': 'hi'}]}
    _o_msgs, o_cfg, _o = translate_openai_request(dict(common))
    _a_msgs, a_cfg, _a = translate_anthropic_request(dict(common))

    shared_keys = ('model', 'preset', 'temperature', 'maxTokens', 'topP',
                   'tools', 'toolChoice', 'searchMode', 'fetchEnabled',
                   'mcpEnabled')
    for k in shared_keys:
        assert o_cfg.get(k) == a_cfg.get(k), f'cfg[{k}] diverged: {o_cfg.get(k)!r} vs {a_cfg.get(k)!r}'
    _ok('translators: shared cfg subset identical for equivalent request')


def main():
    print()
    print(_color('═══ lib/compat/_common Unit Tests ═══', '36'))
    print()
    tests = [
        test_short_id_shape,
        test_apply_common_cfg_maps_shared_fields,
        test_apply_common_cfg_omits_absent,
        test_tools_disable_only_with_tools,
        test_tools_disable_respects_explicit_caller_cfg,
        test_translators_agree_on_shared_cfg_subset,
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
