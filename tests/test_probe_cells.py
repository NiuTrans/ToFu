"""tests/test_probe_cells.py — Access-matrix cell-probe classification.

Covers ``routes.config._probe_one_cell`` — the per-(key, model) reachability
test behind the matrix's "Probe & Recommend" button. We patch the HTTP layer
so no network is touched and assert each HTTP status maps to the right verdict.
"""

import unittest
from unittest import mock


class _FakeResp:
    def __init__(self, status_code, text=''):
        self.status_code = status_code
        self.text = text


class ProbeCellClassificationTest(unittest.TestCase):

    def _probe(self, status_code, text=''):
        import routes.config as cfg
        with mock.patch('lib.http_client.http_post',
                        return_value=_FakeResp(status_code, text)):
            return cfg._probe_one_cell('https://gw.example.com/v1', 'sk-x',
                                       'modelX', {}, 5)

    def test_200_is_ok(self):
        self.assertEqual(self._probe(200, 'hi')[0], 'ok')

    def test_400_is_ok_routing_reached(self):
        # 400 means the gateway accepted (key, model) routing, just rejected
        # the tiny payload shape — the pair is reachable.
        self.assertEqual(self._probe(400, 'bad request')[0], 'ok')

    def test_400_chinese_route_missing_is_not_found(self):
        # 2026-08-04 incident: the AIGC gateway answers a nonexistent route
        # (vertex.claude-opus-5) with HTTP 400 + Chinese validation body —
        # it MUST NOT read as reachable (it did → false-green matrix).
        body = ('{"status":400,"message":"不支持的模型类型'
                '(model=vertex.claude-opus-5)","data":null,"ext":{"error":'
                '{"source":"AIGC","service":"aigc","stage":"validation"}}}')
        self.assertEqual(self._probe(400, body)[0], 'not_found')

    def test_400_english_unsupported_model_is_not_found(self):
        self.assertEqual(
            self._probe(400, '{"error": "unsupported model type"}')[0],
            'not_found')

    def test_429_is_rate_limited(self):
        self.assertEqual(self._probe(429, 'too many requests')[0], 'rate_limited')

    def test_402_is_rate_limited(self):
        self.assertEqual(self._probe(402, 'payment required')[0], 'rate_limited')

    def test_401_is_unauthorized(self):
        self.assertEqual(self._probe(401, 'no')[0], 'unauthorized')

    def test_403_is_unauthorized(self):
        self.assertEqual(self._probe(403, 'forbidden')[0], 'unauthorized')

    def test_404_is_not_found(self):
        self.assertEqual(self._probe(404, 'nope')[0], 'not_found')

    def test_model_not_found_body_is_not_found(self):
        # Some gateways return 200/400-ish codes with a model_not_found body.
        self.assertEqual(self._probe(404, 'the model does not exist')[0], 'not_found')

    def test_503_is_unavailable(self):
        self.assertEqual(self._probe(503, 'service unavailable')[0], 'unavailable')

    def test_network_error_is_unavailable(self):
        import routes.config as cfg
        with mock.patch('lib.http_client.http_post', side_effect=OSError('boom')):
            status, _ = cfg._probe_one_cell('https://gw.example.com/v1', 'sk-x',
                                            'modelX', {}, 5)
        self.assertEqual(status, 'unavailable')

    def test_unknown_code_is_error(self):
        self.assertEqual(self._probe(418, "teapot")[0], 'error')


class ProbeMultiAttemptTest(unittest.TestCase):
    """`_probe_cell_multi` filters out FALSE 429s via retries."""

    def setUp(self):
        # The cell-probe engine moved to lib/provider_probe.py (2026-06).
        # probe_cell_multi calls probe_one_cell THROUGH that module's global,
        # so patches must target lib.provider_probe, not the routes re-export.
        import lib.provider_probe as pp
        self.pp = pp

    def _multi(self, statuses, attempts=3):
        """Patch probe_one_cell to yield the given status sequence."""
        seq = iter(statuses)

        def fake(*a, **kw):
            try:
                return next(seq), 'detail'
            except StopIteration:
                return statuses[-1], 'detail'

        with mock.patch.object(self.pp, 'probe_one_cell', side_effect=fake), \
                mock.patch.object(self.pp, '_time') as _t:
            _t.sleep = lambda *_a, **_k: None
            return self.pp.probe_cell_multi('u', 'k', 'm', {}, 5, attempts=attempts)

    def test_transient_429_then_ok_counts_as_ok(self):
        # First attempt false-429, second ok → reachable.
        status, detail = self._multi(['rate_limited', 'ok'])
        self.assertEqual(status, 'ok')
        self.assertIn('attempt 2/3', detail)

    def test_persistent_429_stays_flagged(self):
        status, detail = self._multi(['rate_limited', 'rate_limited', 'rate_limited'])
        self.assertEqual(status, 'rate_limited')
        self.assertIn('3/3', detail)

    def test_definitive_not_found_returns_immediately(self):
        # not_found is definitive — must NOT retry (only one call consumed).
        calls = {'n': 0}

        def fake(*a, **kw):
            calls['n'] += 1
            return 'not_found', 'HTTP 404'

        with mock.patch.object(self.pp, 'probe_one_cell', side_effect=fake), \
                mock.patch.object(self.pp, '_time') as _t:
            _t.sleep = lambda *_a, **_k: None
            status, _ = self.pp.probe_cell_multi('u', 'k', 'm', {}, 5, attempts=3)
        self.assertEqual(status, 'not_found')
        self.assertEqual(calls['n'], 1)

    def test_first_attempt_ok_no_note(self):
        status, detail = self._multi(['ok'])
        self.assertEqual(status, 'ok')
        self.assertNotIn('attempt', detail)


class ProbeProtocolTest(unittest.TestCase):
    """``protocol='anthropic'`` probes the Messages API with anthropic auth."""

    def _capture(self, protocol):
        import routes.config as cfg
        seen = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            seen['url'] = url
            seen['headers'] = headers
            seen['body'] = json
            return _FakeResp(200, 'ok')

        with mock.patch('lib.http_client.http_post', side_effect=fake_post):
            status, _ = cfg._probe_one_cell(
                'https://api.openai.com/v1/anthropic', 'app-id-123',
                'yuju-claude-opus-4.7-evaDaily', {'M-X': '1'}, 5, protocol)
        return status, seen

    def test_anthropic_branch_url_headers_body(self):
        status, seen = self._capture('anthropic')
        self.assertEqual(status, 'ok')
        self.assertEqual(seen['url'],
                         'https://api.openai.com/v1/anthropic/v1/messages')
        self.assertEqual(seen['headers'].get('x-api-key'), 'app-id-123')
        self.assertEqual(seen['headers'].get('anthropic-version'), '2023-06-01')
        self.assertEqual(seen['headers'].get('M-X'), '1')  # extra_headers merged
        self.assertEqual(seen['body']['max_tokens'], 1)
        self.assertNotIn('stream', seen['body'])

    def test_openai_branch_unchanged(self):
        status, seen = self._capture('openai')
        self.assertEqual(status, 'ok')
        self.assertTrue(seen['url'].endswith('/chat/completions'))
        self.assertEqual(seen['headers'].get('Authorization'), 'Bearer app-id-123')
        self.assertEqual(seen['body']['stream'], False)


if __name__ == '__main__':
    unittest.main()
