"""tests/database_panel/test_database_panel_switch.py — backend switch validation matrix.

A switch only ever persists ``TOFU_PERSONAL_STORAGE_BACKEND`` to .env (the
restart machinery applies it); migration is a background job.  These tests
pin the validation matrix and the resume-journal UUID recovery without
touching real storage.
"""



from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

import threading
import unittest
from unittest.mock import patch

from lib.database_panel import switch


class SwitchValidationTest(unittest.TestCase):

    def setUp(self):
        switch._migration_job = None
        self.env_writes = []
        patches = [
            patch.object(switch, '_write_backend_env',
                         side_effect=lambda backend: self.env_writes.append(
                             {backend: backend})),
            patch('lib.database_panel.probe.configured_backend',
                  return_value='sqlite'),
            patch('lib.database_panel.probe.resolve_tofudb_binary',
                  return_value='/usr/bin/tofu-db'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _describe(self, *, sqlite_data=True, tofudb_data=False):
        return {
            'current': 'sqlite',
            'sqlite': {'available': True, 'hasData': sqlite_data,
                       'dataBytes': 1024 if sqlite_data else 0},
            'tofudb': {'available': True, 'hasData': tofudb_data,
                       'authorityPath': '/data/tofudb-authority'},
        }

    def test_invalid_backend_rejected(self):
        with self.assertRaises(switch.SwitchError) as ctx:
            switch.request_switch('postgres')
        self.assertEqual(ctx.exception.code, 'invalid_backend')

    def test_already_active_rejected(self):
        with self.assertRaises(switch.SwitchError) as ctx:
            switch.request_switch('sqlite')
        self.assertEqual(ctx.exception.code, 'already_active')

    def test_engine_missing_rejected(self):
        with patch('lib.database_panel.probe.resolve_tofudb_binary',
                   return_value=None):
            with self.assertRaises(switch.SwitchError) as ctx:
                switch.request_switch('tofudb')
        self.assertEqual(ctx.exception.code, 'engine_missing')

    def test_plain_switch_to_tofudb_writes_env(self):
        with patch.object(switch, 'describe',
                          return_value=self._describe(sqlite_data=False)):
            result = switch.request_switch('tofudb')
        self.assertTrue(result['restartRequired'])
        self.assertEqual(result['migration'], 'none')
        self.assertEqual(self.env_writes, [{'tofudb': 'tofudb'}])

    def test_existing_authority_switch_notes_reuse(self):
        with patch.object(switch, 'describe',
                          return_value=self._describe(tofudb_data=True)):
            result = switch.request_switch('tofudb')
        self.assertIn('tofudb_authority_reused', result['notices'])
        self.assertNotIn('sqlite_data_not_migrated', result['notices'])

    def test_switch_to_sqlite_retains_tofudb_data_notice(self):
        with patch('lib.database_panel.probe.configured_backend',
                   return_value='tofudb'), \
                patch.object(switch, 'describe', return_value={
                    'current': 'tofudb',
                    'sqlite': {'available': True, 'hasData': False,
                               'dataBytes': 0},
                    'tofudb': {'available': True, 'hasData': True,
                               'authorityPath': '/data/tofudb-authority'},
                }):
            result = switch.request_switch('sqlite')
        self.assertTrue(result['restartRequired'])
        self.assertIn('tofudb_data_retained', result['notices'])
        self.assertEqual(self.env_writes, [{'sqlite': 'sqlite'}])

    def test_migrate_starts_background_job(self):
        started = threading.Event()

        def fake_run(job, binary):
            started.set()
            job['state'] = 'done'

        with patch.object(switch, 'describe',
                          return_value=self._describe(sqlite_data=True)), \
                patch.object(switch, '_run_migration', side_effect=fake_run), \
                patch.object(switch.threading, 'Thread',
                             wraps=threading.Thread) as thread_cls:
            result = switch.request_switch('tofudb', migrate=True)
        self.assertEqual(result['migration'], 'running')
        self.assertFalse(result['restartRequired'])
        self.assertTrue(started.wait(5))
        thread_cls.assert_called_once()
        self.assertEqual(self.env_writes, [])  # env flips only after import

    def test_migration_running_blocks_second_switch(self):
        switch._migration_job = {
            'id': 'x', 'state': 'running', 'phase': 'import',
            'startedAt': 0.0, 'finishedAt': 0.0, 'error': '',
            'resumeSupported': False,
        }
        with patch('lib.database_panel.probe.configured_backend',
                   return_value='tofudb'):
            with self.assertRaises(switch.SwitchError) as ctx:
                switch.request_switch('sqlite')
        self.assertEqual(ctx.exception.code, 'migration_running')


class CheckpointUuidTest(unittest.TestCase):

    def test_recovers_target_uuid(self, tmp_path=None):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / 'checkpoint.json'
            checkpoint.write_text(json.dumps({
                'binding': {'target_authority_uuid': 'abc-123',
                            'source_sha256': 'ff'},
                'domains': [],
            }))
            self.assertEqual(
                switch._uuid_from_checkpoint(checkpoint), 'abc-123')

    def test_missing_uuid_is_typed_error(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / 'checkpoint.json'
            checkpoint.write_text('{"domains": []}')
            with self.assertRaises(switch.SwitchError) as ctx:
                switch._uuid_from_checkpoint(checkpoint)
            self.assertEqual(ctx.exception.code, 'resume_unsupported')


if __name__ == '__main__':
    unittest.main()
