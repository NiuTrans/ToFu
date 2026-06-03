"""tests/test_first_run_token_lifecycle.py — .first_run_token lifecycle.

Guards against the bug where ``data/config/.first_run_token`` (a one-shot
emergency copy of the bootstrap admin key) drifts out of sync with
``api_keys.json``: revoking/rotating the bootstrap key used to leave a
dead reference on disk that the README pointed users at, yielding a
confusing ``Invalid or expired API key`` 401.

Each test runs against a fresh temp config dir so the production
``data/config/`` files are never touched.
"""

import os
import tempfile
import unittest
from unittest.mock import patch


class FirstRunTokenLifecycleTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._store = os.path.join(self._tmp.name, 'api_keys.json')
        self._token_file = os.path.join(self._tmp.name, '.first_run_token')
        self._patches = [
            patch('lib.api_keys._STORE_PATH', self._store),
            patch('lib.api_keys._FIRST_RUN_TOKEN_FILE', self._token_file),
        ]
        for p in self._patches:
            p.start()
        self._reload()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _reload(self):
        from lib import api_keys
        api_keys._cache.clear()
        api_keys._cache_loaded = False

    def test_bootstrap_writes_valid_token(self):
        """case 1: bootstrap → file exists → content validates as admin."""
        from lib.api_keys import bootstrap_personal_key, validate_token
        plaintext = bootstrap_personal_key()
        self.assertIsNotNone(plaintext)
        self.assertTrue(os.path.exists(self._token_file))
        with open(self._token_file, encoding='utf-8') as fh:
            on_disk = fh.read().strip()
        self.assertEqual(on_disk, plaintext)
        ctx = validate_token(on_disk)
        self.assertIsNotNone(ctx)
        self.assertTrue(ctx.has_scope('admin'))

    def test_revoke_bootstrap_key_removes_token_file(self):
        """case 2: bootstrap → revoke that key → file is gone."""
        from lib.api_keys import (bootstrap_personal_key, list_keys,
                                   revoke_key)
        bootstrap_personal_key()
        self.assertTrue(os.path.exists(self._token_file))
        key_id = list_keys()[0]['id']
        self.assertTrue(revoke_key(key_id))
        self.assertFalse(os.path.exists(self._token_file))

    def test_revoke_non_bootstrap_key_leaves_token_file(self):
        """case 3: a non-bootstrap key never touches .first_run_token."""
        from lib.api_keys import (bootstrap_personal_key, create_key,
                                   revoke_key)
        bootstrap_personal_key()
        self.assertTrue(os.path.exists(self._token_file))
        row, _ = create_key(name='ci', scopes=['chat'])
        revoke_key(row['id'])
        # The unrelated revoke must not have cleared the emergency token.
        self.assertTrue(os.path.exists(self._token_file))

    def test_startup_purges_stale_token_file(self):
        """case 4: stale token on disk → next startup deletes it + warns."""
        from lib import api_keys
        from lib.api_keys import (bootstrap_personal_key, create_key,
                                  validate_token)
        # Simulate the real failure: a token file pointing at a key that
        # no longer exists in the store (rotated while the process was
        # down). Write a syntactically-valid but unknown token.
        with open(self._token_file, 'w', encoding='utf-8') as fh:
            fh.write('tofu_admin_' + 'a' * 32 + '\n')
        self.assertIsNone(validate_token('tofu_admin_' + 'a' * 32))

        # Put a *different* key in the store so bootstrap's empty-store
        # short-circuit does NOT mint a new one — we only want to test
        # the stale-purge branch.
        create_key(name='other', scopes=['chat'], admin=True)

        with self.assertLogs('lib.api_keys', level='WARNING') as cm:
            self.assertIsNone(bootstrap_personal_key())
        self.assertFalse(os.path.exists(self._token_file))
        self.assertTrue(any('Stale .first_run_token' in m for m in cm.output))

    def test_startup_keeps_valid_token_file(self):
        """A still-valid token file survives the startup self-check."""
        from lib.api_keys import bootstrap_personal_key
        plaintext = bootstrap_personal_key()
        self.assertIsNotNone(plaintext)
        # Drop the in-memory cache so the next bootstrap re-reads disk and
        # runs the stale-purge against the (still valid) persisted key.
        self._reload()
        self.assertIsNone(bootstrap_personal_key())
        self.assertTrue(os.path.exists(self._token_file))


if __name__ == '__main__':
    unittest.main()
