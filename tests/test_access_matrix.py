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
    """model_id-only routing (owner directive 2026-08-06).

    An entry's aliases are WIRE spellings of THAT entry: they resolve a
    stored name back to the entry's model_id, and an alias slot still
    serves requests for its own root — but two entries (still less two
    providers) are NEVER unioned into one routing group.
    """

    def test_config_alias_slot_still_serves_own_root(self):
        # Root disabled on key#1, alias kept → requesting the ROOT id must be
        # able to land on key#1's alias slot (intra-entry rotation lives).
        d = _dispatcher(_provider([
            {'model_id': 'modelX', 'capabilities': ['text'], 'rpm': 40,
             'aliases': ['mx-mirror'],
             'key_access': {'1': {'disabled_ids': ['modelX']}}},
        ]))
        self.assertEqual(d._route_logical('modelX'), 'modelX')
        self.assertEqual(d._route_logical('mx-mirror'), 'modelX')
        mirror = [s for s in d.slots if s.model == 'mx-mirror']
        self.assertTrue(all(s.logical_model == 'modelX' for s in mirror))
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

    def test_alias_never_hijacks_another_providers_model(self):
        # The 2026-08-06 incident repro: provider B's entry carries an alias
        # that IS provider A's model_id. Old union-find merged them into one
        # routing group, so requesting B's logical id silently landed on A.
        providers = [
            {'id': 'gw', 'base_url': 'https://gw.example.com/v1',
             'api_keys': ['sk-gw'], 'enabled': True,
             'models': [{'model_id': 'shared-name', 'capabilities': ['text'],
                         'rpm': 40}]},
            {'id': 'official', 'base_url': 'https://api.example.com/v1',
             'api_keys': ['sk-off'], 'enabled': True,
             'models': [{'model_id': 'official-only',
                         'capabilities': ['text'], 'rpm': 40,
                         'aliases': ['shared-name']}]},
        ]
        d = _dispatcher(providers)
        d._initialized = True
        # The exact model_id owns its name; the alias claim loses.
        self.assertEqual(d._route_logical('shared-name'), 'shared-name')
        self.assertEqual(d._route_logical('official-only'), 'official-only')
        # Requesting B's logical id must NEVER route to provider A.
        for _ in range(5):
            chosen = d.pick_slot(prefer_model='official-only', strict_model=True)
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen.provider_id, 'official')
            chosen.release()
        # Requesting the shared name routes ONLY to its exact owner.
        for _ in range(5):
            chosen = d.pick_slot(prefer_model='shared-name', strict_model=True)
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen.provider_id, 'gw')
            chosen.release()

    def test_static_group_member_resolves_only_to_unique_configured_home(self):
        # A stored conv naming a static-group spelling (Bedrock id) keeps
        # routing when EXACTLY ONE group member is configured — but the
        # static group never widens live routing beyond that one model_id.
        d = _dispatcher(_provider([
            {'model_id': 'aws.claude-opus-4.8', 'capabilities': ['text'],
             'aliases': ['yuju-claude-opus-4.8-evaDaily']},
        ]))
        self.assertEqual(d._route_logical('aws.claude-opus-4.8'),
                         'aws.claude-opus-4.8')
        self.assertEqual(d._route_logical('yuju-claude-opus-4.8-evaDaily'),
                         'aws.claude-opus-4.8')
        # Static-only member (not in the entry's own wire pool) resolves to
        # the one configured group member.
        self.assertEqual(d._route_logical('claude-opus-4-8'),
                         'aws.claude-opus-4.8')

    def test_model_without_aliases_resolves_to_itself(self):
        d = _dispatcher(_provider([
            {'model_id': 'soloModel', 'capabilities': ['text']},
        ]))
        self.assertEqual(d._route_logical('soloModel'), 'soloModel')
        self.assertIsNone(d._route_logical('never-configured'))


if __name__ == '__main__':
    unittest.main()
