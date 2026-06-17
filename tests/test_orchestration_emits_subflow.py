"""tests/test_orchestration_emits_subflow.py — message-axis + nesting.

Covers the two new orchestration axes that let custom flows fully subsume
both built-in orchestrations (endpoint + autopilot):

  * ``emits`` — per-node MESSAGE axis (user|assistant), orthogonal to role.
  * ``subflow`` — a "big role" composed of small roles (nested flow),
    flattened by ``expand_subflows`` into one graph the engine runs.
  * ``build_autopilot_definition`` — autopilot expressed as a flow, and the
    virtual_user verdict semantics (keep going unless TASK_DONE).
"""

import unittest

from lib.orchestration import (
    MAX_SUBFLOW_DEPTH, SCHEMA_ID, VALID_SCOPES, build_autopilot_definition,
    build_endpoint_definition, expand_subflows, resolve_emits, resolve_scope,
    validate_definition,
)


class ResolveEmitsTest(unittest.TestCase):
    def test_explicit_emits_wins(self):
        self.assertEqual(
            resolve_emits({'role': 'critic', 'params': {'emits': 'assistant'}}),
            'assistant')
        self.assertEqual(
            resolve_emits({'role': 'worker', 'params': {'emits': 'user'}}),
            'user')

    def test_derived_from_role(self):
        self.assertEqual(resolve_emits({'role': 'critic'}), 'user')
        self.assertEqual(resolve_emits({'role': 'reviewer'}), 'user')
        self.assertEqual(resolve_emits({'role': 'virtual_user'}), 'user')
        self.assertEqual(resolve_emits({'role': 'worker'}), 'assistant')
        self.assertEqual(resolve_emits({'role': 'planner'}), 'assistant')
        self.assertEqual(resolve_emits({'role': 'researcher'}), 'assistant')

    def test_invalid_explicit_falls_through_to_derivation(self):
        self.assertEqual(
            resolve_emits({'role': 'critic', 'params': {'emits': 'bogus'}}),
            'user')


class EmitsValidationTest(unittest.TestCase):
    def _wrap(self, node):
        return {
            'schema': SCHEMA_ID, 'name': 'T',
            'nodes': [
                {'id': 's', 'type': 'control', 'kind': 'start'},
                node,
                {'id': 'e', 'type': 'control', 'kind': 'stop'},
            ],
            'edges': [{'from': 's', 'to': node['id']},
                      {'from': node['id'], 'to': 'e'}],
        }

    def test_valid_emits_passes(self):
        for val in ('user', 'assistant'):
            d = self._wrap({'id': 'w', 'type': 'role', 'role': 'worker',
                            'params': {'emits': val}})
            v = validate_definition(d)
            self.assertTrue(v['ok'], (val, v['errors']))

    def test_invalid_emits_is_error(self):
        d = self._wrap({'id': 'w', 'type': 'role', 'role': 'worker',
                        'params': {'emits': 'sideways'}})
        v = validate_definition(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('emits' in e for e in v['errors']))

    def test_virtual_user_is_known_role(self):
        d = self._wrap({'id': 'vu', 'type': 'role', 'role': 'virtual_user'})
        v = validate_definition(d)
        self.assertTrue(v['ok'], v['errors'])
        self.assertEqual(v['warnings'], [])


class SubflowValidationTest(unittest.TestCase):
    def _child(self):
        return {
            'schema': SCHEMA_ID, 'name': 'Child',
            'nodes': [
                {'id': 'cs', 'type': 'control', 'kind': 'start'},
                {'id': 'cw', 'type': 'role', 'role': 'coder'},
                {'id': 'ce', 'type': 'control', 'kind': 'stop'},
            ],
            'edges': [{'from': 'cs', 'to': 'cw'}, {'from': 'cw', 'to': 'ce'}],
        }

    def _wrap_subflow(self, params):
        return {
            'schema': SCHEMA_ID, 'name': 'Parent',
            'nodes': [
                {'id': 's', 'type': 'control', 'kind': 'start'},
                {'id': 'big', 'type': 'subflow', 'role': 'coder', 'params': params},
                {'id': 'e', 'type': 'control', 'kind': 'stop'},
            ],
            'edges': [{'from': 's', 'to': 'big'}, {'from': 'big', 'to': 'e'}],
        }

    def test_embedded_subflow_valid(self):
        d = self._wrap_subflow({'definition': self._child()})
        v = validate_definition(d)
        self.assertTrue(v['ok'], v['errors'])

    def test_subflow_without_def_or_ref_is_error(self):
        d = self._wrap_subflow({})
        v = validate_definition(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('definition' in e or 'ref' in e for e in v['errors']))

    def test_subflow_child_errors_bubble_up(self):
        bad_child = self._child()
        bad_child['name'] = ''   # invalid child
        d = self._wrap_subflow({'definition': bad_child})
        v = validate_definition(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('subflow' in e for e in v['errors']))

    def test_ref_recursion_detected(self):
        # A child that itself references the same ancestor ref.
        child = self._child()
        child['nodes'].append(
            {'id': 'inner', 'type': 'subflow', 'role': 'coder',
             'params': {'ref': 'flow-A'}})
        child['edges'].append({'from': 'cw', 'to': 'inner'})
        # validate with the ancestor ref already "seen"
        v = validate_definition(child, _seen_refs=frozenset({'flow-A'}))
        self.assertFalse(v['ok'])
        self.assertTrue(any('recursive' in e for e in v['errors']))

    def test_deep_nesting_capped(self):
        # Build a chain of embedded subflows deeper than the cap.
        def nest(depth):
            inner_role = {'id': 'r', 'type': 'role', 'role': 'coder'}
            d = {'schema': SCHEMA_ID, 'name': f'L{depth}',
                 'nodes': [
                     {'id': 'cs', 'type': 'control', 'kind': 'start'},
                     inner_role,
                     {'id': 'ce', 'type': 'control', 'kind': 'stop'}],
                 'edges': [{'from': 'cs', 'to': 'r'}, {'from': 'r', 'to': 'ce'}]}
            if depth > 0:
                d['nodes'][1] = {'id': 'r', 'type': 'subflow', 'role': 'coder',
                                 'params': {'definition': nest(depth - 1)}}
            return d
        d = nest(MAX_SUBFLOW_DEPTH + 2)
        v = validate_definition(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('MAX_SUBFLOW_DEPTH' in e or 'nesting' in e
                            for e in v['errors']))


class ExpandSubflowsTest(unittest.TestCase):
    def _child(self):
        return {
            'schema': SCHEMA_ID, 'name': 'Child',
            'nodes': [
                {'id': 'cs', 'type': 'control', 'kind': 'start'},
                {'id': 'a', 'type': 'role', 'role': 'researcher'},
                {'id': 'b', 'type': 'role', 'role': 'writer'},
                {'id': 'ce', 'type': 'control', 'kind': 'stop'},
            ],
            'edges': [{'from': 'cs', 'to': 'a'}, {'from': 'a', 'to': 'b'},
                      {'from': 'b', 'to': 'ce'}],
        }

    def _parent(self):
        return {
            'schema': SCHEMA_ID, 'name': 'Parent',
            'nodes': [
                {'id': 's', 'type': 'control', 'kind': 'start'},
                {'id': 'big', 'type': 'subflow', 'role': 'general',
                 'params': {'definition': self._child()}},
                {'id': 'e', 'type': 'control', 'kind': 'stop'},
            ],
            'edges': [{'from': 's', 'to': 'big'}, {'from': 'big', 'to': 'e'}],
        }

    def test_expansion_inlines_inner_nodes_namespaced(self):
        flat = expand_subflows(self._parent())
        ids = {n['id'] for n in flat['nodes']}
        self.assertIn('big/a', ids)
        self.assertIn('big/b', ids)
        # subflow node itself gone; child start/stop dropped
        self.assertNotIn('big', ids)
        self.assertNotIn('big/cs', ids)
        self.assertNotIn('big/ce', ids)
        # parent start/stop preserved
        self.assertIn('s', ids)
        self.assertIn('e', ids)

    def test_expansion_rewires_edges(self):
        flat = expand_subflows(self._parent())
        edges = {(e['from'], e['to']) for e in flat['edges']}
        # parent start now feeds the child entry
        self.assertIn(('s', 'big/a'), edges)
        # inner chain preserved
        self.assertIn(('big/a', 'big/b'), edges)
        # child exit now feeds the parent stop
        self.assertIn(('big/b', 'e'), edges)

    def test_expanded_graph_validates_and_runs_plan(self):
        from lib.orchestration_engine import compile_plan
        plan = compile_plan(self._parent())
        self.assertTrue(plan['ok'], plan.get('error'))
        roles = [s.get('role') for s in plan['steps'] if s.get('role')]
        self.assertIn('researcher', roles)
        self.assertIn('writer', roles)

    def test_no_subflows_returns_equivalent(self):
        ep = build_endpoint_definition()
        flat = expand_subflows(ep)
        self.assertEqual({n['id'] for n in flat['nodes']},
                         {n['id'] for n in ep['nodes']})

    def test_ref_without_resolver_raises_at_expand(self):
        d = {
            'schema': SCHEMA_ID, 'name': 'P',
            'nodes': [
                {'id': 's', 'type': 'control', 'kind': 'start'},
                {'id': 'big', 'type': 'subflow', 'role': 'general',
                 'params': {'ref': 'stored-1'}},
                {'id': 'e', 'type': 'control', 'kind': 'stop'}],
            'edges': [{'from': 's', 'to': 'big'}, {'from': 'big', 'to': 'e'}]}
        with self.assertRaises(ValueError):
            expand_subflows(d)

    def test_ref_resolver_used(self):
        d = {
            'schema': SCHEMA_ID, 'name': 'P',
            'nodes': [
                {'id': 's', 'type': 'control', 'kind': 'start'},
                {'id': 'big', 'type': 'subflow', 'role': 'general',
                 'params': {'ref': 'stored-1'}},
                {'id': 'e', 'type': 'control', 'kind': 'stop'}],
            'edges': [{'from': 's', 'to': 'big'}, {'from': 'big', 'to': 'e'}]}
        flat = expand_subflows(d, resolver=lambda r: self._child())
        ids = {n['id'] for n in flat['nodes']}
        self.assertIn('big/a', ids)


class ResolveScopeTest(unittest.TestCase):
    def test_default_is_inline(self):
        # Absent or invalid scope falls back to inline (back-compat).
        self.assertEqual(resolve_scope({'type': 'subflow'}), 'inline')
        self.assertEqual(
            resolve_scope({'type': 'subflow', 'params': {'scope': 'bogus'}}),
            'inline')

    def test_explicit_scope_wins(self):
        for val in VALID_SCOPES:
            self.assertEqual(
                resolve_scope({'type': 'subflow', 'params': {'scope': val}}),
                val)


class ScopeValidationTest(unittest.TestCase):
    def _wrap_subflow(self, params):
        child = {
            'schema': SCHEMA_ID, 'name': 'Child',
            'nodes': [
                {'id': 'cs', 'type': 'control', 'kind': 'start'},
                {'id': 'cw', 'type': 'role', 'role': 'coder'},
                {'id': 'ce', 'type': 'control', 'kind': 'stop'}],
            'edges': [{'from': 'cs', 'to': 'cw'}, {'from': 'cw', 'to': 'ce'}]}
        p = {'definition': child}
        p.update(params)
        return {
            'schema': SCHEMA_ID, 'name': 'Parent',
            'nodes': [
                {'id': 's', 'type': 'control', 'kind': 'start'},
                {'id': 'big', 'type': 'subflow', 'role': 'general', 'params': p},
                {'id': 'e', 'type': 'control', 'kind': 'stop'}],
            'edges': [{'from': 's', 'to': 'big'}, {'from': 'big', 'to': 'e'}]}

    def test_valid_scopes_pass(self):
        for val in ('inline', 'isolated'):
            v = validate_definition(self._wrap_subflow({'scope': val}))
            self.assertTrue(v['ok'], (val, v['errors']))

    def test_invalid_scope_is_error(self):
        v = validate_definition(self._wrap_subflow({'scope': 'sideways'}))
        self.assertFalse(v['ok'])
        self.assertTrue(any('scope' in e for e in v['errors']))


class IsolatedSubflowExpandTest(unittest.TestCase):
    """expand_subflows flattens inline subflows but leaves isolated ones
    intact (the engine runs them as a nested black box)."""

    def _child(self):
        return {
            'schema': SCHEMA_ID, 'name': 'Child',
            'nodes': [
                {'id': 'cs', 'type': 'control', 'kind': 'start'},
                {'id': 'a', 'type': 'role', 'role': 'researcher'},
                {'id': 'ce', 'type': 'control', 'kind': 'stop'}],
            'edges': [{'from': 'cs', 'to': 'a'}, {'from': 'a', 'to': 'ce'}]}

    def _parent(self, scope):
        return {
            'schema': SCHEMA_ID, 'name': 'Parent',
            'nodes': [
                {'id': 's', 'type': 'control', 'kind': 'start'},
                {'id': 'big', 'type': 'subflow', 'role': 'general',
                 'params': {'scope': scope, 'definition': self._child()}},
                {'id': 'e', 'type': 'control', 'kind': 'stop'}],
            'edges': [{'from': 's', 'to': 'big'}, {'from': 'big', 'to': 'e'}]}

    def test_isolated_subflow_not_flattened(self):
        flat = expand_subflows(self._parent('isolated'))
        ids = {n['id'] for n in flat['nodes']}
        # subflow node survives; inner nodes are NOT spliced into the parent.
        self.assertIn('big', ids)
        self.assertNotIn('big/a', ids)
        # the subflow's embedded child is preserved for the nested executor.
        big = [n for n in flat['nodes'] if n['id'] == 'big'][0]
        self.assertIn('definition', big['params'])

    def test_inline_subflow_flattened(self):
        flat = expand_subflows(self._parent('inline'))
        ids = {n['id'] for n in flat['nodes']}
        self.assertIn('big/a', ids)
        self.assertNotIn('big', ids)

    def test_isolated_alongside_inline(self):
        # A graph with BOTH: inline flattens, isolated stays.
        defn = {
            'schema': SCHEMA_ID, 'name': 'Mixed',
            'nodes': [
                {'id': 's', 'type': 'control', 'kind': 'start'},
                {'id': 'inl', 'type': 'subflow', 'role': 'general',
                 'params': {'scope': 'inline', 'definition': self._child()}},
                {'id': 'iso', 'type': 'subflow', 'role': 'general',
                 'params': {'scope': 'isolated', 'definition': self._child()}},
                {'id': 'e', 'type': 'control', 'kind': 'stop'}],
            'edges': [{'from': 's', 'to': 'inl'}, {'from': 'inl', 'to': 'iso'},
                      {'from': 'iso', 'to': 'e'}]}
        flat = expand_subflows(defn)
        ids = {n['id'] for n in flat['nodes']}
        self.assertIn('inl/a', ids)   # inline flattened
        self.assertNotIn('inl', ids)
        self.assertIn('iso', ids)     # isolated preserved
        self.assertNotIn('iso/a', ids)


class AutopilotDefinitionTest(unittest.TestCase):
    def test_builds_and_validates(self):
        d = build_autopilot_definition()
        v = validate_definition(d)
        self.assertTrue(v['ok'], v['errors'])
        self.assertEqual(d['schema'], SCHEMA_ID)
        ids = [n['id'] for n in d['nodes']]
        self.assertIn('worker', ids)
        self.assertIn('vu', ids)

    def test_vu_emits_user_worker_emits_assistant(self):
        d = build_autopilot_definition()
        by_id = {n['id']: n for n in d['nodes']}
        self.assertEqual(resolve_emits(by_id['vu']), 'user')
        self.assertEqual(resolve_emits(by_id['worker']), 'assistant')


class VirtualUserVerdictTest(unittest.TestCase):
    """The VU inverts critic semantics: keep going unless explicitly done."""

    def _exec(self):
        from lib.orchestration_engine import FlowExecutor
        return FlowExecutor(build_autopilot_definition(),
                            agent_runner=lambda *a, **k: {'output': '', 'status': 'completed'})

    def test_plain_vu_reply_continues(self):
        fx = self._exec()
        phase, _ = fx._classify_verdict('Sounds good, keep going.',
                                        verifier_role='virtual_user')
        self.assertEqual(phase, 'worker')

    def test_empty_vu_reply_continues(self):
        fx = self._exec()
        phase, _ = fx._classify_verdict('', verifier_role='virtual_user')
        self.assertEqual(phase, 'worker')

    def test_vu_done_sentinel_stops(self):
        fx = self._exec()
        phase, _ = fx._classify_verdict('All set. [VU: TASK_DONE]',
                                        verifier_role='virtual_user')
        self.assertEqual(phase, 'stop')

    def test_vu_stop_verdict_stops(self):
        fx = self._exec()
        phase, _ = fx._classify_verdict('[VERDICT: STOP]',
                                        verifier_role='virtual_user')
        self.assertEqual(phase, 'stop')

    def test_critic_ambiguous_still_stops(self):
        # Sanity: non-VU verifier semantics unchanged (ambiguous → stop).
        fx = self._exec()
        phase, _ = fx._classify_verdict('Looks reasonable.',
                                        verifier_role='critic')
        self.assertEqual(phase, 'stop')


if __name__ == '__main__':
    unittest.main()
