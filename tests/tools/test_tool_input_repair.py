"""Unit tests for lib.tool_input_repair.

Covers the five repair patterns (Awais open-model-harness + stringified
primitive), the load-bearing ordering of stringified_json before
bare_string_to_array, and the no-op guarantee on already-valid inputs.
"""
from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/server.py', 'lib/swarm/integration.py', 'tests/foo.py', 'lib/a.py',
}

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.tool_input_repair import resolve_tool_name, validate_then_repair  # noqa: E402
from lib.tool_input_repair._schema import _schemas  # noqa: E402
from lib.tools.contracts import (  # noqa: E402
    ToolContractError,
    adapt_legacy_tool_contract,
    clamp_arguments_to_schema,
    clamp_tool_arguments_from_documents,
)


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


def test_ingest_clamps_mcp_numeric_bounds_before_contract_rejection():
    """End-to-end pin for the user-reported failure shape: a server-dynamic
    (MCP) tool whose max_chars sits below the declared minimum must be
    clamped by Stage 4.5, not rejected by contract validation."""
    import json

    from lib.tool_input_repair import ingest_tool_call

    contracts = {
        'mcp__xuecheng__read_doc': _v2_contract_document({
            'doc': {'type': 'string'},
            'max_chars': {'type': 'integer', 'minimum': 1000,
                          'maximum': 500000, 'default': 60000},
        }),
    }
    below = ingest_tool_call(
        {'id': 'call-below', 'function': {
            'name': 'mcp__xuecheng__read_doc',
            'arguments': json.dumps({'doc': '123', 'max_chars': 10}),
        }},
        known_tools={'mcp__xuecheng__read_doc'},
        contract_documents_by_name=contracts,
        emit_audit=False,
    )
    above = ingest_tool_call(
        {'id': 'call-above', 'function': {
            'name': 'mcp__xuecheng__read_doc',
            'arguments': json.dumps({'doc': '123', 'max_chars': 999999}),
        }},
        known_tools={'mcp__xuecheng__read_doc'},
        contract_documents_by_name=contracts,
        emit_audit=False,
    )

    assert below.parse_error is None
    assert below.contract_error is None
    assert below.fn_args == {'doc': '123', 'max_chars': 1000}
    assert ('$.max_chars', 'clamp_minimum') in below.repair_log
    assert above.parse_error is None
    assert above.contract_error is None
    assert above.fn_args == {'doc': '123', 'max_chars': 500000}
    assert ('$.max_chars', 'clamp_maximum') in above.repair_log


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
                   'replace': 'window = sse_src[pos:pos + 900]',
                   'description': 'Edit tests/foo.py'}
    assert set(log) == {('path', 'param_alias'), ('search', 'param_alias'),
                        ('replace', 'param_alias'),
                        ('apply_diff', 'structural_transform')}


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
    """write_file: file_path→path and file_text→content. The foreign keys
    also mark the payload as foreign-schema-shaped, so the presentational
    caption Claude Code's Write never carries is derived from the path."""
    out, log = validate_then_repair(
        'write_file',
        {'file_path': 'a.py', 'file_text': 'print(1)\n'},
    )
    assert out == {'path': 'a.py', 'content': 'print(1)\n',
                   'description': 'Write a.py'}
    assert set(log) == {('path', 'param_alias'), ('content', 'param_alias'),
                        ('write_file', 'structural_transform')}


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
        {'path': 'lib/server.py', 'search': 'a = 1', 'replace': 'a = 2',
         'description': 'Edit lib/server.py'},
        {'path': 'lib/server.py', 'search': 'b = 3', 'replace': 'b = 4',
         'replace_all': True, 'description': 'Edit lib/server.py'},
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
        {'todos': [{'content': 'x' * 513, 'status': 'pending'}]})
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


# ═════════════════════════════════════════════════════
#  Value-bounds repair (clamp out-of-range numbers / truncate display fields)
# ═════════════════════════════════════════════════════

def _v2_contract_document(properties):
    return {
        'contractVersion': 'tofu.tool-contract/v2',
        'arguments_schema': {'type': 'object', 'properties': properties},
    }


def test_clamp_native_top_level_above_max():
    repaired, log = validate_then_repair(
        'read_tool_artifact', {'artifact_ref': 'a', 'limit': 999999})
    assert repaired['limit'] == 65536
    assert ('$.limit', 'clamp_maximum') in log


def test_clamp_native_nested_below_min():
    repaired, log = validate_then_repair(
        'read_tool_artifact',
        {'reads': [{'artifact_ref': 'a', 'limit': 0}]})
    assert repaired['reads'][0]['limit'] == 1
    assert ('$.reads[0].limit', 'clamp_minimum') in log


def test_clamp_in_range_is_noop():
    repaired, log = validate_then_repair(
        'read_tool_artifact', {'artifact_ref': 'a', 'limit': 8192})
    assert repaired['limit'] == 8192
    assert log == []


def test_truncate_display_caption_edit_file_description():
    repaired, log = validate_then_repair(
        'edit_file',
        {'path': 'a.py', 'description': 'x' * 134, 'edits': []})
    assert len(repaired['description']) == 120
    assert ('$.description', 'truncate_max_length') in log


def test_identity_strings_are_never_truncated():
    """A todo id is addressable identity — truncating it would silently
    break later sync/push references, so it must stay over-long and let
    contract validation reject with a precise message."""
    repaired, log = validate_then_repair(
        'todo_write',
        {'todos': [{'id': 'x' * 65, 'content': 'c', 'status': 'pending'}]})
    assert len(repaired['todos'][0]['id']) == 65
    assert not any(pattern == 'truncate_max_length' for _, pattern in log)


def test_clamp_arguments_bool_passthrough():
    out, log = clamp_arguments_to_schema(
        {'n': True},
        {'type': 'object',
         'properties': {'n': {'type': 'integer', 'minimum': 1, 'maximum': 5}}})
    assert out['n'] is True
    assert log == []


def test_identity_cursor_params_are_never_clamped():
    """get_conversation.before is a navigation cursor: below-minimum is a
    logic error that must stay rejected, not a size to silently move (its
    before=0 sibling is repaired by the dedicated omission pass)."""
    repaired, log = validate_then_repair(
        'get_conversation', {'conversation_id': 'c', 'before': -1})
    assert repaired['before'] == -1
    assert not any(pattern.startswith('clamp') for _, pattern in log)


def test_clamp_noop_preserves_object_identity():
    args = {'reads': [{'path': 'lib/server.py'}]}
    out, log = clamp_arguments_to_schema(args, _schemas()['read_files'])
    assert log == []
    assert out is args
    assert out['reads'] is args['reads']


def test_document_clamp_covers_mcp_style_tools():
    """The measured failure class: mcp__xuecheng__read_doc max_chars below
    the declared minimum — absent from the static repair index, clamped from
    the request-owned contract document instead of rejected."""
    docs = {'mcp__xuecheng__read_doc': _v2_contract_document({
        'max_chars': {'type': 'integer', 'minimum': 1000, 'maximum': 500000}})}
    out, log = clamp_tool_arguments_from_documents(
        docs, 'mcp__xuecheng__read_doc', {'max_chars': 50})
    assert out['max_chars'] == 1000
    assert ('$.max_chars', 'clamp_minimum') in log
    out, log = clamp_tool_arguments_from_documents(
        docs, 'mcp__xuecheng__read_doc', {'max_chars': 600000})
    assert out['max_chars'] == 500000
    assert ('$.max_chars', 'clamp_maximum') in log


def test_document_clamp_passthrough_without_authority():
    docs = {'t': _v2_contract_document(
        {'n': {'type': 'integer', 'minimum': 1}})}
    assert clamp_tool_arguments_from_documents(
        None, 't', {'n': 0}) == ({'n': 0}, [])
    assert clamp_tool_arguments_from_documents(
        {}, 't', {'n': 0}) == ({'n': 0}, [])
    assert clamp_tool_arguments_from_documents(
        {'t': {'contractVersion': 'other'}}, 't', {'n': 0}) == ({'n': 0}, [])
    out, log = clamp_tool_arguments_from_documents(docs, 't', {'n': 5})
    assert out == {'n': 5}
    assert log == []


def test_min_max_contract_errors_carry_bounds():
    """A rejected bound must name the got/allowed values — the bare
    'Value below minimum' left the model guessing on retry."""
    contract = adapt_legacy_tool_contract({'function': {
        'name': 't', 'description': 'd',
        'parameters': {'type': 'object',
                       'properties': {'n': {'type': 'integer',
                                            'minimum': 1000,
                                            'maximum': 500000}}}}})
    with pytest.raises(ToolContractError) as below:
        contract.validate_arguments({'n': 50})
    assert below.value.code == 'invalid_argument_value'
    assert 'got 50' in str(below.value)
    assert '>= 1000' in str(below.value)
    with pytest.raises(ToolContractError) as above:
        contract.validate_arguments({'n': 600000})
    assert 'got 600000' in str(above.value)
    assert '<= 500000' in str(above.value)


def test_high_traffic_descriptions_carry_bounds():
    assert '120' in _schemas()[
        'edit_file']['properties']['description']['description']
    todo_props = _schemas()[
        'todo_write']['properties']['todos']['items']['properties']
    assert 'id' not in todo_props
    assert todo_props['content']['maxLength'] == 512


# ═════════════════════════════════════════════════════
#  Amazon Q todo_list → todo_write (name alias + command-shape transform)
# ═════════════════════════════════════════════════════

def test_todo_list_name_aliases_to_todo_write():
    assert resolve_tool_name('todo_list', known={'todo_write'}) == (
        'todo_write', 'alias')
    # Case-insensitive like every other alias.
    assert resolve_tool_name('TODO_LIST', known={'todo_write'}) == (
        'todo_write', 'alias')
    # An exact live name always wins over the alias table.
    assert resolve_tool_name(
        'todo_list', known={'todo_write', 'todo_list'}) == ('todo_list', None)


def test_todo_list_command_shape_injects_todos_placeholder():
    repaired, log = validate_then_repair('todo_write', {
        'command': 'create', 'tasks': ['scan', 'patch'],
        'todo_list_description': 'fix bug'})
    assert repaired['todos'] == []
    assert repaired['command'] == 'create'
    assert repaired['tasks'] == ['scan', 'patch']
    assert ('todo_write', 'structural_transform') in log


def test_todo_list_transform_never_touches_native_or_unknown_payloads():
    native = {'todos': [{'id': '1', 'content': 'x', 'status': 'pending'}]}
    repaired, log = validate_then_repair('todo_write', dict(native))
    assert repaired == native and log == []

    unknown = {'command': 'restart'}
    repaired, log = validate_then_repair('todo_write', dict(unknown))
    assert 'todos' not in repaired and log == []

    both = {'command': 'complete',
            'todos': [{'id': '1', 'content': 'x', 'status': 'completed'}]}
    repaired, log = validate_then_repair('todo_write', dict(both))
    assert repaired == both and log == []


def test_todo_write_transform_chain_still_unwraps_nested_envelope():
    payload = {'todos': [{
        'todos': [{'id': '1', 'content': 'real', 'status': 'pending'}],
        'operation': 'sync', 'id': ''}]}
    repaired, log = validate_then_repair('todo_write', payload)
    assert repaired['todos'] == [
        {'id': '1', 'content': 'real', 'status': 'pending'}]
    assert ('todo_write', 'structural_transform') in log


# ═════════════════════════════════════════════════════
#  Kimi CLI SetTodoList → todo_write (name alias + item-vocabulary transform)
# ═════════════════════════════════════════════════════

def test_settodolist_name_aliases_to_todo_write():
    # Kimi CLI's built-in checklist tool (MoonshotAI/kimi-cli
    # src/kimi_cli/tools/todo/__init__.py): name SetTodoList, items
    # {title, status: pending|in_progress|done}, no ids.
    assert resolve_tool_name('SetTodoList', known={'todo_write'}) == (
        'todo_write', 'alias')
    assert resolve_tool_name('settodolist', known={'todo_write'}) == (
        'todo_write', 'alias')
    # An exact live name always wins over the alias table.
    assert resolve_tool_name(
        'SetTodoList', known={'todo_write', 'SetTodoList'}) == (
            'SetTodoList', None)


def test_kimi_item_vocabulary_mapped_to_native_shape():
    repaired, log = validate_then_repair('todo_write', {'todos': [
        {'title': 'scan the repo', 'status': 'done'},
        {'title': 'patch the bug', 'status': 'in_progress'},
        {'title': 'run tests', 'status': 'pending'},
    ]})
    assert repaired['todos'] == [
        {'content': 'scan the repo', 'status': 'completed'},
        {'content': 'patch the bug', 'status': 'in_progress'},
        {'content': 'run tests', 'status': 'pending'},
    ]
    assert ('todo_write', 'structural_transform') in log


def test_kimi_item_vocabulary_survives_contract_validation():
    """The adapted payload must pass the authoritative contract — unadapted
    it died fail-closed: title dropped on the floor, missing item id."""
    result = _ingest_todo({'todos': [
        {'title': 'scan the repo', 'status': 'done'},
        {'title': 'patch the bug', 'status': 'in_progress'},
    ]})
    assert result.parse_error is None
    assert result.fn_args['todos'] == [
        {'content': 'scan the repo', 'status': 'completed'},
        {'content': 'patch the bug', 'status': 'in_progress'},
    ]


def test_kimi_transform_never_touches_native_or_ambiguous_payloads():
    # Fully native list — strict no-op.
    native = {'todos': [{'id': 'a', 'content': 'one', 'status': 'pending'}]}
    repaired, log = validate_then_repair('todo_write', dict(native))
    assert repaired == native and log == []

    # Empty list is meaningful natively (and is Kimi's clear mode) — leave
    # it to the state machine, never guess.
    repaired, log = validate_then_repair('todo_write', {'todos': []})
    assert repaired == {'todos': []} and log == []

    # Query mode (no todos key) stays an honest contract error.
    result = _ingest_todo({})
    assert result.parse_error is not None

    # A real content key is never clobbered by a stray title.
    both = {'todos': [{'id': 'a', 'content': 'real', 'title': 'shadow',
                       'status': 'pending'}]}
    repaired, log = validate_then_repair('todo_write', dict(both))
    assert repaired['todos'][0]['content'] == 'real'
    assert ('todo_write', 'structural_transform') not in log


def test_kimi_done_status_maps_even_in_blended_native_call():
    """status 'done' is never valid natively; silent coercion to pending
    downstream would erase the completion. Map it even when the item
    otherwise looks native (right name, wrong status vocabulary)."""
    repaired, log = validate_then_repair('todo_write', {'todos': [
        {'id': 'a', 'content': 'one', 'status': 'done'},
        {'id': 'b', 'content': 'two', 'status': 'pending'},
    ]})
    assert repaired['todos'][0]['status'] == 'completed'
    assert repaired['todos'][1] == {'id': 'b', 'content': 'two',
                                    'status': 'pending'}
    assert ('todo_write', 'structural_transform') in log


def test_kimi_settodolist_payload_runs_through_state_machine():
    """Full chain: repair adapts the Kimi payload, then the canonical
    state machine applies it as an ordinary sync — statuses survive."""
    from lib.tools.todo import apply_todo_operation
    repaired, _ = validate_then_repair('todo_write', {'todos': [
        {'title': 'scan the repo', 'status': 'done'},
        {'title': 'patch the bug', 'status': 'in_progress'},
        {'title': 'run tests', 'status': 'pending'},
    ]})
    outcome = apply_todo_operation(None, repaired)
    assert not outcome['rejected']
    assert outcome['todos'] == [
        {'content': 'scan the repo', 'status': 'completed'},
        {'content': 'patch the bug', 'status': 'in_progress'},
        {'content': 'run tests', 'status': 'pending'},
    ]

    # A second full replacement needs no positional identity alignment.
    repaired2, _ = validate_then_repair('todo_write', {'todos': [
        {'title': 'scan the repo', 'status': 'done'},
        {'title': 'patch the bug', 'status': 'done'},
        {'title': 'run tests', 'status': 'in_progress'},
    ]})
    outcome2 = apply_todo_operation(outcome['state'], repaired2)
    assert not outcome2['rejected']
    assert [t['status'] for t in outcome2['todos']] == [
        'completed', 'completed', 'in_progress']


# ══════════════════════════════════════════════
#  Claude Code / OpenCode / Gemini / Codex checklist tools → todo_write
# ══════════════════════════════════════════════

def test_harness_checklist_names_alias_to_todo_write():
    # Claude Code ``TodoWrite`` and OpenCode ``todowrite`` casefold to the
    # same alias key; Gemini CLI ``write_todos``; Codex CLI ``update_plan``.
    for emitted in ('TodoWrite', 'todowrite', 'TODOWRITE'):
        assert resolve_tool_name(emitted, known={'todo_write'}) == (
            'todo_write', 'alias')
    assert resolve_tool_name('write_todos', known={'todo_write'}) == (
        'todo_write', 'alias')
    assert resolve_tool_name('update_plan', known={'todo_write'}) == (
        'todo_write', 'alias')
    # An exact live name always wins over the alias table.
    assert resolve_tool_name(
        'write_todos', known={'todo_write', 'write_todos'}) == (
            'write_todos', None)


def test_gemini_run_shell_command_and_friends_alias():
    # Gemini CLI: run_shell_command {command, is_background?} → run_command;
    # google_web_search {query} → web_search; read_many_files {paths}
    # → read_files. Codex CLI: view_image {path} → inspect_image.
    assert resolve_tool_name('run_shell_command', known={'run_command'}) == (
        'run_command', 'alias')
    assert resolve_tool_name('google_web_search', known={'web_search'}) == (
        'web_search', 'alias')
    assert resolve_tool_name('read_many_files', known={'read_files'}) == (
        'read_files', 'alias')
    assert resolve_tool_name('view_image', known={'inspect_image'}) == (
        'inspect_image', 'alias')


def test_codex_exec_command_and_amazon_q_execute_bash_alias():
    # Codex CLI: exec_command {cmd, workdir?} → run_command. Amazon Q
    # Developer CLI: execute_bash {command, summary?} → run_command.
    assert resolve_tool_name('exec_command', known={'run_command'}) == (
        'run_command', 'alias')
    assert resolve_tool_name('execute_bash', known={'run_command'}) == (
        'run_command', 'alias')


def test_run_command_workdir_and_cwd_param_aliases():
    # Codex's exec_command uses ``workdir``; ``cwd`` is the other common
    # wrong-harness key for the same concept.
    out, log = validate_then_repair(
        'run_command', {'cmd': 'ls', 'workdir': '/tmp/x'})
    assert out['command'] == 'ls' and out['working_dir'] == '/tmp/x'
    assert ('command', 'param_alias') in log
    assert ('working_dir', 'param_alias') in log
    out, log = validate_then_repair(
        'run_command', {'command': 'ls', 'cwd': '/tmp/y'})
    assert out['working_dir'] == '/tmp/y'
    # A real working_dir is never clobbered.
    out, log = validate_then_repair(
        'run_command', {'command': 'ls', 'workdir': '/a',
                        'working_dir': '/b'})
    assert out['working_dir'] == '/b' and out['workdir'] == '/a'
    assert ('working_dir', 'param_alias') not in log


def test_update_plan_envelope_reshaped_to_todos():
    repaired, log = validate_then_repair('todo_write', {
        'explanation': 'shipping the fix',
        'plan': [{'step': 'scan the repo', 'status': 'completed'},
                 {'step': 'patch the bug', 'status': 'in_progress'}]})
    assert 'plan' not in repaired
    assert repaired['todos'] == [
        {'content': 'scan the repo', 'status': 'completed'},
        {'content': 'patch the bug', 'status': 'in_progress'},
    ]
    # Explanation is now a native field, preserved across the alias.
    assert repaired['explanation'] == 'shipping the fix'
    assert ('todo_write', 'structural_transform') in log


def test_update_plan_envelope_survives_contract_validation():
    """The reshaped Codex payload must pass the authoritative contract —
    unadapted it died fail-closed on the missing required ``todos`` key."""
    result = _ingest_todo({'plan': [
        {'step': 'scan the repo', 'status': 'pending'},
        {'step': 'patch the bug', 'status': 'in_progress'},
    ]})
    assert result.parse_error is None
    assert result.fn_args['todos'] == [
        {'content': 'scan the repo', 'status': 'pending'},
        {'content': 'patch the bug', 'status': 'in_progress'},
    ]


def test_gemini_write_todos_item_vocabulary():
    # description→content; blocked passes through natively without IDs.
    repaired, log = validate_then_repair('todo_write', {'todos': [
        {'description': 'scan the repo', 'status': 'completed'},
        {'description': 'await review', 'status': 'blocked'},
    ]})
    assert repaired['todos'] == [
        {'content': 'scan the repo', 'status': 'completed'},
        {'content': 'await review', 'status': 'blocked'},
    ]
    assert ('todo_write', 'structural_transform') in log


def test_cancelled_items_drop_from_foreign_list():
    """OpenCode/Gemini ``cancelled`` = leaves the tracked list. Dropping
    matches our full-list sync semantics; mapping to completed would lie."""
    repaired, log = validate_then_repair('todo_write', {'todos': [
        {'content': 'keep me', 'status': 'in_progress', 'priority': 'high'},
        {'content': 'drop me', 'status': 'cancelled', 'priority': 'low'},
    ]})
    assert repaired['todos'] == [
        {'content': 'keep me', 'status': 'in_progress',
         'priority': 'high'},
    ]
    assert ('todo_write', 'structural_transform') in log


def test_claude_code_todowrite_items_need_no_positional_ids():
    """Claude Code content/status already matches the flat native item schema."""
    repaired, log = validate_then_repair('todo_write', {'todos': [
        {'content': 'scan the repo', 'status': 'completed',
         'activeForm': 'Scanning the repo'},
        {'content': 'patch the bug', 'status': 'in_progress',
         'activeForm': 'Patching the bug'},
    ]})
    assert all('id' not in item for item in repaired['todos'])
    assert repaired['todos'][0]['content'] == 'scan the repo'
    assert ('todo_write', 'structural_transform') in log


def test_harness_item_vocabulary_never_touches_native_or_ambiguous():
    # Fully native list — strict no-op.
    native = {'todos': [{'id': 'a', 'content': 'one', 'status': 'pending'}]}
    repaired, log = validate_then_repair('todo_write', dict(native))
    assert repaired == native and log == []

    # A real content key is never clobbered by a stray step/description.
    both = {'todos': [{'id': 'a', 'content': 'real', 'step': 'shadow',
                       'status': 'pending'}]}
    repaired, _ = validate_then_repair('todo_write', dict(both))
    assert repaired['todos'][0]['content'] == 'real'

    # update_plan envelope never fires when a real todos list is present.
    blend = {'todos': [{'id': 'a', 'content': 'one', 'status': 'pending'}],
             'plan': [{'step': 'shadow', 'status': 'pending'}]}
    repaired, log = validate_then_repair('todo_write', dict(blend))
    assert repaired['todos'] == blend['todos'] and repaired['plan'] == blend['plan']
    assert ('todo_write', 'structural_transform') not in log


def test_update_plan_payload_runs_through_state_machine():
    """Full chain: envelope + item repair, then the canonical state machine
    applies the payload as an ordinary sync — statuses survive."""
    from lib.tools.todo import apply_todo_operation
    repaired, _ = validate_then_repair('todo_write', {'plan': [
        {'step': 'scan the repo', 'status': 'completed'},
        {'step': 'patch the bug', 'status': 'in_progress'},
        {'step': 'run tests', 'status': 'pending'},
    ]})
    outcome = apply_todo_operation(None, repaired)
    assert not outcome['rejected']
    assert outcome['todos'] == [
        {'content': 'scan the repo', 'status': 'completed'},
        {'content': 'patch the bug', 'status': 'in_progress'},
        {'content': 'run tests', 'status': 'pending'},
    ]


# ═════════════════════════════════════════════════════
#  Kimi CLI / OpenCode / Gemini base-tool vocabulary
#  (edit / read / search / fetch / ask / skill)
# ═════════════════════════════════════════════════════


def test_kimi_str_replace_file_name_alias():
    assert resolve_tool_name('StrReplaceFile', known={'apply_diff'}) == (
        'apply_diff', 'alias')
    # In edit_file sessions the convergence set routes it there instead.
    assert resolve_tool_name(
        'StrReplaceFile', known={'edit_file', 'apply_diff'}) == (
        'edit_file', 'alias')


def test_kimi_str_replace_file_envelope_to_apply_diff():
    """Kimi StrReplaceFile {path, edit:{old,new,replace_all}} unwraps 1:1."""
    out, _ = validate_then_repair(
        'apply_diff',
        {'path': 'a.py', 'edit': {'old': 'x = 1', 'new': 'x = 2',
                                  'replace_all': True}},
    )
    assert out['search'] == 'x = 1' and out['replace'] == 'x = 2'
    assert out['replace_all'] is True
    assert 'edit' not in out


def test_kimi_str_replace_file_list_form_passes_through():
    """The list form can't be expressed by single-edit apply_diff — it must
    pass through untouched and fail closed downstream, not be half-mapped."""
    args = {'path': 'a.py', 'edit': [{'old': 'a', 'new': 'b'},
                                     {'old': 'c', 'new': 'd'}]}
    out, _ = validate_then_repair('apply_diff', args)
    assert out['edit'] == args['edit']
    assert 'search' not in out


def test_apply_diff_native_call_with_stray_edit_key_untouched():
    args = {'path': 'a.py', 'search': 'x', 'replace': 'y',
            'edit': {'old': '1'}}
    out, _ = validate_then_repair('apply_diff', args)
    assert out == args


def test_kimi_str_replace_file_envelope_to_edit_file():
    """In edit_file sessions the same envelope normalizes to unified edits
    (list form included)."""
    out, _ = validate_then_repair(
        'edit_file',
        {'path': 'a.py', 'edit': [
            {'old': 'a', 'new': 'b'},
            {'old': 'c', 'new': 'd', 'replace_all': True}]},
    )
    assert out['edits'] == [
        {'path': 'a.py', 'operation': 'replace', 'anchor': 'a',
         'content': 'b'},
        {'path': 'a.py', 'operation': 'replace', 'anchor': 'c',
         'content': 'd', 'replace_all': True},
    ]


def test_opencode_edit_keys_to_edit_file():
    """OpenCode edit {filePath, oldString, newString, replaceAll}."""
    out, _ = validate_then_repair(
        'edit_file',
        {'filePath': 'a.py', 'oldString': 'foo', 'newString': 'bar',
         'replaceAll': True},
    )
    assert out['edits'] == [
        {'path': 'a.py', 'operation': 'replace', 'anchor': 'foo',
         'content': 'bar', 'replace_all': True},
    ]


def test_opencode_edit_keys_to_apply_diff():
    out, log = validate_then_repair(
        'apply_diff',
        {'filePath': 'a.py', 'oldString': 'foo', 'newString': 'bar',
         'replaceAll': True},
    )
    assert out == {'path': 'a.py', 'search': 'foo', 'replace': 'bar',
                   'replace_all': True, 'description': 'Edit a.py'}
    assert set(log) == {('path', 'param_alias'), ('search', 'param_alias'),
                        ('replace', 'param_alias'),
                        ('replace_all', 'param_alias'),
                        ('apply_diff', 'structural_transform')}


def test_opencode_read_range_offset_limit():
    out, _ = validate_then_repair(
        'read_files', {'path': 'a.py', 'offset': 10, 'limit': 20})
    assert out['start_line'] == 10 and out['end_line'] == 29


def test_kimi_read_range_line_offset_n_lines():
    out, _ = validate_then_repair(
        'read_files', {'path': 'a.py', 'line_offset': 5, 'n_lines': 3})
    assert out['start_line'] == 5 and out['end_line'] == 7


def test_read_range_native_keys_win():
    """Native start_line/end_line present → foreign keys never fire."""
    out, _ = validate_then_repair(
        'read_files', {'path': 'a.py', 'start_line': 2, 'end_line': 4,
                       'offset': 100, 'limit': 100})
    assert out['start_line'] == 2 and out['end_line'] == 4


def test_kimi_opencode_gemini_base_tool_name_aliases():
    assert resolve_tool_name('SearchWeb', known={'web_search'}) == (
        'web_search', 'alias')
    assert resolve_tool_name('FetchURL', known={'fetch_url'}) == (
        'fetch_url', 'alias')
    assert resolve_tool_name('web_fetch', known={'fetch_url'}) == (
        'fetch_url', 'alias')
    assert resolve_tool_name('question', known={'ask_human'}) == (
        'ask_human', 'alias')
    assert resolve_tool_name('skill', known={'load_skill'}) == (
        'load_skill', 'alias')


def test_opencode_question_envelope_to_ask_human():
    """OpenCode question speaks the same questions[] envelope as Claude
    Code's AskUserQuestion — the existing ask_human transform unwraps it."""
    out, _ = validate_then_repair(
        'ask_human',
        {'questions': [{'question': 'which option?', 'header': 'Pick'}]},
    )
    assert out['question'] == 'which option?'


def test_kimi_grep_glob_and_head_limit_param_aliases():
    out, log = validate_then_repair(
        'grep_search', {'pattern': 'foo', 'glob': '*.py', 'head_limit': 5})
    assert out['include'] == '*.py' and out['max_results'] == 5
    assert ('include', 'param_alias') in log
    assert ('max_results', 'param_alias') in log


def test_kimi_glob_directory_param_alias():
    out, log = validate_then_repair(
        'find_files', {'pattern': '*.py', 'directory': 'src'})
    assert out['path'] == 'src'
    assert ('path', 'param_alias') in log


def test_opencode_skill_name_param_alias():
    out, log = validate_then_repair('load_skill', {'name': 'flyai'})
    assert out['skill_id'] == 'flyai'
    assert ('skill_id', 'param_alias') in log


# ═════════════════════════════════════════════════════
#  Claude Code / Codex base tools (subagents, shell, grep, captions)
# ═════════════════════════════════════════════════════


def test_task_and_agent_names_alias_to_spawn_agents():
    """Claude Code Task / OpenCode task / Kimi Agent — one shared envelope."""
    for foreign in ('task', 'Task', 'agent', 'Agent'):
        assert resolve_tool_name(foreign, known={'spawn_agents'}) == (
            'spawn_agents', 'alias')


def test_claude_code_task_envelope_to_spawn_agents():
    out, log = validate_then_repair('spawn_agents', {
        'description': 'Explore the auth flow',
        'prompt': 'Trace how login works and report back.',
        'subagent_type': 'Explore'})
    assert out['agents'] == [
        {'objective': 'Trace how login works and report back.'}]
    assert ('spawn_agents', 'structural_transform') in log


def test_spawn_agents_native_call_untouched():
    args = {'agents': [{'objective': 'do x', 'role': 'coder'}]}
    out, log = validate_then_repair('spawn_agents', dict(args))
    assert out == args and log == []


def test_spawn_agents_transform_requires_prompt():
    """No agents list AND no prompt → nothing to build from, fail closed."""
    out, log = validate_then_repair('spawn_agents', {'description': 'x'})
    assert 'agents' not in out and log == []


def test_claude_code_skill_param_alias():
    """Claude Code Skill carries {skill, args?}; args stays inert."""
    out, log = validate_then_repair(
        'load_skill', {'skill': 'flyai', 'args': '--verbose'})
    assert out['skill_id'] == 'flyai'
    assert ('skill_id', 'param_alias') in log


def test_codex_legacy_shell_command_array_joined():
    out, log = validate_then_repair(
        'run_command', {'command': ['ls', '-la', 'src']})
    assert out['command'] == 'ls -la src'
    assert ('run_command', 'structural_transform') in log


def test_codex_exec_command_cmd_array_joined():
    out, _ = validate_then_repair(
        'run_command', {'cmd': ['git', 'status']})
    assert out['command'] == 'git status'


def test_claude_code_bash_timeout_milliseconds_inert():
    """Claude Code's 120000 ms default still gets unit-normalized, but the
    timeout parameter itself is retired — the value is inert and never
    becomes timeout_s: commands run to completion unless killed."""
    out, _ = validate_then_repair(
        'run_command', {'command': 'pytest', 'timeout': 120000})
    assert 'timeout_s' not in out


def test_run_command_legacy_timeout_inert():
    """The old `timeout` spelling is inert: run_command no longer has any
    timeout parameter, so it is never aliased to timeout_s."""
    out, _ = validate_then_repair(
        'run_command', {'command': 'pytest', 'timeout': 300})
    assert 'timeout_s' not in out


def test_run_command_native_timeout_s_untouched():
    args = {'command': 'pytest', 'timeout_s': 300}
    out, log = validate_then_repair('run_command', dict(args))
    assert out == args and log == []


def test_run_command_timeout_s_explicit_seconds_not_ms_converted():
    """timeout_s declares its unit — a large value is explicit seconds."""
    out, _ = validate_then_repair(
        'run_command', {'command': 'pytest', 'timeout_s': 120000})
    assert out['timeout_s'] == 120000


def test_run_command_timeout_s_wins_over_legacy_timeout():
    out, _ = validate_then_repair(
        'run_command', {'command': 'pytest', 'timeout': 300, 'timeout_s': 60})
    assert out['timeout_s'] == 60


def test_grep_output_mode_count_maps_to_count_only():
    for mode in ('count', 'count_matches'):
        out, _ = validate_then_repair(
            'grep_search', {'pattern': 'TODO', 'output_mode': mode})
        assert out['count_only'] is True


def test_grep_output_mode_content_stays_inert():
    args = {'pattern': 'TODO', 'output_mode': 'files_with_matches'}
    out, log = validate_then_repair('grep_search', dict(args))
    assert 'count_only' not in out and log == []


def test_claude_code_write_caption_backfilled():
    out, log = validate_then_repair(
        'write_file', {'file_path': 'lib/a.py', 'content': 'x'})
    assert out['description'] == 'Write lib/a.py'
    assert ('write_file', 'structural_transform') in log


def test_claude_code_edit_caption_backfilled_edit_file():
    out, _ = validate_then_repair(
        'edit_file', {'file_path': 'a.py', 'old_string': 'x',
                      'new_string': 'y'})
    assert out['description'] == 'Edit a.py'


def test_caption_backfill_multi_file_summary():
    out, _ = validate_then_repair('edit_file', {'edits': [
        {'file_path': 'a.py', 'old_string': 'x', 'new_string': 'y'},
        {'file_path': 'b.py', 'old_string': 'u', 'new_string': 'v'}]})
    assert out['description'] == 'Edit 2 files'


def test_caption_backfill_never_fires_for_native_spelling():
    """A native-schema payload missing description fails closed naming the
    key — the backfill only fires on foreign key spellings, so the
    native-payloads-untouched invariant holds even for broken native calls."""
    out, log = validate_then_repair(
        'write_file', {'path': 'a.py', 'content': 'x'})
    assert 'description' not in out
    assert ('write_file', 'structural_transform') not in log
    out, log = validate_then_repair(
        'edit_file', {'edits': [{'path': 'a.py', 'operation': 'replace',
                                 'anchor': 'x', 'content': 'y'}]})
    assert 'description' not in out
    assert ('edit_file', 'structural_transform') not in log


def test_edit_file_allow_outside_workspace_survives_native_call():
    """conv mtq3ev44samfhr: the edit_file reshape rebuilt the envelope as
    {edits, description?} on EVERY call — even a fully native one — and
    silently dropped allow_outside_workspace. The write gate then refused
    in a loop, its own error text telling the model to re-issue the exact
    call whose flag the repair layer kept stripping."""
    args = {'description': 'patch smali',
            'allow_outside_workspace': True,
            'edits': [{'path': '/abs/eh/c.smali', 'operation': 'insert_after',
                       'anchor': '    return-void', 'content': '    nop'}]}
    out, log = validate_then_repair('edit_file', args)
    assert out['allow_outside_workspace'] is True
    assert out['edits'] == args['edits']
    assert out['description'] == 'patch smali'
    assert ('edit_file', 'structural_transform') not in log


def test_edit_file_allow_outside_workspace_survives_foreign_reshape():
    """The flag is envelope-scoped, not edit-item data: a foreign-shaped
    payload that legitimately gets reshaped must still carry it through."""
    out, log = validate_then_repair(
        'edit_file', {'file_path': '/abs/eh/c.smali', 'old_string': 'x',
                      'new_string': 'y', 'allow_outside_workspace': True})
    assert out['edits'] == [{'path': '/abs/eh/c.smali', 'operation': 'replace',
                             'anchor': 'x', 'content': 'y'}]
    assert out['allow_outside_workspace'] is True
    assert ('edit_file', 'structural_transform') in log

def test_apply_diffs_native_spelling_gets_no_caption_backfill():
    """Top-level native ``path`` + native item keys: the multiedit reshape
    fires (shared-path injection) but the caption backfill must not —
    nothing marks this payload as foreign-schema-shaped."""
    out, _ = validate_then_repair('apply_diffs', {
        'path': 'a.py',
        'edits': [{'search': 'x', 'replace': 'y'}]})
    assert out['edits'][0]['path'] == 'a.py'
    assert 'description' not in out['edits'][0]


def test_existing_description_never_clobbered():
    out, log = validate_then_repair(
        'write_file', {'file_path': 'a.py', 'content': 'x',
                       'description': 'seed the config'})
    assert out['description'] == 'seed the config'
    assert ('write_file', 'structural_transform') not in log


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


def test_gateway_stringified_calls_preserve_nested_arguments():
    import json
    calls = [{'name': 'environment_providers', 'arguments': {}}]
    args, log = validate_then_repair('execute_tools', {'calls': json.dumps(calls)})
    assert args['calls'] == calls
    assert ('calls', 'stringified_json') in log
    valid, valid_log = validate_then_repair('execute_tools', {'calls': calls})
    assert valid['calls'] == calls
    assert not valid_log


def test_gateway_repair_does_not_interpret_nested_user_strings():
    import json
    calls = [{'name': 'write_file', 'arguments': {'content': '[1,2]'}}]
    args, log = validate_then_repair('execute_tools', {'calls': json.dumps(calls)})
    assert args['calls'][0]['arguments']['content'] == '[1,2]'
    assert log == [('calls', 'stringified_json')]
