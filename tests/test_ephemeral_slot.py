"""tests/test_ephemeral_slot.py — Ephemeral Slot lifecycle.

Covers:
* mint adds a Slot to the dispatcher pool with the right base_url + key
* dispose removes it again; double-dispose is idempotent
* count_ephemeral_slots() reflects the live count
* invalid args are rejected before touching the pool
* private hosts trigger no_proxy registration
"""

import os
import unittest


class EphemeralSlotTest(unittest.TestCase):

    def setUp(self):
        # The lifecycle tests mint slots to fake private IPs that aren't
        # listening. The mint-time reachability probe (added 2026-06) would
        # reject those, so disable it here — these tests exercise pool
        # mechanics, not reachability. A dedicated test below re-enables it.
        self._prev_preflight = os.environ.get('TOFU_EPHEMERAL_PREFLIGHT')
        os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = '0'

    def tearDown(self):
        if self._prev_preflight is None:
            os.environ.pop('TOFU_EPHEMERAL_PREFLIGHT', None)
        else:
            os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = self._prev_preflight

    def _initial_count(self):
        from lib.llm_dispatch.factory import get_dispatcher
        d = get_dispatcher()
        d.initialize()
        return len(d.slots)

    def test_mint_and_dispose(self):
        from lib.llm_dispatch.ephemeral import (
            count_ephemeral_slots, dispose_ephemeral_slot,
            mint_ephemeral_slot,
        )
        from lib.llm_dispatch.factory import get_dispatcher

        n_before = self._initial_count()
        eph_before = count_ephemeral_slots()

        h = mint_ephemeral_slot(
            base_url='http://10.0.0.5:8080/v1',
            api_key='sk-test',
            model_id='deepseek-v4-pro',
            owner='test-task',
        )
        self.assertEqual(count_ephemeral_slots(), eph_before + 1)
        d = get_dispatcher()
        with d._lock:
            self.assertEqual(len(d.slots), n_before + 1)
            slot = next(s for s in d.slots if s.key_name == h.slot.key_name)
        self.assertEqual(slot.base_url, 'http://10.0.0.5:8080/v1')
        self.assertEqual(slot.api_key, 'sk-test')
        self.assertEqual(slot.model, 'deepseek-v4-pro')
        # provider_id includes the owner tag so logs disambiguate
        self.assertIn('test-task', slot.provider_id)

        # Dispose
        self.assertTrue(dispose_ephemeral_slot(h))
        self.assertEqual(count_ephemeral_slots(), eph_before)
        with d._lock:
            self.assertEqual(len(d.slots), n_before)

        # Double-dispose is a no-op
        self.assertFalse(dispose_ephemeral_slot(h))

    def test_invalid_base_url_rejected(self):
        from lib.llm_dispatch.ephemeral import mint_ephemeral_slot
        with self.assertRaises(ValueError):
            mint_ephemeral_slot(base_url='ftp://nope', api_key='',
                                  model_id='m')
        with self.assertRaises(ValueError):
            mint_ephemeral_slot(base_url='', api_key='', model_id='m')

    def test_invalid_model_rejected(self):
        from lib.llm_dispatch.ephemeral import mint_ephemeral_slot
        with self.assertRaises(ValueError):
            mint_ephemeral_slot(base_url='http://h:8080/v1',
                                  api_key='', model_id='')

    def test_normalises_trailing_slash(self):
        from lib.llm_dispatch.ephemeral import (
            dispose_ephemeral_slot, mint_ephemeral_slot,
        )
        h = mint_ephemeral_slot(
            base_url='http://10.0.0.5:8080/v1/',  # trailing slash
            api_key='', model_id='m1',
            owner='trailing-slash-test',
        )
        try:
            self.assertEqual(h.slot.base_url, 'http://10.0.0.5:8080/v1')
        finally:
            dispose_ephemeral_slot(h)

    def test_private_host_registers_no_proxy(self):
        from lib import proxy
        from lib.llm_dispatch.ephemeral import (
            dispose_ephemeral_slot, mint_ephemeral_slot,
        )
        h = mint_ephemeral_slot(
            base_url='http://10.0.0.99:8080/v1',  # RFC1918 private
            api_key='', model_id='m1',
            owner='no-proxy-test',
        )
        try:
            # The host should be in the registered set after mint.
            self.assertIn('10.0.0.99', proxy._registered_hosts)
        finally:
            dispose_ephemeral_slot(h)

    def test_preflight_rejects_unreachable_self_hosted(self):
        # With the probe ENABLED (override setUp), a dead self-hosted /
        # raw-IP endpoint must be rejected at mint with a ValueError so
        # callers surface a clean 400 instead of stalling on the first
        # request's connect timeout.
        os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = '1'
        os.environ['TOFU_EPHEMERAL_PREFLIGHT_TIMEOUT'] = '1'
        from lib.llm_dispatch.ephemeral import (
            count_ephemeral_slots, mint_ephemeral_slot,
        )
        n_before = count_ephemeral_slots()
        # 127.0.0.1:1 — loopback, port 1 is never listening → fast refuse.
        with self.assertRaises(ValueError):
            mint_ephemeral_slot(
                base_url='http://127.0.0.1:1/v1', api_key='',
                model_id='m1', owner='preflight-test')
        # No slot should have leaked into the pool on rejection.
        self.assertEqual(count_ephemeral_slots(), n_before)


class PreflightModeTest(unittest.TestCase):
    """Pre-flight probe precedence: explicit env > benchmark auto-skip > on.

    A dead self-hosted endpoint must NOT hard-400 a whole benchmark arm
    just because a TCP handshake didn't land — in TOFU_DISABLE_CONFIGURED_SLOTS
    mode the ephemeral slot is the only dispatch route, so reachability is
    the dispatch retry loop's job, not the mint's.
    """

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (
            'TOFU_EPHEMERAL_PREFLIGHT', 'TOFU_DISABLE_CONFIGURED_SLOTS',
            'TOFU_EPHEMERAL_PREFLIGHT_TIMEOUT')}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_normal_mode_probes(self):
        from lib.llm_dispatch.ephemeral import _preflight_enabled
        self.assertTrue(_preflight_enabled())

    def test_benchmark_mode_auto_skips(self):
        from lib.llm_dispatch.ephemeral import _preflight_enabled
        os.environ['TOFU_DISABLE_CONFIGURED_SLOTS'] = '1'
        self.assertFalse(_preflight_enabled())

    def test_explicit_on_overrides_benchmark_mode(self):
        # An operator who explicitly asks for the probe gets it, even in
        # benchmark mode.
        from lib.llm_dispatch.ephemeral import _preflight_enabled
        os.environ['TOFU_DISABLE_CONFIGURED_SLOTS'] = '1'
        os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = '1'
        self.assertTrue(_preflight_enabled())

    def test_explicit_off_in_normal_mode(self):
        from lib.llm_dispatch.ephemeral import _preflight_enabled
        os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = '0'
        self.assertFalse(_preflight_enabled())

    def test_benchmark_mode_mint_skips_probe_for_dead_endpoint(self):
        # The whole point: in benchmark mode, minting against a dead
        # self-hosted endpoint must SUCCEED (slot added), not raise the
        # "endpoint unreachable" ValueError.
        os.environ['TOFU_DISABLE_CONFIGURED_SLOTS'] = '1'
        os.environ['TOFU_EPHEMERAL_PREFLIGHT_TIMEOUT'] = '1'
        from lib.llm_dispatch.ephemeral import (
            dispose_ephemeral_slot, mint_ephemeral_slot)
        # 127.0.0.1:1 — loopback, port 1 never listening. With the probe
        # active this raises; in benchmark mode it must be skipped.
        h = mint_ephemeral_slot(
            base_url='http://127.0.0.1:1/v1', api_key='',
            model_id='m1', owner='benchmark-arm')
        try:
            self.assertIsNotNone(h)
            self.assertEqual(h.slot.base_url, 'http://127.0.0.1:1/v1')
        finally:
            dispose_ephemeral_slot(h)


if __name__ == '__main__':
    unittest.main()
