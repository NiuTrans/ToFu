"""Unit tests for lib.tool_input_repair.

Covers the five repair patterns (Awais open-model-harness + stringified
primitive), the load-bearing ordering of stringified_json before
bare_string_to_array, and the no-op guarantee on already-valid inputs.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.tool_input_repair import resolve_tool_name, validate_then_repair  # noqa: E402


def test_valid_inputs_are_never_touched():
    args = {'reads': [{'path': 'lib/server.py'}]}
    out, log = validate_then_repair('read_files', args)
    assert log == []
    assert out == args
    # original dict identity preserved when nothing changed
    assert out['reads'] is args['reads']


def test_bare_string_wrap_to_array():
    out, log = validate_then_repair('read_files', {'reads': 'lib/server.py'})
    assert out == {'reads': ['lib/server.py']}
    assert log == [('reads', 'bare_string_to_array')]


def test_stringified_json_decoded_before_bare_wrap():
    """Ordering test: a JSON string must decode, NOT be wrapped as ['...']."""
    out, log = validate_then_repair(
        'grep_search',
        {'searches': '[{"pattern": "foo"}, {"pattern": "bar"}]'},
    )
    assert out['searches'] == [{'pattern': 'foo'}, {'pattern': 'bar'}]
    assert log == [('searches', 'stringified_json')]


def test_stringified_int_coerced():
    out, log = validate_then_repair(
        'grep_search',
        {'pattern': 'foo', 'max_results': '20', 'context_lines': '3'},
    )
    assert out['max_results'] == 20
    assert out['context_lines'] == 3
    assert ('max_results', 'stringified_primitive') in log
    assert ('context_lines', 'stringified_primitive') in log


def test_stringified_bool_coerced():
    out, log = validate_then_repair(
        'grep_search',
        {'pattern': 'x', 'count_only': 'true'},
    )
    assert out['count_only'] is True
    assert ('count_only', 'stringified_primitive') in log


def test_null_omission_drops_optional_key():
    out, log = validate_then_repair(
        'grep_search',
        {'pattern': 'x', 'include': None},
    )
    assert 'include' not in out
    assert log == [('include', 'null_omission')]


def test_null_kept_when_required():
    """null on a REQUIRED key must NOT be dropped — the model needs to fix it.

    list_dir declares ``required: ['path']`` — null_omission must skip it.
    """
    out, log = validate_then_repair('list_dir', {'path': None})
    assert 'path' in out
    assert out['path'] is None
    assert log == []


def test_empty_placeholder_unwrap():
    """{'a': 'x', 'b': 'y'} where array expected → ['x', 'y']."""
    out, log = validate_then_repair(
        'grep_search',
        {'pattern': 'x', 'searches': {'first': 'a', 'second': 'b'}},
    )
    assert out['searches'] == ['a', 'b']
    assert log == [('searches', 'empty_placeholder_unwrap')]


def test_unknown_tool_passes_through():
    args = {'anything': '["x"]'}
    out, log = validate_then_repair('not_a_real_tool', args)
    assert out == args
    assert log == []


def test_non_dict_args_passes_through():
    out, log = validate_then_repair('read_files', None)
    assert out == {}
    assert log == []


def test_multiple_repairs_in_one_call():
    """Real-world combo: bad reads, stringified max_results, null include."""
    out, log = validate_then_repair(
        'grep_search',
        {
            'pattern': 'foo',
            'searches': '[{"pattern": "a"}]',
            'max_results': '5',
            'include': None,
        },
    )
    assert out['searches'] == [{'pattern': 'a'}]
    assert out['max_results'] == 5
    assert 'include' not in out
    patterns = {p for _, p in log}
    assert 'stringified_json' in patterns
    assert 'stringified_primitive' in patterns
    assert 'null_omission' in patterns


def test_leaked_tool_call_markup_in_reads():
    """Conv mpus9bcfbrkbvq: Opus leaked <parameter name="path">VALUE into reads.

    The markup must be stripped AND the recovered path wrapped into the
    expected array — a single repair labelled leaked_tool_call_syntax.
    """
    out, log = validate_then_repair(
        'read_files',
        {'reads': '\n<parameter name="path">CLAUDE.md'},
    )
    assert out['reads'] == ['CLAUDE.md']
    assert log == [('reads', 'leaked_tool_call_syntax')]


def test_leaked_tool_call_markup_with_extra_line_args():
    """The start_line/end_line siblings are untouched; reads recovered."""
    out, log = validate_then_repair(
        'read_files',
        {'reads': '\n<parameter name="path">lib/swarm/integration.py'},
    )
    assert out['reads'] == ['lib/swarm/integration.py']
    assert log == [('reads', 'leaked_tool_call_syntax')]


def test_leaked_markup_absent_leaves_string_to_normal_wrap():
    """A plain string with a '<' but no tool-call markup is NOT mis-stripped."""
    out, log = validate_then_repair('read_files', {'reads': 'a<b.py'})
    assert out['reads'] == ['a<b.py']
    assert log == [('reads', 'bare_string_to_array')]


def test_stringified_json_not_decodable_falls_back_to_wrap():
    """A non-JSON string in an array slot becomes a single-element array."""
    out, log = validate_then_repair('read_files', {'reads': 'not json'})
    assert out == {'reads': ['not json']}
    assert log == [('reads', 'bare_string_to_array')]


def test_malformed_json_array_not_wrapped():
    """A string that LOOKS like a JSON array but fails to parse (unescaped
    inner quotes) must be LEFT UNTOUCHED — wrapping it into ['[{...}]'] only
    hides the error one layer deeper (conv mpyv4vq9qod3dr 'Invalid edit
    entry' bug)."""
    bad = '[{"path": "x.py", "search": "the "quoted" word", "replace": "y"}]'
    out, log = validate_then_repair('apply_diffs', {'edits': bad})
    # Unchanged: still the raw malformed string, no repair logged.
    assert out == {'edits': bad}
    assert log == []


def test_malformed_json_object_not_wrapped():
    """Same guard for a '{'-leading malformed blob in an array slot."""
    bad = '{"path": "x.py" "search": "a"}'  # missing comma
    out, log = validate_then_repair('apply_diffs', {'edits': bad})
    assert out == {'edits': bad}
    assert log == []


def test_stringified_array_with_trailing_comma_recovered():
    """A stringified array with a trailing comma is recovered via the
    lenient fallback inside stringified_json (not left as a raw string)."""
    out, log = validate_then_repair('read_files', {'reads': '[{"path": "a.py"},]'})
    assert out == {'reads': [{'path': 'a.py'}]}
    assert log == [('reads', 'stringified_json')]


def test_stringified_array_truncated_recovered():
    """A stringified array truncated mid-stream (missing closers) is
    recovered — the failure mode behind 'reads expects an array' rejections
    when the model emits ``reads`` as a slightly-malformed JSON string."""
    out, log = validate_then_repair(
        'read_files',
        {'reads': '[{"path": "a.py", "start_line": 1, "end_line": 10}, {"path": "b.py"}'},
    )
    assert out == {'reads': [{'path': 'a.py', 'start_line': 1, 'end_line': 10},
                             {'path': 'b.py'}]}
    assert log == [('reads', 'stringified_json')]


# ═════════════════════════════════════════════════════
#  Tool-NAME repair (resolve_tool_name)
# ═════════════════════════════════════════════════════

_KNOWN = {
    'read_files', 'grep_search', 'list_dir', 'find_files', 'write_file',
    'apply_diff', 'apply_diffs', 'insert_content', 'insert_contents',
    'run_command', 'web_search', 'fetch_url', 'create_project',
    'mcp__github__create_issue',
}


def test_resolve_exact_name_untouched():
    assert resolve_tool_name('read_files', known=_KNOWN) == ('read_files', None)


def test_resolve_static_aliases():
    cases = {
        'read_file': 'read_files',
        'read_text': 'read_files',
        'cat': 'read_files',
        'bash': 'run_command',
        'shell': 'run_command',
        'ls': 'list_dir',
        'grep': 'grep_search',
        'grep_file': 'grep_search',
        'write_files': 'write_file',
        'create_file': 'write_file',
        'edit': 'apply_diff',
        'find': 'find_files',
        'fetch': 'fetch_url',
    }
    for wrong, canonical in cases.items():
        name, kind = resolve_tool_name(wrong, known=_KNOWN)
        assert name == canonical, f'{wrong!r} -> {name!r}, expected {canonical!r}'
        assert kind == 'alias'


def test_resolve_casefold_match():
    """Claude-Code CamelCase / stray capitalisation resolves case-insensitively."""
    assert resolve_tool_name('Grep_Search', known=_KNOWN) == ('grep_search', 'casefold')
    assert resolve_tool_name('READ_FILES', known=_KNOWN) == ('read_files', 'casefold')


def test_resolve_camelcase_via_alias():
    """'Read'/'Grep' hit the lowercase static alias before casefold."""
    assert resolve_tool_name('Read', known=_KNOWN) == ('read_files', 'alias')
    assert resolve_tool_name('Grep', known=_KNOWN) == ('grep_search', 'alias')


def test_resolve_never_invents_unknown_target():
    """An alias whose target is NOT in this session's known set is not applied."""
    assert resolve_tool_name('read_file', known={'list_dir'}) == ('read_file', None)


def test_resolve_unknown_passes_through():
    assert resolve_tool_name('totally_made_up_xyz', known=_KNOWN) == ('totally_made_up_xyz', None)


def test_resolve_mcp_tool_untouched():
    """A real MCP tool is an exact match and must never be aliased away."""
    assert resolve_tool_name('mcp__github__create_issue', known=_KNOWN) == (
        'mcp__github__create_issue', None)


def test_resolve_empty_name():
    assert resolve_tool_name('', known=_KNOWN) == ('', None)


if __name__ == '__main__':
    import traceback
    failed = 0
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                passed += 1
                print(f'PASS {name}')
            except Exception:
                failed += 1
                print(f'FAIL {name}')
                traceback.print_exc()
    print(f'\n{passed} passed, {failed} failed')
    sys.exit(0 if failed == 0 else 1)
