"""tests/test_provider_pin.py — Hard thread-scoped provider isolation.

Reproduces the 429 / no-fallback cross-tenant leak and verifies the fix:
an inline-provider / BYO ephemeral slot, once pinned on a thread, is the
ONLY slot the dispatcher may pick — even when an operator-curated slot
for the same model name has a far better score().

See lib/llm_dispatch/provider_pin.py and the inline-provider isolation
memory for the full rationale.
"""

import os
import unittest


class ProviderPinTest(unittest.TestCase):

    def setUp(self):
        os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = '0'
        from lib.llm_dispatch.factory import get_dispatcher
        from lib.llm_dispatch.slot import Slot
        from lib.llm_dispatch.provider_pin import clear_pinned_provider

        clear_pinned_provider()
        self.d = get_dispatcher()
        self.d.initialize()
        # Inject a fake OPERATOR slot for a popular alias model with a great
        # (low) score — this is the Meituan-keyed slot that the leak routes
        # onto. We give it tiny latency + zero inflight so it ALWAYS wins a
        # naive min(score) race.
        self._operator = Slot(
            key_name='operator_key', api_key='sk-operator',
            model='deepseek-v4-pro', capabilities={'text', 'cheap'},
            base_url='https://gateway.internal/v1',
            provider_id='sankuai', latency_ema=10.0,
        )
        with self.d._lock:
            self.d.slots.append(self._operator)
        self._eph = None

    def tearDown(self):
        from lib.llm_dispatch.provider_pin import clear_pinned_provider
        clear_pinned_provider()
        with self.d._lock:
            if self._operator in self.d.slots:
                self.d.slots.remove(self._operator)
        if self._eph is not None:
            from lib.llm_dispatch.ephemeral import dispose_ephemeral_slot
            dispose_ephemeral_slot(self._eph)
        os.environ.pop('TOFU_EPHEMERAL_PREFLIGHT', None)

    def _mint_ephemeral(self):
        from lib.llm_dispatch.ephemeral import mint_ephemeral_slot
        # Slower latency than the operator slot so, WITHOUT a pin, the
        # operator slot would win the score race.
        self._eph = mint_ephemeral_slot(
            base_url='http://33.0.0.1:8080/v1', api_key='x',
            model_id='deepseek-v4-pro', owner='arm-42',
        )
        return self._eph

    def test_unpinned_can_pick_operator_slot(self):
        # Baseline: with no pin, the operator slot (better score) is eligible.
        h = self._mint_ephemeral()
        chosen = self.d.pick_slot(capability='text',
                                  prefer_model='deepseek-v4-pro')
        self.assertIsNotNone(chosen)
        # The faster operator slot should win the naive race — this is
        # exactly the leak the pin closes.
        self.assertEqual(chosen.provider_id, 'sankuai')

    def test_pinned_only_picks_its_own_provider(self):
        from lib.llm_dispatch.provider_pin import provider_pin
        h = self._mint_ephemeral()
        with provider_pin(h.slot.provider_id):
            chosen = self.d.pick_slot(capability='text',
                                      prefer_model='deepseek-v4-pro')
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.provider_id, h.slot.provider_id)
        self.assertEqual(chosen.base_url, 'http://33.0.0.1:8080/v1')

    def test_pinned_cheap_capability_still_isolated(self):
        # The L2 compaction summary dispatches capability='cheap' with no
        # prefer_model. The ephemeral slot is seeded {'text'} only, so this
        # exercises the capability-fallback branch of _pick — which must
        # ALSO respect the pin (the worst silent leak: compaction on the
        # operator's cheap key every turn).
        from lib.llm_dispatch.provider_pin import provider_pin
        h = self._mint_ephemeral()  # caps default to {'text'}
        with provider_pin(h.slot.provider_id):
            chosen = self.d.pick_slot(capability='cheap')
        # Either it finds the pinned text slot via the fallback branch, or
        # returns None — but it must NEVER return the operator slot.
        if chosen is not None:
            self.assertEqual(chosen.provider_id, h.slot.provider_id)

    def test_pinned_to_absent_provider_returns_none(self):
        # Pinned to a provider with no live slot → wait/None, NEVER widen
        # to the operator key.
        from lib.llm_dispatch.provider_pin import provider_pin
        with provider_pin('ephemeral:does-not-exist'):
            chosen = self.d.pick_slot(capability='text',
                                      prefer_model='deepseek-v4-pro')
        self.assertIsNone(chosen)

    def test_has_capable_slots_respects_pin(self):
        from lib.llm_dispatch.provider_pin import provider_pin
        # Operator slot exists, but a pin to a non-existent provider means
        # the retry loop must see NO capable slots (so it gives up cleanly
        # rather than spinning toward the operator key).
        with provider_pin('ephemeral:nope'):
            self.assertFalse(
                self.d.has_capable_slots('text'))

    def test_ephemeral_provider_id_unique_per_handle(self):
        # Two mints from the same owner must get DISTINCT provider_ids so
        # concurrent same-key requests don't share a pin.
        from lib.llm_dispatch.ephemeral import (
            dispose_ephemeral_slot, mint_ephemeral_slot)
        h1 = mint_ephemeral_slot(base_url='http://33.0.0.1:8080/v1',
                                 api_key='x', model_id='m', owner='same')
        h2 = mint_ephemeral_slot(base_url='http://33.0.0.2:8080/v1',
                                 api_key='x', model_id='m', owner='same')
        try:
            self.assertNotEqual(h1.slot.provider_id, h2.slot.provider_id)
        finally:
            dispose_ephemeral_slot(h1)
            dispose_ephemeral_slot(h2)

    def test_context_manager_restores_previous_pin(self):
        from lib.llm_dispatch.provider_pin import (
            get_pinned_provider, provider_pin)
        self.assertIsNone(get_pinned_provider())
        with provider_pin('ephemeral:outer'):
            self.assertEqual(get_pinned_provider(), 'ephemeral:outer')
            with provider_pin('ephemeral:inner'):
                self.assertEqual(get_pinned_provider(), 'ephemeral:inner')
            self.assertEqual(get_pinned_provider(), 'ephemeral:outer')
        self.assertIsNone(get_pinned_provider())


if __name__ == '__main__':
    unittest.main()
