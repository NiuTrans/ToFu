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

import copy
import logging

from jsonschema import Draft7Validator
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

    def test_kimi_root_union_is_projected_and_nested_union_is_normalized(
            self, caplog):
        dynamic = _fn('dynamic_choice')
        dynamic['function']['parameters'] = {
            'type': 'object',
            'properties': {
                'mode': {'type': 'string'},
                'payload': {
                    'type': 'object',
                    'properties': {'kind': {'type': 'string'}},
                    'anyOf': [
                        {'properties': {'kind': {'const': 'a'}}},
                        {'properties': {'kind': {'const': 'b'}}},
                    ],
                },
            },
            'required': ['mode'],
            'anyOf': [
                {'properties': {'mode': {'const': 'read'}}},
                {'properties': {'mode': {'const': 'write'}}},
            ],
        }
        original = copy.deepcopy(dynamic)

        with caplog.at_level(logging.WARNING):
            normalized_tools = sanitize_wire_tools(
                [dynamic], model='kimi-k3')

        assert dynamic == original
        assert normalized_tools[0] is not dynamic
        parameters = normalized_tools[0]['function']['parameters']
        assert parameters['type'] == 'object'
        assert 'anyOf' not in parameters
        nested = parameters['properties']['payload']
        assert 'type' not in nested
        assert [branch['type'] for branch in nested['anyOf']] == [
            'object', 'object']
        assert [
            branch['properties']['kind']['enum'][0]
            for branch in nested['anyOf']
        ] == ['a', 'b']
        assert 'repaired_schema' in caplog.text
        assert 'dynamic_choice' in caplog.text

        before = Draft7Validator(original['function']['parameters'])
        after = Draft7Validator(parameters)
        for value in (
                {'mode': 'read'},
                {'mode': 'write', 'payload': {'kind': 'b'}}):
            assert before.is_valid(value)
            assert after.is_valid(value)
        # Root alternatives are not expressible in MFJS tool parameters.
        # The wire projection is intentionally a relaxation; the untouched
        # canonical schema remains the execution validator.
        assert not before.is_valid({'mode': 'invalid'})
        assert after.is_valid({'mode': 'invalid'})
        assert not after.is_valid([])
        assert not after.is_valid('not-an-object')

        fitted = fit_tool_schema_budget(
            [dynamic], budget_tokens=0, model='kimi-k3')
        assert fitted == normalized_tools

    def test_kimi_repairs_persisted_branch_typed_root_union(self):
        # Exact shape captured from the 2026-08-30 ask_human incident after
        # the first attempted repair: no root type, object types in branches.
        dynamic = _fn('persisted_shape')
        dynamic['function']['parameters'] = {
            'properties': {'mode': {'type': 'string'}},
            'anyOf': [
                {'type': 'object', 'properties': {'mode': {'enum': ['a']}}},
                {'type': 'object', 'properties': {'mode': {'enum': ['b']}}},
            ],
        }

        out = sanitize_wire_tools([dynamic], model='kimi-k3')

        parameters = out[0]['function']['parameters']
        assert parameters['type'] == 'object'
        assert 'anyOf' not in parameters
        assert dynamic['function']['parameters'].get('type') is None

    def test_kimi_repairs_missing_and_true_parameter_schemas(self):
        missing = {'type': 'function', 'function': {'name': 'no_arguments'}}
        unconstrained = {
            'type': 'function',
            'function': {'name': 'any_arguments', 'parameters': True},
        }

        wire = sanitize_wire_tools(
            [missing, unconstrained], model='kimi-k3')

        assert [tool['function']['parameters']['type'] for tool in wire] == [
            'object', 'object']
        assert 'parameters' not in missing['function']
        assert unconstrained['function']['parameters'] is True

    def test_kimi_isolates_false_parameter_schema(self):
        impossible = {
            'type': 'function',
            'function': {'name': 'impossible', 'parameters': False},
        }
        diagnostics = []

        assert sanitize_wire_tools(
            [impossible], model='kimi-k3',
            on_tool_isolated=diagnostics.append,
        ) == []
        assert diagnostics[0]['reasonCode'] == 'invalid_schema'
        assert 'does not permit object tool arguments' in diagnostics[0][
            'detail']

    def test_non_kimi_keeps_strict_root_union_by_identity(self):
        dynamic = _fn('strict_elsewhere')
        dynamic['function']['parameters']['anyOf'] = [
            {'properties': {'mode': {'enum': ['a']}}},
            {'properties': {'mode': {'enum': ['b']}}},
        ]
        tools = [dynamic]

        assert sanitize_wire_tools(tools, model='gpt-5.6') is tools

    def test_already_mfjs_clean_kimi_catalog_keeps_identity(self):
        tools = [_fn('clean_mfjs')]

        assert sanitize_wire_tools(tools, model='kimi-k3') is tools

    def test_kimi_projects_root_allof_and_nested_schema_dialects(self):
        dynamic = _fn('mixed_dialects')
        dynamic['function']['parameters'] = {
            'allOf': [
                {
                    'type': 'object',
                    'properties': {
                        'choice': {
                            'oneOf': [
                                {'type': ['string', 'null']},
                                {'const': 7},
                            ],
                        },
                    },
                    'required': ['choice'],
                },
                {
                    'properties': {'note': {'type': 'string'}},
                    'required': ['note'],
                },
            ],
        }

        out = sanitize_wire_tools([dynamic], model='kimi-k3')

        parameters = out[0]['function']['parameters']
        assert parameters['type'] == 'object'
        assert set(parameters['properties']) == {'choice', 'note'}
        assert parameters['required'] == ['choice', 'note']
        wire_json = repr(parameters)
        assert 'allOf' not in wire_json
        assert 'oneOf' not in wire_json
        assert 'const' not in wire_json
        assert "['string', 'null']" not in wire_json

    def test_kimi_projects_the_documented_mfjs_subset_end_to_end(self):
        from lib.tools.moonshot_schema import mfjs_schema_error

        dynamic = _fn('wide_json_schema')
        dynamic['function']['parameters'] = {
            'type': 'object',
            'title': 'unsupported annotation',
            'format': 'unsupported-at-object',
            'properties': {
                'uri': {
                    'type': 'string',
                    'format': 'uri',
                    'minLength': 1,
                },
                'mixed': {'enum': ['a', 1]},
                'tuple': {
                    'type': 'array',
                    'prefixItems': [{'type': 'string'}],
                    'items': False,
                },
                'patterned': {
                    'type': 'object',
                    'patternProperties': {'^x': {'type': 'integer'}},
                    'additionalProperties': False,
                },
                'node': {'$ref': '#/definitions/Node'},
                'never': False,
            },
            # Legal JSON Schema: required need not appear in properties.
            'required': ['uri', 'undeclared'],
            'definitions': {
                'Node': {
                    'type': 'object',
                    'properties': {
                        'next': {
                            'anyOf': [
                                {'$ref': '#/definitions/Node'},
                                {'type': 'null'},
                            ],
                        },
                    },
                },
            },
        }
        original = copy.deepcopy(dynamic)

        wire = sanitize_wire_tools([dynamic], model='kimi-k3')

        assert dynamic == original
        parameters = wire[0]['function']['parameters']
        assert mfjs_schema_error(
            parameters, path='$.function.parameters') == ''
        assert 'title' not in parameters
        assert 'format' not in parameters
        assert parameters['properties']['uri'] == {'type': 'string'}
        assert parameters['properties']['mixed'] == {}
        assert parameters['properties']['tuple']['items'] == {}
        assert parameters['properties']['patterned'][
            'additionalProperties'] is True
        assert parameters['properties']['never'] == {}
        assert parameters['properties']['undeclared'] == {}
        assert 'definitions' not in parameters
        assert parameters['properties']['node']['$ref'] == '#/$defs/Node'
        recursive_ref = parameters['$defs']['Node']['properties'][
            'next']['anyOf'][0]['$ref']
        assert recursive_ref == '#/$defs/Node'

    def test_kimi_repairs_required_without_local_property_declaration(self):
        canonical = _fn('cross_schema_required')
        canonical['function']['parameters'].update({
            'required': ['declared_elsewhere'],
        })

        wire = sanitize_wire_tools([canonical], model='kimi-k3')

        assert wire[0]['function']['parameters']['properties'] == {
            'declared_elsewhere': {},
        }
        assert 'properties' not in canonical['function']['parameters']

    def test_kimi_relaxes_multiple_sibling_applicators(self):
        from lib.tools.moonshot_schema import mfjs_schema_error

        dynamic = _fn('intersected_applicators')
        dynamic['function']['parameters'].update({
            'properties': {
                'value': {
                    'type': 'string',
                    'allOf': [{'minLength': 1}],
                    'anyOf': [{'const': 'a'}, {'const': 'b'}],
                    'oneOf': [{'pattern': '^a'}, {'pattern': '^b'}],
                },
            },
            'anyOf': [{'required': ['value']}],
            'allOf': [{'additionalProperties': False}],
        })

        wire = sanitize_wire_tools([dynamic], model='kimi-k3')

        parameters = wire[0]['function']['parameters']
        assert mfjs_schema_error(parameters) == ''
        assert not ({'anyOf', 'oneOf', 'allOf'} & set(parameters))
        nested = parameters['properties']['value']
        assert 'oneOf' not in nested
        assert 'allOf' not in nested
        assert 'type' not in nested
        assert [branch['type'] for branch in nested['anyOf']] == [
            'string', 'string']

    def test_kimi_relaxes_external_ref_instead_of_rejecting_request(self):
        dynamic = _fn('external_ref')
        dynamic['function']['parameters']['properties'] = {
            'payload': {'$ref': 'https://example.invalid/schema.json'},
        }

        wire = sanitize_wire_tools([dynamic], model='kimi-k3')

        assert wire[0]['function']['parameters']['properties']['payload'] == {}

    def test_all_builtin_schemas_survive_kimi_preflight(self):
        # Inventory assembly covers built-ins that do not coexist in one UI
        # mode. Dynamic MCP/plugin schemas traverse the same runtime boundary.
        from scripts.gen_tool_inventory import _tool_schemas

        functions = _tool_schemas()
        tools = [
            {'type': 'function', 'function': function}
            for function in functions.values()
        ]
        diagnostics = []

        wire = sanitize_wire_tools(
            tools, model='kimi-k3',
            on_tool_isolated=diagnostics.append,
        )

        assert diagnostics == []
        assert len(wire) == len(tools)
        from lib.tools.moonshot_schema import mfjs_schema_error

        for tool in wire:
            parameters = tool['function'].get('parameters')
            if not isinstance(parameters, dict):
                continue
            assert parameters.get('type') == 'object', tool['function']['name']
            assert not ({'anyOf', 'oneOf', 'allOf'} & set(parameters)), (
                tool['function']['name'], parameters)
            assert mfjs_schema_error(parameters) == '', tool['function']['name']

    def test_impossible_type_anyof_is_isolated_instead_of_changing_meaning(self):
        impossible = _fn('impossible_choice')
        impossible['function']['parameters'] = {
            'type': 'object',
            'anyOf': [{'type': 'string'}],
        }
        diagnostics = []

        assert sanitize_wire_tools(
            [impossible], model='kimi-k3',
            on_tool_isolated=diagnostics.append,
        ) == []
        assert diagnostics[0]['toolName'] == 'impossible_choice'
        assert diagnostics[0]['reasonCode'] == 'invalid_schema'
        assert 'no object-compatible branch' in diagnostics[0]['detail']

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
        assert sanitize_wire_tools(fitted, model='kimi-k3') is fitted


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

    def test_kimi_type_anyof_is_normalized_before_provider_projection(self):
        schema = _fn('choice_tool')
        schema['function']['parameters'] = {
            'type': 'object',
            'properties': {'mode': {'type': 'string'}},
            'anyOf': [
                {'properties': {'mode': {'const': 'a'}}},
                {'properties': {'mode': {'const': 'b'}}},
            ],
        }
        diagnostics = []
        from lib.llm._sse_core import prepare_request

        plan = prepare_request({
            'model': 'kimi-k3',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': [schema],
            '_request_activity_sink': diagnostics.append,
        }, log_prefix='[kimi-anyof]')

        parameters = plan.body['tools'][0]['function']['parameters']
        assert parameters['type'] == 'object'
        assert 'anyOf' not in parameters
        projection = next(
            row for row in diagnostics if row.get('kind') == 'wire_projection')
        assert projection['toolNames'] == ['choice_tool']
        assert projection['toolCount'] == 1
        assert '_request_activity_sink' not in plan.body

    def test_nonstream_chat_uses_the_same_kimi_schema_preflight(
            self, monkeypatch):
        import importlib

        chat_module = importlib.import_module('lib.llm.chat')
        captured = {}

        class Response:
            status_code = 200
            headers = {}

            @staticmethod
            def json():
                return {
                    'choices': [{
                        'finish_reason': 'stop',
                        'message': {'content': 'OK'},
                    }],
                    'usage': {},
                }

        def post(_url, **kwargs):
            captured['body'] = kwargs['json']
            return Response()

        monkeypatch.setattr(chat_module, 'http_post', post)
        schema = _fn('nonstream_choice')
        schema['function']['parameters']['anyOf'] = [
            {'properties': {'mode': {'enum': ['a']}}},
            {'properties': {'mode': {'enum': ['b']}}},
        ]

        content, _usage = chat_module.chat(
            [{'role': 'user', 'content': 'reply OK'}],
            model='kimi-k3', api_key='test-only',
            base_url='https://public.example/v1', max_retries=0,
            extra={'tools': [schema]},
        )

        assert content == 'OK'
        parameters = captured['body']['tools'][0]['function']['parameters']
        assert parameters['type'] == 'object'
        assert 'anyOf' not in parameters

    def test_no_tools_key_untouched(self):
        plan = self._prepare(None)
        assert plan.body.get('tools') in (None, [])

    def test_activity_sink_observes_isolation_and_never_reaches_wire(self):
        from lib.llm._sse_core import prepare_request

        malformed = _fn('write_file')
        malformed['function']['parameters']['properties'] = []
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
        malformed['function']['parameters']['properties'] = []
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


class TestKimiAnyOfApplicatorFold:
    """mtgrjqtuhzi4i9 incident: a nested anyOf whose PARENT also declares
    properties/required/additionalProperties is a vendor hard-400
    ("conflicting keywords found in anyOf with parent"). Standard JSON
    Schema treats parent-side applicators as an intersection with every
    branch, so the wire projection folds them INTO each object branch —
    logically equivalent and vendor-legal."""

    _SHAPE = {
        'type': 'object',
        'properties': {
            'payload': {
                'type': 'object',
                'properties': {'kind': {'type': 'string'}},
                'required': ['kind'],
                'additionalProperties': False,
                'anyOf': [
                    {'properties': {'kind': {'const': 'a'}}},
                    {'properties': {'kind': {'const': 'b'}}},
                ],
            },
        },
    }

    def test_fold_applied_and_wire_passes_mfjs(self):
        from lib.tools.moonshot_schema import mfjs_schema_error

        dynamic = _fn('conflicted_union')
        dynamic['function']['parameters'] = copy.deepcopy(self._SHAPE)
        original = copy.deepcopy(dynamic)

        out = sanitize_wire_tools([dynamic], model='kimi-k3')

        assert dynamic == original  # canonical caller copy untouched
        payload = out[0]['function']['parameters']['properties']['payload']
        assert [b['type'] for b in payload['anyOf']] == ['object', 'object']
        # Parent-side applicators moved INTO the branches, off the parent.
        for key in ('properties', 'required', 'additionalProperties'):
            assert key not in payload
        for branch, const in zip(payload['anyOf'], ('a', 'b')):
            assert branch['properties']['kind']['enum'] == [const]
            assert branch['required'] == ['kind']
            assert branch['additionalProperties'] is False
        assert mfjs_schema_error(out[0]['function']['parameters']) == ''

    def test_fold_preserves_validation_semantics(self):
        dynamic = _fn('conflicted_union')
        dynamic['function']['parameters'] = copy.deepcopy(self._SHAPE)
        before = Draft7Validator(dynamic['function']['parameters'])

        after = Draft7Validator(sanitize_wire_tools(
            [dynamic], model='kimi-k3')[0]['function']['parameters'])

        for value, expected in (
                ({'payload': {'kind': 'a'}}, True),
                ({'payload': {'kind': 'b'}}, True),
                ({'payload': {'kind': 'c'}}, False),
                ({'payload': {'kind': 'a', 'extra': 1}}, False),
                ({'payload': {}}, False)):
            assert before.is_valid(value) is expected
            assert after.is_valid(value) is expected

    def test_non_object_branch_left_untouched(self):
        dynamic = _fn('mixed_union')
        dynamic['function']['parameters'] = {
            'type': 'object',
            'properties': {
                'payload': {
                    'properties': {'kind': {'type': 'string'}},
                    'anyOf': [
                        {'type': 'string'},
                        {'type': 'object',
                         'properties': {'kind': {'const': 'a'}}}],
                },
            },
        }

        payload = sanitize_wire_tools(
            [dynamic], model='kimi-k3'
        )[0]['function']['parameters']['properties']['payload']

        string_branch, object_branch = payload['anyOf']
        # Vacuous on non-object instances — folding there would be noise.
        assert string_branch == {'type': 'string'}
        assert object_branch['properties']['kind']['enum'] == ['a']
        assert 'properties' not in payload

    def test_mfjs_validator_flags_applicator_on_both_sides(self):
        from lib.tools.moonshot_schema import mfjs_schema_error

        conflicted = {
            'type': 'object',
            'properties': {
                'payload': {
                    'properties': {'a': {'type': 'string'}},
                    'anyOf': [
                        {'type': 'object',
                         'properties': {'b': {'type': 'string'}}}],
                },
            },
        }
        error = mfjs_schema_error(conflicted)
        assert 'payload' in error
        assert 'conflicts with' in error

        one_side = {
            'type': 'object',
            'properties': {
                'payload': {
                    'anyOf': [
                        {'type': 'object',
                         'properties': {'b': {'type': 'string'}}}],
                },
            },
        }
        assert mfjs_schema_error(one_side) == ''
