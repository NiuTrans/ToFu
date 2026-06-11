"""tests/test_byo_egress.py — SSRF egress guard for BYO provider base URLs.

Covers lib.byo_egress: cloud-metadata / link-local / reserved are always
denied; loopback + private are allowed by default (self-hosted LLM case)
but lockable via env flags; scheme + allow-list handling.
"""

import os
import unittest
from unittest.mock import patch

from lib.byo_egress import EgressDenied, is_egress_allowed, validate_egress_url


class EgressGuardTest(unittest.TestCase):

    def _clear_flags(self):
        for k in ('TOFU_BYO_BLOCK_LOOPBACK', 'TOFU_BYO_BLOCK_PRIVATE',
                  'TOFU_BYO_ALLOW_HOSTS'):
            os.environ.pop(k, None)

    def setUp(self):
        self._clear_flags()

    def tearDown(self):
        self._clear_flags()

    def test_cloud_metadata_always_blocked(self):
        ok, reason = is_egress_allowed('http://169.254.169.254/latest/meta-data/')
        self.assertFalse(ok)
        self.assertIn('link-local', reason)

    def test_unspecified_blocked(self):
        ok, _ = is_egress_allowed('http://0.0.0.0:8080/v1')
        self.assertFalse(ok)

    def test_bad_scheme_blocked(self):
        for url in ('file:///etc/passwd', 'gopher://x/', 'ftp://h/'):
            ok, _ = is_egress_allowed(url)
            self.assertFalse(ok, url)

    def test_loopback_allowed_by_default(self):
        ok, reason = is_egress_allowed('http://127.0.0.1:8000/v1')
        self.assertTrue(ok, reason)

    def test_loopback_blocked_under_flag(self):
        os.environ['TOFU_BYO_BLOCK_LOOPBACK'] = '1'
        ok, reason = is_egress_allowed('http://127.0.0.1:8000/v1')
        self.assertFalse(ok)
        self.assertIn('loopback', reason)

    def test_private_allowed_by_default(self):
        ok, reason = is_egress_allowed('http://10.0.0.5:8080/v1')
        self.assertTrue(ok, reason)

    def test_private_blocked_under_flag(self):
        os.environ['TOFU_BYO_BLOCK_PRIVATE'] = '1'
        ok, reason = is_egress_allowed('http://10.0.0.5:8080/v1')
        self.assertFalse(ok)
        self.assertIn('private', reason)

    def test_allow_hosts_bypass(self):
        os.environ['TOFU_BYO_ALLOW_HOSTS'] = 'api.openai.com'
        ok, reason = is_egress_allowed('https://api.openai.com/v1')
        self.assertTrue(ok, reason)

    def test_validate_raises(self):
        with self.assertRaises(EgressDenied):
            validate_egress_url('http://169.254.169.254/')

    def test_dns_failure_denied(self):
        # A host that doesn't resolve must be denied, not allowed-through.
        ok, reason = is_egress_allowed('http://nonexistent.invalid./v1')
        self.assertFalse(ok)


if __name__ == '__main__':
    unittest.main()
