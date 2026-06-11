"""tests/test_byo_providers.py — Persistent BYO provider store.

Covers:
* create / list / get / update / delete
* per-key isolation (caller A never sees caller B's row)
* api_key redaction in public views
* resolve_model_string ('foo' / 'foo@prov_xxx' / 'foo@1.0' / unknown)
* per-key quota enforcement
"""

import os
import tempfile
import unittest


class ByoProviderStoreTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        from lib import byo_providers
        cls._orig_store = byo_providers._STORE_PATH
        byo_providers._STORE_PATH = os.path.join(cls._tmp.name,
                                                  'byo_providers.json')
        byo_providers._cache.clear()
        byo_providers._cache_loaded = False

    @classmethod
    def tearDownClass(cls):
        from lib import byo_providers
        byo_providers._STORE_PATH = cls._orig_store
        byo_providers._cache.clear()
        byo_providers._cache_loaded = False
        cls._tmp.cleanup()

    def setUp(self):
        from lib import byo_providers
        byo_providers._cache.clear()
        byo_providers._cache_loaded = False
        # Wipe the on-disk store so each test starts fresh.
        try:
            os.remove(byo_providers._STORE_PATH)
        except FileNotFoundError:
            pass

    # ── CRUD ────────────────────────────────────────────────────────

    def test_create_and_list(self):
        from lib.byo_providers import (
            create_provider, get_provider, list_providers, redact,
        )
        row = create_provider(
            owner_key_id='k_alice',
            name='cluster-A',
            base_url='http://10.0.0.5:8080/v1',
            api_key='sk-secret-AAAA',
            models=[{'model_id': 'deepseek-v4-pro'}],
        )
        self.assertTrue(row['id'].startswith('prov_'))
        self.assertEqual(row['owner_key_id'], 'k_alice')
        self.assertEqual(row['api_key'], 'sk-secret-AAAA')

        # list_providers redacts api_key into key_hint
        listed = list_providers('k_alice')
        self.assertEqual(len(listed), 1)
        self.assertNotIn('api_key', listed[0])
        self.assertIn('key_hint', listed[0])
        self.assertTrue(listed[0]['key_hint'].startswith('sk-sec'))

        # get_provider (internal) keeps the raw api_key
        fetched = get_provider(row['id'], 'k_alice')
        self.assertEqual(fetched['api_key'], 'sk-secret-AAAA')

        # redact is idempotent on already-public row
        red = redact(row)
        self.assertNotIn('api_key', red)
        self.assertIn('key_hint', red)

    def test_per_key_isolation(self):
        from lib.byo_providers import (
            create_provider, get_provider, list_providers,
        )
        a = create_provider(
            owner_key_id='k_alice', name='A',
            base_url='http://1.1.1.1:8080/v1', api_key='', models=[],
        )
        create_provider(
            owner_key_id='k_bob', name='B',
            base_url='http://2.2.2.2:8080/v1', api_key='', models=[],
        )
        # Alice sees only her row
        alice_list = list_providers('k_alice')
        self.assertEqual(len(alice_list), 1)
        self.assertEqual(alice_list[0]['name'], 'A')
        # Bob can't get Alice's prov_id
        self.assertIsNone(get_provider(a['id'], 'k_bob'))
        # Alice can
        self.assertIsNotNone(get_provider(a['id'], 'k_alice'))

    def test_update_and_delete(self):
        from lib.byo_providers import (
            create_provider, delete_provider, get_provider, update_provider,
        )
        row = create_provider(
            owner_key_id='k', name='n',
            base_url='http://127.0.0.1:8080/v1', api_key='', models=[],
        )
        ok = update_provider(row['id'], 'k', name='renamed', disabled=True)
        self.assertTrue(ok)
        upd = get_provider(row['id'], 'k')
        self.assertEqual(upd['name'], 'renamed')
        self.assertTrue(upd['disabled'])

        # Wrong owner update fails
        self.assertFalse(update_provider(row['id'], 'someone-else', name='x'))

        # Delete
        self.assertTrue(delete_provider(row['id'], 'k'))
        self.assertIsNone(get_provider(row['id'], 'k'))
        # Idempotent
        self.assertFalse(delete_provider(row['id'], 'k'))

    def test_validation_rejects_bad_input(self):
        from lib.byo_providers import create_provider
        with self.assertRaises(ValueError):
            create_provider(owner_key_id='k', name='', base_url='x',
                            api_key='', models=[])
        with self.assertRaises(ValueError):
            create_provider(owner_key_id='k', name='n',
                            base_url='ftp://nope', api_key='', models=[])
        with self.assertRaises(ValueError):
            create_provider(owner_key_id='k', name='n',
                            base_url='http://h/v1', api_key='',
                            models=[{'no_model_id': 'x'}])

    def test_quota_per_key(self):
        from lib.byo_providers import _MAX_PROVIDERS_PER_KEY, create_provider
        for i in range(_MAX_PROVIDERS_PER_KEY):
            create_provider(
                owner_key_id='k_quota', name=f'n{i}',
                base_url=f'http://127.0.0.1:{8000 + i}/v1',
                api_key='', models=[],
            )
        with self.assertRaises(RuntimeError):
            create_provider(
                owner_key_id='k_quota', name='overflow',
                base_url='http://127.0.0.1:9999/v1', api_key='', models=[],
            )

    # ── resolve_model_string ────────────────────────────────────────

    def test_resolve_plain_model(self):
        from lib.byo_providers import resolve_model_string
        rm = resolve_model_string('deepseek-v4-pro', 'k_alice')
        self.assertIsNotNone(rm)
        self.assertEqual(rm.model_id, 'deepseek-v4-pro')
        self.assertIsNone(rm.provider)

    def test_resolve_byo_suffix_hits(self):
        from lib.byo_providers import create_provider, resolve_model_string
        row = create_provider(
            owner_key_id='k_alice', name='c',
            base_url='http://127.0.0.1:8080/v1', api_key='sk-x',
            models=[{'model_id': 'deepseek-v4-pro'}],
        )
        rm = resolve_model_string(f'deepseek-v4-pro@{row["id"]}', 'k_alice')
        self.assertIsNotNone(rm)
        self.assertEqual(rm.model_id, 'deepseek-v4-pro')
        self.assertIsNotNone(rm.provider)
        self.assertEqual(rm.provider['id'], row['id'])
        self.assertEqual(rm.provider['api_key'], 'sk-x')  # internal lookup carries plaintext

    def test_resolve_byo_suffix_wrong_owner_returns_none(self):
        from lib.byo_providers import create_provider, resolve_model_string
        row = create_provider(
            owner_key_id='k_alice', name='c',
            base_url='http://127.0.0.1:8080/v1', api_key='', models=[],
        )
        # Bob can't pin to Alice's provider
        rm = resolve_model_string(f'foo@{row["id"]}', 'k_bob')
        self.assertIsNone(rm)

    def test_resolve_byo_suffix_disabled_returns_none(self):
        from lib.byo_providers import (
            create_provider, resolve_model_string, update_provider,
        )
        row = create_provider(
            owner_key_id='k', name='c',
            base_url='http://127.0.0.1:8080/v1', api_key='', models=[],
        )
        update_provider(row['id'], 'k', disabled=True)
        self.assertIsNone(resolve_model_string(f'foo@{row["id"]}', 'k'))

    def test_resolve_version_suffix_passes_through(self):
        from lib.byo_providers import resolve_model_string
        # `foo@1.0` is NOT a BYO suffix — must not look up a provider
        rm = resolve_model_string('foo@1.0', 'k_alice')
        self.assertIsNotNone(rm)
        self.assertEqual(rm.model_id, 'foo@1.0')
        self.assertIsNone(rm.provider)


if __name__ == '__main__':
    unittest.main()
