"""tests/test_orchestration_role_params.py — per-role structured params.

Covers the hybrid structured-params design (each role keeps a core
``objective`` field with a role-specific LABEL, plus role-specific structured
fields) implemented in ``lib/orchestration.py`` and rendered into the
delegation brief by the engine (Option A: render in the engine, the swarm
stays dumb).

Layers under test:
  1. Schema well-formedness — every FieldSpec is structurally sound.
  2. Back-compat — an objective-only node renders byte-identical to the
     pre-structured-params behavior; the canonical builders are unchanged.
  3. Validation — per-field kind/enum/cap checks are ERRORS; unknown keys
     are WARNINGS (forward-compat).
  4. render_role_brief — section presence/omission/ordering per role.
  5. Engine integration — the rendered brief reaches SubTaskSpec.objective.
"""

from __future__ import annotations

import unittest

from lib.orchestration import (
    ROLE_PARAM_SCHEMA, VALID_PARAM_KINDS, render_role_brief, role_param_schema,
    validate_definition, build_endpoint_definition, build_autopilot_definition,
)
from lib.orchestration_engine import FlowExecutor


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

    def test_keys_unique_within_role(self):
        for role, schema in self._all_schemas():
            keys = [s['key'] for s in schema]
            self.assertEqual(len(keys), len(set(keys)), f'{role} dup keys')

    def test_unknown_role_gets_generic(self):
        self.assertEqual(role_param_schema('totally-made-up'),
                         role_param_schema('__generic__'))


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
        for defn in (build_endpoint_definition(), build_autopilot_definition()):
            v = validate_definition(defn)
            self.assertTrue(v['ok'], v['errors'])

    def test_canonical_builder_briefs_are_plain_objectives(self):
        # The builders set only objective → rendered brief == that objective,
        # so the cutover changes nothing for endpoint/autopilot.
        defn = build_endpoint_definition()
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


class EngineIntegrationTest(unittest.TestCase):
    def test_rendered_brief_reaches_subtaskspec_objective(self):
        # Spy on SubTaskSpec to capture exactly what objective the engine's
        # DEFAULT runner constructs — it must be the rendered structured brief,
        # not the raw params['objective']. This pins the engine→spec seam.
        import lib.swarm.protocol as protocol

        captured = {}
        real_spec = protocol.SubTaskSpec

        def _spy(*args, **kwargs):
            captured['objective'] = kwargs.get('objective')
            return real_spec(*args, **kwargs)

        node = {'id': 'w', 'type': 'role', 'role': 'worker', 'params': {
            'objective': 'Build X.', 'must_do': ['ship it'],
            'expected_outcome': 'X works.'}}
        defn = {'schema': 'tofu.orchestration/v1', 'name': 'T', 'nodes': [
            {'id': 's', 'type': 'control', 'kind': 'start'}, node,
            {'id': 'e', 'type': 'control', 'kind': 'stop'}],
            'edges': [{'from': 's', 'to': 'w'}, {'from': 'w', 'to': 'e'}]}

        # The default runner builds a real SubAgent; stub its run() so no LLM
        # is invoked — we only care about the spec it was constructed with.
        import lib.swarm.agent as agent_mod

        class _StubAgent:
            def __init__(self, spec, **kw):
                self.spec = spec

            def run(self):
                class _R:
                    final_answer = 'done'
                    status = 'completed'
                    error_message = ''
                    tool_log = []
                return _R()

        orig_spec = protocol.SubTaskSpec
        orig_agent = agent_mod.SubAgent
        protocol.SubTaskSpec = _spy
        agent_mod.SubAgent = _StubAgent
        try:
            FlowExecutor(defn).run()
        finally:
            protocol.SubTaskSpec = orig_spec
            agent_mod.SubAgent = orig_agent

        obj = captured.get('objective') or ''
        self.assertTrue(obj.startswith('Build X.'), obj)
        self.assertIn('### Must Do', obj)
        self.assertIn('- ship it', obj)
        self.assertIn('### Expected Outcome', obj)


if __name__ == '__main__':
    unittest.main()
