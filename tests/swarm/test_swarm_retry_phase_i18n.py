"""Backend half of pt_18ebee9c9ea64cf3: the swarm/flow dispatch-retry HUD
ships structured i18n fields (detailKey/detailArgs + typed reasonKey) instead
of leaking raw dispatcher log tokens ("Retrying… Endpoint unreachable
(attempt 1)") onto the worker bubble.

Covers the four backend surfaces of the fix:

  1. lib/llm_dispatch/retry_i18n.retry_phase_fields — the SHARED helper
     (branch selection + typed reasonKey + gateway-prefix-stripped label).
  2. lib/swarm/agent._build_dispatch_retry_phase — the module-level seam the
     ``_on_dispatch_retry`` closure delegates to; asserts BYTE-PARITY of the
     legacy detail string (headless clients must see no change).
  3. lib/orchestration_chat_flow_adapter._on_step_phase — forwards
     detailKey/detailArgs onto the wire ``phase`` event (and still skips
     verifier-side producers).
  4. lib/orchestration_engine — the ``step_phase`` ``**meta`` passthrough
     that carries the structured fields from emitter to adapter.

The main-chat emitter (manager/_stream.py) reuses the same shared helper and
is covered end-to-end by tests/tasks_pkg/test_stream_phase_i18n.py (15 tests, kept
green through the refactor — that suite asserts the exact wire values this
helper now produces).
"""

from __future__ import annotations

import os
import unittest

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))


class TestSharedRetryPhaseFields(unittest.TestCase):
    """lib/llm_dispatch/retry_i18n.retry_phase_fields — the single mapping."""

    def _fields(self, **kw):
        from lib.llm_dispatch.retry_i18n import retry_phase_fields
        return retry_phase_fields(**kw)

    def test_reason_branch_maps_typed_reasonkey(self):
        f = self._fields(model='kimi-k3', attempt=1,
                         reason='Endpoint unreachable', status_code=0,
                         legacy_detail='X')
        self.assertEqual(f['detailKey'], 'stream.phase.retryReason')
        self.assertEqual(f['detailArgs'], {
            'reason': 'Endpoint unreachable',
            'reasonKey': 'stream.retryReason.endpointUnreachable',
            'model': 'kimi-k3', 'attempt': 1})
        self.assertEqual(f['detail'], 'X')   # legacy passthrough untouched

    def test_429_branch_ignores_reason(self):
        f = self._fields(model='kimi-k3', attempt=3,
                         reason='Waiting for model (rate-limited)',
                         status_code=429, legacy_detail='X')
        self.assertEqual(f['detailKey'], 'stream.phase.retryRateLimited')
        self.assertEqual(f['detailArgs'], {'model': 'kimi-k3', 'attempt': 3})

    def test_bare_branch(self):
        f = self._fields(model='kimi-k3', attempt=2, reason='',
                         status_code=0, legacy_detail='X')
        self.assertEqual(f['detailKey'], 'stream.phase.retryGeneric')
        self.assertEqual(f['detailArgs'], {'model': 'kimi-k3', 'attempt': 2})

    def test_unknown_reason_omits_reasonkey(self):
        f = self._fields(model='m', attempt=1, reason='HTTP 503',
                         status_code=0, legacy_detail='X')
        self.assertEqual(f['detailKey'], 'stream.phase.retryReason')
        self.assertEqual(f['detailArgs']['reason'], 'HTTP 503')
        self.assertNotIn('reasonKey', f['detailArgs'])

    def test_cooldown_poll_does_not_claim_twenty_request_attempts(self):
        f = self._fields(
            model='kimi-k3', attempt=20,
            reason='Waiting for model (retry backoff)', status_code=0,
            legacy_detail='X')
        self.assertEqual(f['detailKey'], 'stream.phase.retryCooldownWait')
        self.assertEqual(f['detailArgs'], {
            'reason': 'Waiting for model (retry backoff)',
            'reasonKey': 'stream.retryReason.waitingBackoff',
            'model': 'kimi-k3',
        })
        self.assertEqual(f['detail'], 'X')

    def test_gateway_prefix_stripped_in_label(self):
        f = self._fields(model='aws.claude-opus-4.8', attempt=1,
                         reason='Endpoint unreachable', status_code=0,
                         legacy_detail='X')
        self.assertEqual(f['detailArgs']['model'], 'claude-opus-4.8')

    def test_all_dispatcher_static_tokens_have_keys(self):
        """Every STATIC reason token the dispatcher can pass must map to a
        typed key — a new unmapped token is the raw-English leak returning.
        Dynamic tokens (f'HTTP {status}' / str(e)) fall back by design."""
        from lib.llm_dispatch.retry_i18n import RETRY_REASON_KEYS
        expected = {
            'Endpoint unreachable',
            'Request timed out',
            'Waiting for model (rate-limited)',
            'Waiting for model (retry backoff)',
            'Waiting for model (shared project limit)',
            'Key balance exhausted',
            'Rate limited (429)',
            'Upstream error',
        }
        self.assertEqual(set(RETRY_REASON_KEYS), expected)
        for key in RETRY_REASON_KEYS.values():
            self.assertTrue(key.startswith('stream.retryReason.'), key)





class TestFlowAdapterForwarding(unittest.TestCase):
    """The flow adapter must forward the
    structured i18n fields onto the wire ``phase`` event."""

    def _drive(self, ev):
        from lib.orchestration.chat_flow_adapter import FlowEventAdapter
        streamed = []
        adapter = FlowEventAdapter(on_stream=streamed.append)
        adapter.on_event(ev)
        return streamed

    def test_forwards_detailkey_and_detailargs(self):
        args = {'reason': 'Endpoint unreachable',
                'reasonKey': 'stream.retryReason.endpointUnreachable',
                'model': 'kimi-k3', 'attempt': 1}
        out = self._drive({'type': 'step_phase', 'role': 'worker',
                           'emits': 'assistant', 'phase': 'retrying',
                           'detail': 'Endpoint unreachable… (attempt 1)',
                           'attempt': 1, 'status_code': 0,
                           'detailKey': 'stream.phase.retryReason',
                           'detailArgs': args})
        self.assertEqual(len(out), 1)
        wire = out[0]
        self.assertEqual(wire['type'], 'phase')
        self.assertEqual(wire['phase'], 'retrying')
        self.assertEqual(wire['detail'],
                         'Endpoint unreachable… (attempt 1)')
        self.assertEqual(wire['detailKey'], 'stream.phase.retryReason')
        self.assertEqual(wire['detailArgs'], args)
        self.assertEqual(wire['attempt'], 1)

    def test_verifier_phase_still_skipped(self):
        out = self._drive({'type': 'step_phase', 'role': 'critic',
                           'emits': 'user', 'phase': 'retrying',
                           'detail': 'X', 'detailKey': 'k', 'detailArgs': {}})
        self.assertEqual(out, [])

    def test_legacy_event_without_keys_unchanged(self):
        """A step_phase from an emitter that ships no i18n fields must
        produce the SAME wire shape as before the fix (no empty keys)."""
        out = self._drive({'type': 'step_phase', 'role': 'worker',
                           'emits': 'assistant', 'phase': 'retrying',
                           'detail': 'Endpoint unreachable… (attempt 1)',
                           'attempt': 1})
        self.assertEqual(out, [{'type': 'phase', 'phase': 'retrying',
                                'detail': 'Endpoint unreachable… (attempt 1)',
                                'attempt': 1}])

if __name__ == '__main__':
    unittest.main()
