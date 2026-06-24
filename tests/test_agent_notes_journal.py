"""tests/test_agent_notes_journal.py — JOURNAL.md evolution-journal context.

Covers ``get_context_for_prompt``'s journal handling (Option A — auto-create):
  * a writable primary root that lacks JOURNAL.md gets one SEEDED on disk,
    and its content is injected into the prompt
  * an existing JOURNAL.md is NEVER clobbered (seed must not overwrite)
  * a read-only primary root is left untouched (no file written, no inject)
  * the seed carries the evolution-tracking rules + update/reference instruction
  * the journal coexists with CLAUDE.md
"""

from __future__ import annotations

import os
import unittest

from lib.project_mod import config as cfg
from lib.project_mod.indexer import _JOURNAL_FILE, get_context_for_prompt
from lib.project_mod.scanner import clear_project, set_project_paths


class _TmpProject(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix='journal-')
        self.proj = os.path.join(self._tmp, 'proj')
        os.makedirs(self.proj)

    def tearDown(self):
        clear_project()
        cfg._conv_roots.clear()
        cfg._conv_primary.clear()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    @property
    def journal_path(self):
        return os.path.join(self.proj, _JOURNAL_FILE)

    def _read_journal(self):
        with open(self.journal_path, encoding='utf-8') as f:
            return f.read()

    def _write_claude(self, text='# rules\n'):
        with open(os.path.join(self.proj, 'CLAUDE.md'), 'w') as f:
            f.write(text)


class JournalAutoCreateTest(_TmpProject):

    def test_writable_root_seeds_journal_and_injects(self):
        set_project_paths([self.proj])
        ctx = get_context_for_prompt(self.proj)
        # File was created on disk...
        self.assertTrue(os.path.isfile(self.journal_path))
        seed = self._read_journal()
        # ...with the evolution-tracking rules + update/reference instruction.
        self.assertIn('evolution journal', seed)
        self.assertIn('Read it first', seed)
        self.assertIn('experiment results', seed)
        # ...and the content is injected for the model to read.
        self.assertIn(f'Project Journal — {_JOURNAL_FILE}', ctx)
        self.assertIn('evolution journal', ctx)

    def test_existing_journal_is_never_clobbered(self):
        original = '# Project Journal\n\n### 2026-06-01 — first entry\nReal history.\n'
        with open(self.journal_path, 'w') as f:
            f.write(original)
        set_project_paths([self.proj])
        ctx = get_context_for_prompt(self.proj)
        # Seed must NOT overwrite real content.
        self.assertEqual(self._read_journal(), original)
        self.assertIn('Real history.', ctx)

    def test_readonly_root_is_left_untouched(self):
        set_project_paths([self.proj], readonly_paths=[self.proj])
        ctx = get_context_for_prompt(self.proj)
        # No file written, nothing injected.
        self.assertFalse(os.path.exists(self.journal_path))
        self.assertNotIn(_JOURNAL_FILE, ctx)

    def test_seed_is_idempotent_across_calls(self):
        set_project_paths([self.proj])
        get_context_for_prompt(self.proj)
        first = self._read_journal()
        # A user adds a real entry; a later context build must not reset it.
        with open(self.journal_path, 'a') as f:
            f.write('\n### 2026-06-02 — entry\nDid a thing.\n')
        get_context_for_prompt(self.proj)
        self.assertIn('Did a thing.', self._read_journal())
        self.assertNotEqual(first, self._read_journal())

    def test_gitignore_appended_in_git_repo(self):
        os.makedirs(os.path.join(self.proj, '.git'))
        set_project_paths([self.proj])
        get_context_for_prompt(self.proj)
        gi = os.path.join(self.proj, '.gitignore')
        self.assertTrue(os.path.isfile(gi))
        with open(gi) as f:
            self.assertIn(_JOURNAL_FILE, f.read())

    def test_gitignore_appended_when_gitignore_already_exists(self):
        gi = os.path.join(self.proj, '.gitignore')
        with open(gi, 'w') as f:
            f.write('node_modules/\n')
        set_project_paths([self.proj])
        get_context_for_prompt(self.proj)
        with open(gi) as f:
            body = f.read()
        self.assertIn('node_modules/', body)   # preserved
        self.assertIn(_JOURNAL_FILE, body)      # appended

    def test_no_stray_gitignore_in_non_git_dir(self):
        set_project_paths([self.proj])
        get_context_for_prompt(self.proj)
        # Journal created, but no .gitignore conjured in a non-git directory.
        self.assertTrue(os.path.isfile(self.journal_path))
        self.assertFalse(os.path.exists(os.path.join(self.proj, '.gitignore')))

    def test_gitignore_not_duplicated_when_already_listed(self):
        os.makedirs(os.path.join(self.proj, '.git'))
        gi = os.path.join(self.proj, '.gitignore')
        with open(gi, 'w') as f:
            f.write(f'{_JOURNAL_FILE}\n')
        set_project_paths([self.proj])
        get_context_for_prompt(self.proj)
        with open(gi) as f:
            self.assertEqual(f.read().count(_JOURNAL_FILE), 1)

    def test_journal_coexists_with_claude_md(self):
        self._write_claude('# MANDATORY rules\n')
        set_project_paths([self.proj])
        ctx = get_context_for_prompt(self.proj)
        self.assertIn('Project Intelligence — CLAUDE.md', ctx)
        self.assertIn('MANDATORY', ctx)
        self.assertIn(f'Project Journal — {_JOURNAL_FILE}', ctx)
        self.assertTrue(os.path.isfile(self.journal_path))


if __name__ == '__main__':
    unittest.main()
