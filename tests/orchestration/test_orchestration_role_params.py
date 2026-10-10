"""tests/orchestration/test_orchestration_role_params.py — per-role structured params.

Covers the hybrid structured-params design (each role keeps a core
``objective`` field with a role-specific LABEL, plus role-specific structured
fields) implemented in ``lib/orchestration.py`` and rendered into the
delegation brief by the graph's invocation scheduling policy.

Layers under test:
  1. Schema well-formedness — every FieldSpec is structurally sound.
  2. Back-compat — an objective-only node renders byte-identical to the
     pre-structured-params behavior; the canonical builders are unchanged.
  3. Validation — per-field kind/enum/cap checks are ERRORS; unknown keys
     are WARNINGS (forward-compat).
  4. render_role_brief — section presence/omission/ordering per role.
  5. Engine integration — the rendered brief reaches the child invocation input.
"""

from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/parse.py'}

import unittest
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit

from lib.orchestration._builtin_definitions import (
    build_autopilot_definition,
)
from lib.orchestration._execution_projection import render_role_brief
from lib.orchestration._role_specs import (
    MAX_LIST_ITEM_LEN,
    MAX_LIST_ITEMS,
    MAX_OBJECTIVE_LEN,
    ROLE_PARAM_SCHEMA,
    role_param_schema,
)
from lib.orchestration._validate import validate_definition
from lib.orchestration.field_spec_contract import VALID_PARAM_KINDS
from lib.orchestration.engine import FlowExecutor
from tests.support.orchestration_definitions import (
    build_verifier_loop_definition,
)


def _wrap(role, params):
    """Minimal valid single-role flow for validation tests."""
    return {'schema': 'tofu.orchestration/v1', 'name': 'T', 'nodes': [
        {'id': 's', 'type': 'control', 'kind': 'start'},
        {'id': 'r', 'type': 'role', 'role': role, 'params': params},
        {'id': 'e', 'type': 'control', 'kind': 'stop'}],
        'edges': [{'from': 's', 'to': 'r'}, {'from': 'r', 'to': 'e'}]}


class SchemaWellFormednessTest(unittest.TestCase):
    def _all_schemas(self):
        yield '__generic__', role_param_schema('__generic__')
        for role in ROLE_PARAM_SCHEMA:
            yield role, ROLE_PARAM_SCHEMA[role]

    def test_every_field_is_well_formed(self):
        for role, schema in self._all_schemas():
            self.assertIsInstance(schema, list, role)
            self.assertTrue(schema, f'{role} schema empty')
            for spec in schema:
                self.assertIn('key', spec, (role, spec))
                self.assertIn('kind', spec, (role, spec))
                self.assertIn('label', spec, (role, spec))
                self.assertIn(spec['kind'], VALID_PARAM_KINDS, (role, spec))
                self.assertIsInstance(spec['label'], str, (role, spec))
                self.assertTrue(spec['label'].startswith('orch.'),
                                f'{role}:{spec["key"]} label not an i18n key')

    def test_every_role_has_objective_core(self):
        # The hybrid contract: every role keeps the core objective field.
        for role, schema in self._all_schemas():
            keys = [s['key'] for s in schema]
            self.assertIn('objective', keys, f'{role} lost its objective core')
            self.assertEqual(keys[0], 'objective',
                             f'{role} objective must lead the schema')

    def test_select_fields_have_options(self):
        for role, schema in self._all_schemas():
            for spec in schema:
                if spec['kind'] == 'select':
                    self.assertIn('options', spec, (role, spec))
                    self.assertTrue(spec['options'], (role, spec))
                    for opt in spec['options']:
                        self.assertIn('value', opt, (role, opt))
                        self.assertIn('label', opt, (role, opt))

    def test_field_specs_publish_the_limits_used_by_validation(self):
        from lib.orchestration._role_specs import _validate_role_params
        for role, schema in self._all_schemas():
            for spec in schema:
                if spec['kind'] in {'text', 'textarea'}:
                    value = 'x' * spec['maxLength']
                    errors = []
                    _validate_role_params(role, 'node', {spec['key']: value}, errors, [])
                    self.assertEqual(errors, [])
                    _validate_role_params(role, 'node', {spec['key']: value + 'x'}, errors, [])
                    self.assertTrue(errors)
                if spec['kind'] == 'list':
                    value = ['x'] * spec['maxItems']
                    errors = []
                    _validate_role_params(role, 'node', {spec['key']: value}, errors, [])
                    self.assertEqual(errors, [])
                    _validate_role_params(role, 'node', {spec['key']: value + ['x']}, errors, [])
                    self.assertTrue(errors)

    def test_keys_unique_within_role(self):
        for role, schema in self._all_schemas():
            keys = [s['key'] for s in schema]
            self.assertEqual(len(keys), len(set(keys)), f'{role} dup keys')

    def test_unknown_role_gets_generic(self):
        self.assertEqual(role_param_schema('totally-made-up'),
                         role_param_schema('__generic__'))

    def test_capability_roles_have_bespoke_schema(self):
        # The audit: every first-class palette role with specific capabilities
        # must expose a bespoke field set, NOT fall through to the bare
        # generic (task + expected_outcome) schema.
        generic_keys = [s['key'] for s in role_param_schema('__generic__')]
        for role in ['coder', 'analyst', 'writer', 'browser', 'synthesizer',
                     'router', 'planner']:
            self.assertIn(role, ROLE_PARAM_SCHEMA, f'{role} has no bespoke schema')
            keys = [s['key'] for s in ROLE_PARAM_SCHEMA[role]]
            self.assertNotEqual(keys, generic_keys,
                                f'{role} still uses the generic schema')

    def test_bespoke_field_keys_present(self):
        # Spot-check the signature field of each new schema matches the
        # role's real capability (so the inspector surfaces the right input).
        expect = {
            'coder': 'scope_paths', 'analyst': 'data_sources',
            'writer': 'audience', 'browser': 'steps',
            'synthesizer': 'conflict_policy', 'router': 'categories',
            'planner': 'acceptance_criteria',
        }
        for role, key in expect.items():
            keys = [s['key'] for s in ROLE_PARAM_SCHEMA[role]]
            self.assertIn(key, keys, f'{role} missing {key}')

    def test_role_contracts_have_focused_physical_owners(self):
        axes = (ROOT / 'lib/orchestration/_role_axes.py').read_text()
        specs = (ROOT / 'lib/orchestration/_role_specs.py').read_text()
        personas = (ROOT / 'lib/orchestration/_role_personas.py').read_text()

        self.assertIn('EXECUTION_OPTION_ORDER = {', axes)
        self.assertNotIn('ROLE_PARAM_SCHEMA = {', axes)
        self.assertIn('ROLE_PARAM_SCHEMA = {', specs)
        self.assertNotIn('def role_persona(', specs)
        self.assertIn('def role_persona(', personas)
        self.assertIn('get_role_model_hint', personas)
        self.assertNotIn("config.get('model_hint', 'standard')", personas)
        self.assertNotIn('ROLE_PARAM_SCHEMA = {', personas)


class BackCompatTest(unittest.TestCase):
    def test_objective_only_renders_byte_identical(self):
        for role in ['worker', 'planner', 'critic', 'researcher', 'general',
                     'writer', 'virtual_user', 'made-up-role']:
            node = {'role': role, 'params': {'objective': 'Do the thing.'}}
            self.assertEqual(render_role_brief(node), 'Do the thing.', role)

    def test_empty_params_render_empty(self):
        self.assertEqual(render_role_brief({'role': 'general', 'params': {}}), '')
        self.assertEqual(render_role_brief({'role': 'worker'}), '')

    def test_canonical_builders_still_valid(self):
        for defn in (
            build_verifier_loop_definition(),
            build_autopilot_definition(),
        ):
            v = validate_definition(defn)
            self.assertTrue(v['ok'], v['errors'])

    def test_canonical_builder_briefs_are_plain_objectives(self):
        # The builders set only objective → rendered brief == that objective,
        # so structured params do not change verifier-loop prompt rendering.
        defn = build_verifier_loop_definition()
        for n in defn['nodes']:
            if n.get('type') == 'role':
                self.assertEqual(render_role_brief(n),
                                 n['params']['objective'], n['id'])

    def test_pre_structured_fixture_still_validates(self):
        # A definition authored before structured params (only objective/tier).
        old = _wrap('worker', {'objective': 'x', 'tier': 'heavy',
                               'isolation': 'shared-context'})
        self.assertTrue(validate_definition(old)['ok'])


class ValidationTest(unittest.TestCase):
    def test_bad_select_value_is_error(self):
        v = validate_definition(_wrap('critic', {'verdict_format': 'bogus'}))
        self.assertFalse(v['ok'])
        self.assertTrue(any('verdict_format' in e for e in v['errors']))

    def test_good_select_value_passes(self):
        v = validate_definition(_wrap('critic', {'verdict_format': 'pass_fail'}))
        self.assertTrue(v['ok'], v['errors'])

    def test_bool_field_type_checked(self):
        v = validate_definition(_wrap('critic', {'adversarial': 'yes'}))
        self.assertFalse(v['ok'])
        self.assertTrue(any('adversarial' in e for e in v['errors']))
        self.assertTrue(validate_definition(
            _wrap('critic', {'adversarial': True}))['ok'])

    def test_list_field_type_checked(self):
        v = validate_definition(_wrap('worker', {'must_do': 123}))
        self.assertFalse(v['ok'])
        self.assertTrue(any('must_do' in e for e in v['errors']))

    def test_list_accepts_list_and_string(self):
        self.assertTrue(validate_definition(
            _wrap('worker', {'must_do': ['a', 'b']}))['ok'])
        self.assertTrue(validate_definition(
            _wrap('worker', {'must_do': 'a\nb'}))['ok'])

    def test_list_item_count_cap(self):
        v = validate_definition(_wrap('worker', {'must_do': [f'i{n}' for n in range(40)]}))
        self.assertFalse(v['ok'])
        self.assertTrue(any('items' in e for e in v['errors']))

    def test_unknown_param_is_warning_not_error(self):
        v = validate_definition(_wrap('worker', {'objective': 'x', 'zzz': 'q'}))
        self.assertTrue(v['ok'], v['errors'])
        self.assertTrue(any('zzz' in w for w in v['warnings']))

    def test_infra_keys_not_flagged_unknown(self):
        v = validate_definition(_wrap('worker',
                                      {'tier': 'heavy', 'isolation': 'fresh-context',
                                       'emits': 'assistant'}))
        self.assertTrue(v['ok'], v['errors'])
        self.assertFalse(any('unknown param' in w for w in v['warnings']),
                         v['warnings'])


class RenderBriefTest(unittest.TestCase):
    def test_sections_present_when_set(self):
        node = {'role': 'worker', 'params': {
            'objective': 'Build X.', 'must_do': ['a', 'b'],
            'must_not_do': ['no c'], 'expected_outcome': 'X works.'}}
        out = render_role_brief(node)
        self.assertTrue(out.startswith('Build X.'))
        self.assertIn('### Must Do', out)
        self.assertIn('- a', out)
        self.assertIn('### Must Not Do', out)
        self.assertIn('### Expected Outcome', out)

    def test_empty_sections_omitted(self):
        node = {'role': 'worker', 'params': {
            'objective': 'Build X.', 'must_do': [], 'must_not_do': '',
            'expected_outcome': '   '}}
        self.assertEqual(render_role_brief(node), 'Build X.')

    def test_bool_renders_only_when_true(self):
        on = render_role_brief({'role': 'critic', 'params': {
            'objective': 'c', 'adversarial': True}})
        self.assertIn('### Adversarial Verification', on)
        off = render_role_brief({'role': 'critic', 'params': {
            'objective': 'c', 'adversarial': False}})
        self.assertNotIn('Adversarial', off)
        self.assertEqual(off, 'c')

    def test_section_order_follows_schema(self):
        node = {'role': 'worker', 'params': {
            'objective': 'o', 'expected_outcome': 'eo', 'must_do': ['m'],
            'must_not_do': ['n']}}
        out = render_role_brief(node)
        self.assertLess(out.index('### Must Do'), out.index('### Must Not Do'))
        self.assertLess(out.index('### Must Not Do'), out.index('### Expected Outcome'))

    def test_objective_omitted_still_renders_sections(self):
        # No objective, but a set section → brief is just the section.
        node = {'role': 'worker', 'params': {'must_do': ['only this']}}
        out = render_role_brief(node)
        self.assertEqual(out, '### Must Do\n- only this')

    def test_coder_bespoke_sections_render(self):
        node = {'role': 'coder', 'params': {
            'objective': 'Fix the parser.', 'scope_paths': ['lib/parse.py'],
            'constraints': ['no public API change'],
            'verify_cmd': 'pytest tests/test_parse.py'}}
        out = render_role_brief(node)
        self.assertTrue(out.startswith('Fix the parser.'))
        self.assertIn('### Files / Paths', out)
        self.assertIn('- lib/parse.py', out)
        self.assertIn('### Constraints', out)
        self.assertIn('### Verify Command', out)
        self.assertIn('pytest tests/test_parse.py', out)

    def test_writer_select_renders_value(self):
        node = {'role': 'writer', 'params': {
            'objective': 'Write the README.', 'tone': 'technical',
            'audience': 'maintainers'}}
        out = render_role_brief(node)
        self.assertIn('### Tone', out)
        self.assertIn('technical', out)
        self.assertIn('### Audience', out)


class EngineIntegrationTest(unittest.TestCase):
    def test_rendered_brief_reaches_child_invocation_input(self):
        """Default graph scheduling supplies the full brief, not just objective."""
        from unittest.mock import patch
        from lib.tasks_pkg.manager import create_task, discard_task
        from lib.tasks_pkg.manager.runtime import chat_task_runtime

        captured = []

        def execute(child, *, policy):
            self.assertTrue(policy.is_child)
            self.assertEqual(policy.parent_task_id, parent['id'])
            captured.append(child)
            child.update(content='done', status='done', finishReason='stop',
                         streamState='provider_finished')

        node = {'id': 'w', 'type': 'role', 'role': 'worker', 'params': {
            'objective': 'Build X.', 'must_do': ['ship it'],
            'expected_outcome': 'X works.'}}
        defn = {'schema': 'tofu.orchestration/v1', 'name': 'T', 'nodes': [
            {'id': 's', 'type': 'control', 'kind': 'start'}, node,
            {'id': 'e', 'type': 'control', 'kind': 'stop'}],
            'edges': [{'from': 's', 'to': 'w'}, {'from': 'w', 'to': 'e'}]}
        parent = create_task('', [], {'model': 'fixture-model'}, user_id=71,
                             transient=True, supersede=False)
        try:
            with patch('lib.tasks_pkg.child_invocation.execute_invocation', execute):
                result = FlowExecutor(
                    defn, parent_task=parent, model='fixture-model',
                    model_routing_policy='selected',
                ).run(initial_context='Accepted request context.')
            self.assertTrue(result['ok'], result.get('error'))
            self.assertEqual(len(captured), 1)
            child = captured[0]
            inputs = [message['content'] for message in child['messages']
                      if message['role'] == 'user']
            self.assertEqual(len(inputs), 1)
            self.assertTrue(inputs[0].startswith('Build X.'), inputs[0])
            self.assertIn('### Must Do\n- ship it', inputs[0])
            self.assertIn('### Expected Outcome\nX works.', inputs[0])
            self.assertIn('Accepted request context.', inputs[0])
            self.assertIsNone(chat_task_runtime.get(child['id']))
            self.assertNotEqual(parent['status'], 'done')
        finally:
            discard_task(parent['id'])


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_orchestration_role_params.__main__')
    unittest.main()
