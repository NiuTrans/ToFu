"""Unit tests for lib.tool_input_repair.

Covers the five repair patterns (Awais open-model-harness + stringified
primitive), the load-bearing ordering of stringified_json before
bare_string_to_array, and the no-op guarantee on already-valid inputs.
"""
from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/server.py', 'lib/swarm/integration.py', 'tests/foo.py',
}

import os
import sys

import pytest

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


@pytest.mark.parametrize('zero', [0, 0.0, '0', ' 00 '])
def test_get_conversation_zero_cursor_is_audibly_omitted(zero):
    """A model's conventional first-page sentinel must not waste a tool call."""
    args = {'conversation_id': 'conv-old', 'before': zero, 'limit': 20}

    out, log = validate_then_repair('get_conversation', args)

    assert out == {'conversation_id': 'conv-old', 'limit': 20}
    assert log == [('before', 'zero_cursor_omission')]
    assert args['before'] == zero, 'repair must not mutate provider input'


@pytest.mark.parametrize('invalid', [False, -1, 0.5, '-1', '0.5'])
def test_get_conversation_ambiguous_cursor_is_not_repaired(invalid):
    out, log = validate_then_repair(
        'get_conversation', {'conversation_id': 'conv-old', 'before': invalid})
    assert 'before' in out
    assert all(pattern != 'zero_cursor_omission' for _, pattern in log)


def test_zero_cursor_repair_precedes_authoritative_minimum_validation():
    """The unified ingest accepts only the curated zero sentinel repair."""
    import json

    from lib.tool_input_repair import ingest_tool_call
    from lib.tools.contracts import compile_execution_contract_documents
    from lib.tools.conversation import CONV_REF_GET_TOOL

    contracts = compile_execution_contract_documents([CONV_REF_GET_TOOL])
    zero = ingest_tool_call(
        {'id': 'call-zero', 'function': {
            'name': 'get_conversation',
            'arguments': json.dumps({
                'conversation_id': 'conv-old', 'before': 0}),
        }},
        known_tools={'get_conversation'},
        contract_documents_by_name=contracts,
        emit_audit=False,
    )
    negative = ingest_tool_call(
        {'id': 'call-negative', 'function': {
            'name': 'get_conversation',
            'arguments': json.dumps({
                'conversation_id': 'conv-old', 'before': -1}),
        }},
        known_tools={'get_conversation'},
        contract_documents_by_name=contracts,
        emit_audit=False,
    )

    assert zero.parse_error is None
    assert zero.fn_args == {'conversation_id': 'conv-old'}
    assert zero.repair_log == [('before', 'zero_cursor_omission')]
    assert negative.parse_error is not None
    assert negative.contract_error is not None


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
#  Parameter-KEY alias repair (_apply_param_aliases via validate_then_repair)
# ═════════════════════════════════════════════════════


def test_param_alias_edit_keys_to_apply_diff():
    """Conv-debug screenshot: apply_diff called with Claude *Edit* keys.

    {file_path, old_string, new_string} must be renamed to {path, search,
    replace} BEFORE the type-walk, so the executor gets a real path instead
    of '' (the empty 'File not found:' bug)."""
    out, log = validate_then_repair(
        'apply_diff',
        {'file_path': 'tests/foo.py',
         'old_string': 'window = sse_src[pos:pos + 600]',
         'new_string': 'window = sse_src[pos:pos + 900]'},
    )
    assert out == {'path': 'tests/foo.py',
                   'search': 'window = sse_src[pos:pos + 600]',
                   'replace': 'window = sse_src[pos:pos + 900]'}
    assert set(log) == {('path', 'param_alias'), ('search', 'param_alias'),
                        ('replace', 'param_alias')}


def test_param_alias_valid_keys_untouched():
    """A correct apply_diff call must NOT be touched by the alias pass."""
    args = {'path': 'a.py', 'search': 'x', 'replace': 'y'}
    out, log = validate_then_repair('apply_diff', args)
    assert out == args
    assert log == []


def test_param_alias_does_not_overwrite_canonical():
    """If both the canonical AND the alias key are present, keep canonical."""
    out, log = validate_then_repair(
        'apply_diff',
        {'path': 'real.py', 'file_path': 'bogus.py', 'search': 'x', 'replace': 'y'},
    )
    assert out['path'] == 'real.py'
    assert 'file_path' not in out or out.get('path') == 'real.py'
    assert ('path', 'param_alias') not in log


def test_param_alias_write_file_body_keys():
    """write_file: file_path→path and file_text→content."""
    out, log = validate_then_repair(
        'write_file',
        {'file_path': 'a.py', 'file_text': 'print(1)\n'},
    )
    assert out == {'path': 'a.py', 'content': 'print(1)\n'}
    assert set(log) == {('path', 'param_alias'), ('content', 'param_alias')}


def test_param_alias_then_type_repair_combined():
    """A renamed key is STILL type-checked: read_files paths→reads (string)
    then bare_string_to_array wraps it into the expected array."""
    out, log = validate_then_repair('read_files', {'paths': 'lib/server.py'})
    assert out == {'reads': ['lib/server.py']}
    assert ('reads', 'param_alias') in log
    assert ('reads', 'bare_string_to_array') in log


def test_param_alias_unknown_tool_no_table():
    """A tool with no alias table passes through untouched."""
    args = {'file_path': 'a.py'}
    out, log = validate_then_repair('list_dir', {'path': 'x'})
    assert out == {'path': 'x'} and log == []
    # grep_search has a table but only renames into declared keys
    out2, log2 = validate_then_repair('grep_search', {'regex': 'foo'})
    assert out2 == {'pattern': 'foo'}
    assert ('pattern', 'param_alias') in log2


# ═════════════════════════════════════════════════════
#  Structural transforms (cross-harness whole-payload reshape)
# ═════════════════════════════════════════════════════


def test_multiedit_reshaped_to_apply_diffs():
    """Claude Code MultiEdit: top-level file_path pushed into each edit, and
    old_string/new_string renamed to search/replace."""
    out, log = validate_then_repair(
        'apply_diffs',
        {'file_path': 'lib/server.py',
         'edits': [
             {'old_string': 'a = 1', 'new_string': 'a = 2'},
             {'old_string': 'b = 3', 'new_string': 'b = 4', 'replace_all': True},
         ]},
    )
    assert out == {'edits': [
        {'path': 'lib/server.py', 'search': 'a = 1', 'replace': 'a = 2'},
        {'path': 'lib/server.py', 'search': 'b = 3', 'replace': 'b = 4',
         'replace_all': True},
    ]}
    assert ('apply_diffs', 'structural_transform') in log
    assert 'file_path' not in out


def test_native_apply_diffs_not_reshaped():
    """A correct apply_diffs call (no top-level path, items already
    {path,search,replace}) must NOT be touched by the transform."""
    args = {'edits': [{'path': 'a.py', 'search': 'x', 'replace': 'y'}]}
    out, log = validate_then_repair('apply_diffs', args)
    assert out == args
    assert log == []


def test_askuserquestion_reshaped_to_ask_human():
    """Claude Code AskUserQuestion: questions[0] lifted to the top level,
    options preserved and response_type set to 'choice'."""
    out, log = validate_then_repair(
        'ask_human',
        {'questions': [
            {'question': 'Which palette?',
             'options': [{'label': 'Dark'}, 'Light']},
        ]},
    )
    assert out['question'] == 'Which palette?'
    assert out['response_type'] == 'choice'
    assert out['options'] == [{'label': 'Dark'}, {'label': 'Light'}]
    assert ('ask_human', 'structural_transform') in log


def test_askuserquestion_free_text_when_no_options():
    out, log = validate_then_repair(
        'ask_human',
        {'questions': [{'question': 'What is your goal?'}]},
    )
    assert out == {'question': 'What is your goal?', 'response_type': 'free_text'}
    assert ('ask_human', 'structural_transform') in log


def test_native_ask_human_not_reshaped():
    """A correct ask_human call (top-level question present) is untouched."""
    args = {'question': 'Q?', 'response_type': 'free_text'}
    out, log = validate_then_repair('ask_human', args)
    assert out == args
    assert log == []


# ═════════════════════════════════════════════════════
#  Schema-guided object-array salvage (last-resort rung)
# ═════════════════════════════════════════════════════
#
# Fires ONLY after repair_json (trailing-comma/balance/delimiter/bracket-match)
# has failed to parse a stringified array. Anchors on the schema's declared
# item keys+types instead of the broken punctuation. Gated OFF for the
# free-text destructive editors (apply_diffs / insert_contents).


def test_salvage_recovers_when_repair_json_gives_up():
    """Unescaped inner quote makes repair_json bail; keys are quoted so the
    schema-guided salvage reconstructs both records by anchoring on
    path/start_line."""
    import json
    from lib.utils import repair_json
    payload = '[{"path": "a"b.py", "start_line": 10}, {"path": "c.py", "start_line": 99}]'
    # Precondition: repair_json genuinely cannot parse it.
    with pytest.raises(json.JSONDecodeError):
        repair_json(payload)
    out, log = validate_then_repair('read_files', {'reads': payload})
    assert log == [('reads', 'schema_array_salvage')], log
    assert out['reads'][-1] == {'path': 'c.py', 'start_line': 99}
    assert out['reads'][0]['start_line'] == 10


def test_salvage_embedded_delimiter_in_value_not_missplit():
    """A value containing ':' / ',' must not be mis-split into a new record.
    Force the salvage path by making repair_json fail (stray inner quote)."""
    from lib.tool_input_repair import _salvage_object_array, _array_item_schema
    types, req = _array_item_schema('fetch_url', 'urls')
    got = _salvage_object_array('"url": "https://x.com/a?b=1,c=2:d"', types, req)
    assert got == [{'url': 'https://x.com/a?b=1,c=2:d'}]


def test_salvage_gated_off_for_apply_diffs():
    """Destructive free-text editor: salvage must REFUSE (search/replace can
    contain arbitrary quotes/braces → mis-split would corrupt a code edit).
    The malformed string is left untouched for an honest model retry."""
    import json
    from lib.tool_input_repair import _try_schema_array_salvage
    from lib.utils import repair_json
    # Unescaped inner quote → repair_json genuinely CANNOT parse it, so the
    # only thing that could recover it is salvage — which must refuse here.
    bad = '[{"path": "a"x.py", "search": "foo", "replace": "bar"}]'
    with pytest.raises(json.JSONDecodeError):
        repair_json(bad)
    assert _try_schema_array_salvage('apply_diffs', 'edits', bad) is None
    out, log = validate_then_repair('apply_diffs', {'edits': bad})
    assert out == {'edits': bad}
    assert log == []


def test_salvage_gated_off_for_insert_contents():
    from lib.tool_input_repair import _try_schema_array_salvage
    bad = '[{"path": "a.py", "anchor": "x", "content": "line1\\nline2", "description": "d"'
    assert _try_schema_array_salvage('insert_contents', 'edits', bad) is None


def test_salvage_requires_all_required_item_keys():
    """A record missing a required item key (path) is dropped — an untrusted
    split must not manufacture a pathless read."""
    from lib.tool_input_repair import _salvage_object_array, _array_item_schema
    types, req = _array_item_schema('read_files', 'reads')
    # Only start_line present, no path → dropped → whole salvage returns None.
    assert _salvage_object_array('"start_line": 5', types, req) is None


def test_salvage_not_triggered_when_repair_json_succeeds():
    """The rung is last-resort: a recoverable stringified array is handled by
    stringified_json, NOT schema_array_salvage."""
    out, log = validate_then_repair(
        'read_files',
        {'reads': '[{"path": "a.py", "start_line": 1, "end_line": 10}, {"path": "b.py"}'},
    )
    assert log == [('reads', 'stringified_json')]


def test_salvage_no_keys_leaves_string_untouched():
    """No quoted key tokens at all → salvage returns None (nothing to anchor)."""
    from lib.tool_input_repair import _try_schema_array_salvage
    assert _try_schema_array_salvage('read_files', 'reads', 'garbage no keys here') is None


def test_salvage_valid_input_never_reaches_rung():
    """A well-formed list is a no-op — salvage only runs on a leftover string."""
    args = {'reads': [{'path': 'a.py', 'start_line': 1}]}
    out, log = validate_then_repair('read_files', args)
    assert log == []
    assert out == args


# ═════════════════════════════════════════════════════
#  todo_write nested-envelope transform (conv mtdqz4bkuyitzj)
# ═════════════════════════════════════════════════════

def _todo_contract_docs():
    from lib.tools.contracts import adapt_legacy_tool_contract
    from lib.tools.todo import TODO_WRITE_TOOL
    return {'todo_write': adapt_legacy_tool_contract(
        TODO_WRITE_TOOL).search_document()}


def _ingest_todo(args_dict):
    import json as _json
    from lib.tool_input_repair import ingest_tool_call
    return ingest_tool_call(
        {'id': 'tc1', 'function': {
            'name': 'todo_write', 'arguments': _json.dumps(args_dict)}},
        emit_audit=False,
        contract_documents_by_name=_todo_contract_docs())


def test_todo_write_nested_envelope_unwrapped():
    """kimi-k3 pushed the whole envelope one level down into todos[0] and
    padded it with a junk id=\"\" that half-satisfied the item schema, then
    died on minLength at $.todos[0].id. The intent is unambiguous — unwrap
    deterministically: todos becomes the inner list, envelope keys hoist."""
    nested = {'todos': [{
        'todos': [
            {'id': 'a', 'content': 'one', 'status': 'pending'},
            {'id': 'b', 'content': 'two', 'status': 'in_progress'},
        ],
        'operation': 'sync',
        'reason': 'initial plan',
        'id': '',
    }]}
    out, log = validate_then_repair('todo_write', nested)
    assert out == {
        'todos': [
            {'id': 'a', 'content': 'one', 'status': 'pending'},
            {'id': 'b', 'content': 'two', 'status': 'in_progress'},
        ],
        'operation': 'sync',
        'reason': 'initial plan',
    }
    assert ('todo_write', 'structural_transform') in log


def test_todo_write_nested_envelope_survives_contract_validation():
    """The unwrapped call must pass the authoritative contract — the
    incident shape previously died fail-closed at $.todos[0].id."""
    result = _ingest_todo({'todos': [{
        'todos': [{'id': 'a', 'content': 'one', 'status': 'pending'}],
        'operation': 'sync', 'reason': 'r', 'id': ''}]})
    assert result.parse_error is None
    assert result.fn_args['todos'] == [
        {'id': 'a', 'content': 'one', 'status': 'pending'}]
    assert result.fn_args['operation'] == 'sync'
    assert result.fn_args['reason'] == 'r'


def test_todo_write_correct_calls_never_unwrapped():
    """A genuine single item has `content` and no nested todos list — the
    transform must stay a strict no-op on well-formed input."""
    good = {'operation': 'sync',
            'todos': [{'id': 'a', 'content': 'one', 'status': 'pending'}]}
    out, log = validate_then_repair('todo_write', good)
    assert log == []
    assert out == good


def test_todo_write_multi_element_outer_list_not_unwrapped():
    """Two+ elements means the outer array is real data, not a wrapper —
    leave it to contract validation, never guess."""
    args = {'todos': [
        {'todos': [], 'id': ''},
        {'id': 'a', 'content': 'x', 'status': 'pending'}]}
    out, log = validate_then_repair('todo_write', args)
    assert out['todos'][0] == {'todos': [], 'id': ''}
    assert ('todo_write', 'structural_transform') not in log


def test_todo_write_contract_rejection_carries_schema_hint():
    """The recovery round must see the expected shape, not a bare path —
    the observed retry loop misdiagnosed the failure without it."""
    result = _ingest_todo(
        {'todos': [{'id': 'x' * 65, 'content': 'c', 'status': 'pending'}]})
    assert result.parse_error is not None
    assert '[invalid_argument_length]' in result.parse_error
    assert 'expects a JSON object' in result.parse_error
    assert 'todos' in result.parse_error


# ═════════════════════════════════════════════════════
#  Tool-NAME repair (resolve_tool_name)
# ═════════════════════════════════════════════════════

_KNOWN = {
    'read_files', 'grep_search', 'list_dir', 'find_files', 'write_file',
    'apply_diff', 'apply_diffs', 'insert_content', 'insert_contents',
    'run_command', 'web_search', 'fetch_url',
    'browser_download_url_to_server',
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
        'download_url_to_server': 'browser_download_url_to_server',
    }
    for wrong, canonical in cases.items():
        name, kind = resolve_tool_name(wrong, known=_KNOWN)
        assert name == canonical, f'{wrong!r} -> {name!r}, expected {canonical!r}'
        assert kind == 'alias'


def test_tool_result_reader_alias_never_shadows_real_artifact_tool():
    assert resolve_tool_name(
        'read_artifact', known={'read_tool_artifact'},
    ) == ('read_tool_artifact', 'alias')
    assert resolve_tool_name(
        'read_artifact', known={'read_artifact', 'read_tool_artifact'},
    ) == ('read_artifact', None)


def test_resolve_casefold_match():
    """Claude-Code CamelCase / stray capitalisation resolves case-insensitively."""
    assert resolve_tool_name('Grep_Search', known=_KNOWN) == ('grep_search', 'casefold')
    assert resolve_tool_name('READ_FILES', known=_KNOWN) == ('read_files', 'casefold')


def test_resolve_camelcase_via_alias():
    """'Read'/'Grep' hit the lowercase static alias before casefold."""
    assert resolve_tool_name('Read', known=_KNOWN) == ('read_files', 'alias')
    assert resolve_tool_name('Grep', known=_KNOWN) == ('grep_search', 'alias')


def test_resolve_claude_code_native_names():
    """Claude Code's native tool names that DON'T match ours case-foldingly
    must alias to the canonical Tofu tool (lets the model use the names it is
    most fluent in). Matched case-insensitively, so the CamelCase native form
    resolves via the lowercase alias entry."""
    known = _KNOWN | {'ask_human'}
    cases = {
        'AskUserQuestion': 'ask_human',
        'MultiEdit': 'apply_diffs',
        'WebFetch': 'fetch_url',
    }
    for wrong, canonical in cases.items():
        name, kind = resolve_tool_name(wrong, known=known)
        assert name == canonical, f'{wrong!r} -> {name!r}, expected {canonical!r}'
        assert kind == 'alias'


def test_resolve_ask_human_only_when_registered():
    """The ask_human aliases must NOT be invented when human-guidance is off
    (ask_human absent from the session tool set)."""
    assert resolve_tool_name('AskUserQuestion', known=_KNOWN) == (
        'AskUserQuestion', None)


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


# ══════════════════════════════════════════
#  Repair-index coverage drift pin (conv mtdqz4bkuyitzj bug class)
# ══════════════════════════════════════════

_COVERAGE_SCAN_PACKAGES = (
    'lib.tools', 'lib.skills', 'lib.swarm', 'lib.memory',
    'lib.scheduler', 'lib.knowledge', 'lib.mcp', 'lib.paper',
)
# Dynamic per-server schemas with their own coercion (lib/mcp/client/_coerce.py);
# per-vertical paper shims that would clobber the canonical web_search/fetch_url
# index entries.
_COVERAGE_EXEMPT_PREFIXES = ('lib.mcp',)
_COVERAGE_EXEMPT_MODULES = {'lib.paper.tools'}


def _walk_module_schema_names(mod) -> set:
    """Mirror ``_build_schema_index``'s walk: literal dicts, lists, and
    zero-arg ``build_*`` factories exposing ``{'type': 'function'}``."""
    names = set()
    for attr in dir(mod):
        obj = getattr(mod, attr, None)
        cands = []
        if isinstance(obj, list):
            cands = [e for e in obj if isinstance(e, dict)]
        elif isinstance(obj, dict):
            cands = [obj]
        elif callable(obj) and attr.startswith('build_'):
            try:
                built = obj()
            except Exception:
                continue
            if isinstance(built, list):
                cands = [e for e in built if isinstance(e, dict)]
            elif isinstance(built, dict):
                cands = [built]
        for e in cands:
            if e.get('type') == 'function':
                n = (e.get('function') or {}).get('name')
                if n:
                    names.add(n)
    return names


def test_all_static_schema_modules_are_indexed():
    """Every module exposing static built-in tool schemas must be registered
    in ``_schema_owner_modules()`` — otherwise its tools are fail-closed
    contract-validated with ZERO repair, the exact gap that left todo_write
    unprotected for months. If this fails, register the module in
    ``lib/tool_input_repair/_schema.py`` (or exempt it here with a reason)."""
    import importlib
    import pkgutil

    from lib.tool_input_repair._schema import _schemas
    indexed = set(_schemas())
    uncovered = {}
    for pkg_name in _COVERAGE_SCAN_PACKAGES:
        pkg = importlib.import_module(pkg_name)
        mod_names = [pkg_name]
        if hasattr(pkg, '__path__'):
            mod_names += [f'{pkg_name}.{i.name}'
                          for i in pkgutil.iter_modules(pkg.__path__)]
        for mod_name in mod_names:
            if mod_name in _COVERAGE_EXEMPT_MODULES or any(
                    mod_name.startswith(p) for p in _COVERAGE_EXEMPT_PREFIXES):
                continue
            try:
                mod = importlib.import_module(mod_name)
            except Exception:
                continue
            missing = _walk_module_schema_names(mod) - indexed
            if missing:
                uncovered[mod_name] = sorted(missing)
    assert not uncovered, (
        'schema-owning modules missing from the repair index: '
        f'{uncovered}')


def test_newly_indexed_modules_get_type_repair():
    """Tools from the six modules registered after the audit receive the
    same stringified_json repair as the original set."""
    repaired, log = validate_then_repair(
        'create_memory', {'description': 'd', 'name': 'n', 'body': 'b',
                          'tags': '["x", "y"]'})
    assert repaired['tags'] == ['x', 'y']
    assert ('tags', 'stringified_json') in log

    repaired, log = validate_then_repair(
        'spawn_agents', {'agents': '[{"objective": "o"}]'})
    assert repaired['agents'] == [{'objective': 'o'}]
    assert ('agents', 'stringified_json') in log


def test_structural_transform_runs_without_index_entry(monkeypatch):
    """Name-keyed transforms must fire even when the tool has no indexed
    schema — index coverage must not gate them (the todo_write transform was
    dead code until lib.tools.todo happened to be registered)."""
    from lib.tool_input_repair import _transform as _tf

    def _dummy(args):
        return {**args, 'unwrapped': True}, True

    monkeypatch.setitem(_tf._STRUCTURAL_TRANSFORMS,
                        'never_indexed_tool_xyz', _dummy)
    repaired, log = validate_then_repair(
        'never_indexed_tool_xyz', {'a': 1})
    assert repaired == {'a': 1, 'unwrapped': True}
    assert ('never_indexed_tool_xyz', 'structural_transform') in log


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
