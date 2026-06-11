"""tests/test_access_matrix.py — Per-(key, model) capability matrix.

Verifies that ``LLMDispatcher._build_slots_from_providers`` honors the
``model.key_access`` override map:

* absent key_access  → every key gets the model (legacy behavior)
* enabled:false cell → that one (key, model) slot is dropped, others stay
* per-cell rpm       → only that key's slot picks up the override
* per-cell aliases   → alias slots are created only for that key
* per-cell caps      → only that key's slot gets the overridden capabilities
* model enabled:false → no slots for any key (global kill switch)
"""

import unittest


def _build(providers):
    """Build a throwaway dispatcher's slots from a provider list."""
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    d = LLMDispatcher()
    d.slots = []
    d._build_slots_from_providers(providers)
    return d.slots


def _provider(models):
    return [{
        'id': 'mt',
        'base_url': 'https://gw.example.com/v1',
        'api_keys': ['sk-aaa', 'sk-bbb'],
        'enabled': True,
        'models': models,
    }]


class AccessMatrixTest(unittest.TestCase):

    def test_no_key_access_is_legacy_product(self):
        slots = _build(_provider([
            {'model_id': 'modelX', 'capabilities': ['text'], 'rpm': 40},
        ]))
        modelx = [s for s in slots if s.model == 'modelX']
        # 2 keys × 1 model = 2 slots
        self.assertEqual(len(modelx), 2)
        self.assertEqual({s.rpm_limit for s in modelx}, {40})

    def test_cell_disabled_drops_only_that_pair(self):
        slots = _build(_provider([
            {'model_id': 'modelX', 'capabilities': ['text'], 'rpm': 40,
             'key_access': {'1': {'enabled': False}}},
        ]))
        modelx = [s for s in slots if s.model == 'modelX']
        # key #1 disabled → only key #0 remains
        self.assertEqual(len(modelx), 1)
        self.assertEqual(modelx[0].key_name, 'mt_key_0')

    def test_model_globally_disabled_drops_all(self):
        slots = _build(_provider([
            {'model_id': 'modelX', 'capabilities': ['text'], 'enabled': False,
             'key_access': {'0': {'enabled': True}}},
        ]))
        self.assertEqual([s for s in slots if s.model == 'modelX'], [])

    def test_per_cell_rpm_override(self):
        slots = _build(_provider([
            {'model_id': 'modelX', 'capabilities': ['text'], 'rpm': 40,
             'key_access': {'0': {'rpm': 5}}},
        ]))
        by_key = {s.key_name: s for s in slots if s.model == 'modelX'}
        self.assertEqual(by_key['mt_key_0'].rpm_limit, 5)
        self.assertEqual(by_key['mt_key_1'].rpm_limit, 40)

    def test_per_cell_aliases_only_for_that_key(self):
        slots = _build(_provider([
            {'model_id': 'modelX', 'capabilities': ['text'], 'rpm': 40,
             'aliases': [],
             'key_access': {'0': {'aliases': ['modelX-mirror']}}},
        ]))
        mirror = [s for s in slots if s.model == 'modelX-mirror']
        # alias slot exists only for key #0
        self.assertEqual(len(mirror), 1)
        self.assertEqual(mirror[0].key_name, 'mt_key_0')

    def test_per_cell_capabilities_override(self):
        slots = _build(_provider([
            {'model_id': 'modelX', 'capabilities': ['text', 'vision'], 'rpm': 40,
             'key_access': {'1': {'capabilities': ['text']}}},
        ]))
        by_key = {s.key_name: s for s in slots if s.model == 'modelX'}
        self.assertIn('vision', by_key['mt_key_0'].capabilities)
        self.assertNotIn('vision', by_key['mt_key_1'].capabilities)

    def test_disabled_ids_drops_only_that_alias_for_that_key(self):
        # Aliases are distinct models: disabling one alias on one key must
        # leave the root + other aliases reachable on that key, and leave the
        # alias reachable on the other key.
        slots = _build(_provider([
            {'model_id': 'modelX', 'capabilities': ['text'], 'rpm': 40,
             'aliases': ['mx-fast', 'mx-pro'],
             'key_access': {'0': {'disabled_ids': ['mx-fast']}}},
        ]))
        served = {(s.key_name, s.model) for s in slots}
        # key#0 keeps root + mx-pro, loses mx-fast
        self.assertIn(('mt_key_0', 'modelX'), served)
        self.assertIn(('mt_key_0', 'mx-pro'), served)
        self.assertNotIn(('mt_key_0', 'mx-fast'), served)
        # key#1 keeps everything
        self.assertIn(('mt_key_1', 'mx-fast'), served)
        self.assertIn(('mt_key_1', 'mx-pro'), served)

    def test_disabled_ids_can_drop_root_keeping_aliases(self):
        slots = _build(_provider([
            {'model_id': 'modelX', 'capabilities': ['text'], 'rpm': 40,
             'aliases': ['mx-fast'],
             'key_access': {'0': {'disabled_ids': ['modelX']}}},
        ]))
        served = {(s.key_name, s.model) for s in slots}
        self.assertNotIn(('mt_key_0', 'modelX'), served)
        self.assertIn(('mt_key_0', 'mx-fast'), served)
        self.assertIn(('mt_key_1', 'modelX'), served)


def _dispatcher(providers):
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    d = LLMDispatcher()
    d.slots = []
    d._build_slots_from_providers(providers)
    return d


class AliasRoutingTest(unittest.TestCase):
    """Config-declared aliases are first-class routing targets.

    Requesting any member of an alias group must be able to pick a slot
    belonging to any other member — even when only one key serves the root
    id and another key serves only an alias.
    """

    def test_config_alias_routes_to_root_request(self):
        # Root disabled on key#1, alias kept → requesting the ROOT id must be
        # able to land on key#1's alias slot.
        d = _dispatcher(_provider([
            {'model_id': 'modelX', 'capabilities': ['text'], 'rpm': 40,
             'aliases': ['mx-mirror'],
             'key_access': {'1': {'disabled_ids': ['modelX']}}},
        ]))
        alias_set = d._alias_set('modelX')
        self.assertIn('mx-mirror', alias_set)
        self.assertIn('modelX', alias_set)
        # Force key#0's root slot into cooldown so the only pick left is the
        # key#1 alias slot; strict_model must still find it.
        for s in d.slots:
            if s.key_name == 'mt_key_0':
                s.cooldown_until = __import__('time').time() + 1000
        d._initialized = True
        chosen = d.pick_slot(prefer_model='modelX', strict_model=True)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.model, 'mx-mirror')
        self.assertEqual(chosen.key_name, 'mt_key_1')

    def test_alias_index_merges_with_static_groups(self):
        # A config entry that names the gateway id 'aws.claude-opus-4.8' must
        # transitively pull in the static-group members (claude-opus-4-8, the
        # Bedrock id) AND the config-only alias.
        d = _dispatcher(_provider([
            {'model_id': 'aws.claude-opus-4.8', 'capabilities': ['text'],
             'aliases': ['yuju-claude-opus-4.8-evaDaily']},
        ]))
        group = d._alias_set('aws.claude-opus-4.8')
        self.assertIn('yuju-claude-opus-4.8-evaDaily', group)
        self.assertIn('claude-opus-4-8', group)          # from static group
        # And the reverse lookup from the config-only alias resolves the same.
        self.assertEqual(d._alias_set('yuju-claude-opus-4.8-evaDaily'), group)

    def test_model_without_aliases_resolves_to_itself(self):
        d = _dispatcher(_provider([
            {'model_id': 'soloModel', 'capabilities': ['text']},
        ]))
        self.assertEqual(d._alias_set('soloModel'), {'soloModel'})


if __name__ == '__main__':
    unittest.main()
