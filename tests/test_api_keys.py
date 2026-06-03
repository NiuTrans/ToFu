"""tests/test_api_keys.py — lib.api_keys unit tests.

Exercises the issuance, validation, scope, persistence, and revocation
paths. Uses a temporary config_dir so the production data/config/
file is never touched.
"""

import os
import tempfile
import unittest
from unittest.mock import patch


class ApiKeysTest(unittest.TestCase):

    def setUp(self):
        # Patch CONFIG_DIR to a fresh tempdir for each test so api_keys.json
        # writes never collide with the real project file.
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = patch('lib.api_keys._STORE_PATH',
                             os.path.join(self._tmp.name, 'api_keys.json'))
        self._patch.start()
        # Force a fresh cache load.
        from lib import api_keys
        api_keys._cache.clear()
        api_keys._cache_loaded = False

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _reload(self):
        from lib import api_keys
        api_keys._cache.clear()
        api_keys._cache_loaded = False

    def test_create_returns_plaintext_once(self):
        from lib.api_keys import create_key, list_keys
        row, plaintext = create_key(name='build-bot',
                                     scopes=['chat', 'tasks'],
                                     rate_limit_rpm=60)
        self.assertTrue(plaintext.startswith('tofu_live_'))
        self.assertEqual(row['name'], 'build-bot')
        self.assertEqual(sorted(row['scopes']), ['chat', 'tasks'])
        self.assertNotIn('secret_hash', row)
        # list_keys should NEVER expose the hash.
        for k in list_keys():
            self.assertNotIn('secret_hash', k)

    def test_admin_token_has_admin_scope(self):
        from lib.api_keys import create_key
        row, plaintext = create_key(name='admin-bot', scopes=[], admin=True)
        self.assertTrue(plaintext.startswith('tofu_admin_'))
        self.assertIn('admin', row['scopes'])

    def test_validate_token_returns_context(self):
        from lib.api_keys import create_key, validate_token
        row, plaintext = create_key(name='build-bot', scopes=['chat'])
        ctx = validate_token(plaintext)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.key_id, row['id'])
        self.assertEqual(ctx.name, 'build-bot')
        self.assertIn('chat', ctx.scopes)
        self.assertTrue(ctx.is_authenticated)

    def test_validate_unknown_token_returns_none(self):
        from lib.api_keys import validate_token
        self.assertIsNone(validate_token('tofu_live_' + 'z' * 32))
        self.assertIsNone(validate_token('not-a-tofu-token'))
        self.assertIsNone(validate_token(''))

    def test_disabled_token_rejected(self):
        from lib.api_keys import create_key, update_key, validate_token
        row, plaintext = create_key(name='b', scopes=['chat'])
        update_key(row['id'], disabled=True)
        self.assertIsNone(validate_token(plaintext))

    def test_expired_token_rejected(self):
        from lib.api_keys import create_key, update_key, validate_token
        row, plaintext = create_key(name='b', scopes=['chat'])
        update_key(row['id'], expires_at=1.0)  # very long ago
        self.assertIsNone(validate_token(plaintext))

    def test_revoke(self):
        from lib.api_keys import create_key, get_key_by_id, revoke_key, validate_token
        row, plaintext = create_key(name='b', scopes=['chat'])
        self.assertTrue(revoke_key(row['id']))
        self.assertIsNone(get_key_by_id(row['id']))
        self.assertIsNone(validate_token(plaintext))

    def test_revoke_unknown_returns_false(self):
        from lib.api_keys import revoke_key
        self.assertFalse(revoke_key('k_doesnotexist'))

    def test_unknown_scope_dropped(self):
        from lib.api_keys import create_key
        row, _ = create_key(name='b', scopes=['chat', 'nonsense'])
        self.assertEqual(row['scopes'], ['chat'])

    def test_persistence_round_trip(self):
        from lib.api_keys import create_key, validate_token
        row, plaintext = create_key(name='persistent', scopes=['chat'])
        # Drop and reload the cache → should re-read from disk.
        self._reload()
        ctx = validate_token(plaintext)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.name, 'persistent')

    def test_authcontext_admin_implicit_grant(self):
        from lib.api_keys import AuthContext
        ctx = AuthContext(key_id='x', name='a',
                          scopes=frozenset({'admin'}))
        self.assertTrue(ctx.has_scope('chat'))
        self.assertTrue(ctx.has_scope('tasks'))
        self.assertTrue(ctx.has_scope('agents:trading'))

    def test_authcontext_tunnel_full_grant(self):
        from lib.api_keys import AuthContext
        ctx = AuthContext(via_tunnel_token=True, scopes=frozenset())
        self.assertTrue(ctx.has_scope('chat'))
        self.assertTrue(ctx.has_scope('admin'))


if __name__ == '__main__':
    unittest.main()
