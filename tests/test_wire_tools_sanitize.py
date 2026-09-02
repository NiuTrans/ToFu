"""Wire-tools invariant at the last common boundary (2026-08-19 incident).

A single ``None`` inside ``body['tools']`` crashed
``add_cache_breakpoints`` (``'NoneType' object has no attribute 'get'``,
Orchestrator run_task FATAL, task c9aba5d0) and hard-400ed the Gemini
pool-rescue chain ("Expected a(n) 'tools' array element to be an object");
a function entry missing ``type`` 400s kimi ("unknown tool type: ").
Producers are validated upstream, but the array crosses registry assembly,
conversation-latched catalogs, headless custom tools and rescue-body
re-dispatch — so ``prepare_request`` re-asserts the contract instead of
trusting every future producer.
"""

import logging

import pytest

from lib.tools.gateway import fit_tool_schema_budget, sanitize_wire_tools

pytestmark = pytest.mark.unit


def _fn(name, **extra):
    return {'type': 'function',
            'function': {'name': name, 'parameters': {'type': 'object'}},
            **extra}


class TestSanitizeWireTools:
    def test_clean_list_passes_through_by_identity(self):
        tools = [_fn('a'), _fn('b')]
        # Identity, not just equality: the prompt-cache hot path must not
        # pay a rebuild (or a byte-flip) when nothing is wrong.
        assert sanitize_wire_tools(tools) is tools

    def test_non_list_returns_empty(self):
        assert sanitize_wire_tools(None) == []
        assert sanitize_wire_tools('tools') == []

    def test_none_and_nameless_entries_dropped(self, caplog):
        tools = [_fn('good'), None, {'function': {}}, 42, _fn('good2')]
        with caplog.at_level(logging.WARNING):
            out = sanitize_wire_tools(tools)
        assert [t['function']['name'] for t in out] == ['good', 'good2']
        # The regressing producer must be named in the logs.
        assert 'non-dict' in caplog.text
        assert 'nameless' in caplog.text

    def test_missing_type_repaired_without_mutating_caller(self, caplog):
        broken = {'function': {'name': 'custom_tool'}}
        tools = [_fn('ok'), broken]
        with caplog.at_level(logging.WARNING):
            out = sanitize_wire_tools(tools)
        assert out[1]['type'] == 'function'
        assert out[1]['function']['name'] == 'custom_tool'
        assert out[0] is tools[0]  # untouched entries keep identity
        assert 'type' not in broken  # caller's canonical catalog not mutated
        assert 'repaired_type' in caplog.text

    def test_anthropic_shaped_entry_without_name_dropped(self):
        # No function.name and no top-level name → unusable on this wire.
        out = sanitize_wire_tools([{'input_schema': {}}, _fn('kept')])
        assert [t['function']['name'] for t in out] == ['kept']

    def test_required_property_mismatch_is_dropped_before_provider(self, caplog):
        malformed = _fn('broken')
        malformed['function']['parameters'].update({
            'properties': {'path': {'type': 'string'}},
            'required': ['description', 'path'],
        })
        tools = [_fn('good'), malformed]

        with caplog.at_level(logging.WARNING):
            out = sanitize_wire_tools(tools)

        assert [tool['function']['name'] for tool in out] == ['good']
        assert 'invalid-schema' in caplog.text
        assert 'description' in caplog.text

    def test_schema_isolation_reports_one_bounded_structured_diagnostic(self):
        malformed = _fn('write_file')
        malformed['function']['parameters'].update({
            'properties': {'path': {'type': 'string'}},
            'required': ['description', 'path'],
        })
        diagnostics = []

        assert sanitize_wire_tools(
            [malformed], on_tool_isolated=diagnostics.append,
        ) == []
        assert diagnostics == [{
            'toolName': 'write_file',
            'stage': 'wire_preflight',
            'reasonCode': 'invalid_schema',
            'detail': (
                '$.function.parameters.required references properties not '
                'declared at that level: description'
            ),
            'action': 'omitted',
        }]

    def test_kimi_500_token_compaction_preserves_description_parameter(self):
        verbose = 'provider-facing annotation ' * 300
        schema = _fn('search_catalog')
        schema['function']['description'] = verbose
        schema['function']['parameters'].update({
            'properties': {
                'query': {'type': 'string', 'description': verbose},
                # This is an ARGUMENT NAME, not a JSON-Schema annotation.
                'description': {'type': 'string', 'description': verbose},
            },
            'required': ['query', 'description'],
        })

        fitted = fit_tool_schema_budget(
            [schema], budget_tokens=500, model='kimi-k3',
        )

        assert len(fitted) == 1
        parameters = fitted[0]['function']['parameters']
        assert parameters['required'] == ['query', 'description']
        assert set(parameters['properties']) == {'query', 'description'}
        assert parameters['properties']['description'] == {'type': 'string'}
        assert sanitize_wire_tools(fitted) is fitted


class TestPrepareRequestBoundary:
    """End-to-end: the exact cache.py crash shape must not reach the wire."""

    def _prepare(self, tools):
        from lib.llm._sse_core import prepare_request
        body = {
            'model': 'gpt-5.6',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': tools,
        }
        plan = prepare_request(body, log_prefix='[t]')
        return plan

    def test_none_tool_neither_crashes_nor_leaks(self):
        # Regression: cache.py Phase 0 used to die on the None with
        # AttributeError before any wire validation ran.
        plan = self._prepare([None, _fn('ok_tool')])
        wire_tools = plan.body.get('tools') or []
        assert all(isinstance(t, dict) for t in wire_tools)
        assert [t['function']['name'] for t in wire_tools] == ['ok_tool']

    def test_missing_type_repaired_on_wire(self):
        plan = self._prepare([{'function': {'name': 'custom_tool'}}])
        wire_tools = plan.body.get('tools') or []
        assert wire_tools[0]['type'] == 'function'

    def test_no_tools_key_untouched(self):
        plan = self._prepare(None)
        assert plan.body.get('tools') in (None, [])

    def test_activity_sink_observes_isolation_and_never_reaches_wire(self):
        from lib.llm._sse_core import prepare_request

        malformed = _fn('write_file')
        malformed['function']['parameters'].update({
            'properties': {'path': {'type': 'string'}},
            'required': ['description', 'path'],
        })
        diagnostics = []
        plan = prepare_request({
            'model': 'kimi-k3',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': [malformed],
            '_request_activity_sink': diagnostics.append,
        }, log_prefix='[activity]')

        assert plan.body.get('tools') in (None, [])
        assert diagnostics[0]['toolName'] == 'write_file'
        assert diagnostics[0]['reasonCode'] == 'invalid_schema'
        assert '_request_activity_sink' not in plan.body

    def test_only_bad_tool_removes_empty_array_and_dangling_choice(self):
        from lib.llm._sse_core import prepare_request

        malformed = _fn('write_file')
        malformed['function']['parameters'].update({
            'properties': {'path': {'type': 'string'}},
            'required': ['description', 'path'],
        })
        plan = prepare_request({
            'model': 'kimi-k3',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': [malformed],
            'tool_choice': {
                'type': 'function',
                'function': {'name': 'write_file'},
            },
        }, log_prefix='[only-bad-tool]')

        assert 'tools' not in plan.body
        assert 'tool_choice' not in plan.body
