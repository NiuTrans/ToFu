"""Project context advertises journal location without reading or mutating history."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.project_mod import config as cfg
from lib.project_mod.indexer import (
    _TOFU_ARTIFACT_IGNORES,
    _ensure_gitignored,
    get_context_for_prompt,
)
from lib.project_mod.scanner import clear_project, set_project_paths


class JournalContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='journal-context-')
        self.addCleanup(self.tmp.cleanup)
        self.proj = Path(self.tmp.name) / 'proj'
        self.proj.mkdir()
        self.path = str(self.proj)

    def tearDown(self):
        clear_project()
        cfg._conv_roots.clear()
        cfg._conv_primary.clear()

    def snapshot(self):
        return {str(p.relative_to(self.proj)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.proj.rglob('*') if p.is_file()}

    def test_missing_journal_is_locator_only_without_writes(self):
        (self.proj / '.git').mkdir()
        set_project_paths([self.path])
        before = self.snapshot()
        ctx = get_context_for_prompt(self.path)
        self.assertIn(str(self.proj / 'JOURNAL.md'), ctx)
        self.assertIn('NOT current project rules', ctx)
        self.assertNotIn('append a dated entry', ctx)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.proj / 'JOURNAL.md').exists())
        self.assertFalse((self.proj / '.gitignore').exists())
        self.assertFalse((self.proj / '.tofu').exists())

    def test_existing_history_and_gitignore_unchanged_across_reads(self):
        (self.proj / 'JOURNAL.md').write_text(
            '# Project Journal\n\n### 2026-06-01 — PRIVATE OLD TITLE\nHistorical body.\n')
        (self.proj / '.gitignore').write_text('node_modules/\n')
        set_project_paths([self.path])
        before = self.snapshot()
        for _ in range(2):
            ctx = get_context_for_prompt(self.path)
            self.assertNotIn('PRIVATE OLD TITLE', ctx)
            self.assertNotIn('Historical body.', ctx)
        self.assertEqual(self.snapshot(), before)

    def test_prompt_never_invokes_journal_or_gitignore_maintenance(self):
        (self.proj / 'JOURNAL.md').write_text('Existing history')
        set_project_paths([self.path])
        with patch('lib.project_mod.journal.maybe_rotate') as rotate, \
                patch('lib.project_mod.journal.read_for_injection') as read, \
                patch('lib.project_mod.indexer._ensure_gitignored') as ignore:
            get_context_for_prompt(self.path)
        rotate.assert_not_called()
        read.assert_not_called()
        ignore.assert_not_called()

    def test_all_project_instruction_files_remain_in_context(self):
        for name in ('CLAUDE.md', 'AGENTS.md', '.cursorrules', 'COPILOT.md'):
            (self.proj / name).write_text(f'# {name}\nUnique mandatory rule from {name}.\n')
        set_project_paths([self.path])
        ctx = get_context_for_prompt(self.path)
        for name in ('CLAUDE.md', 'AGENTS.md', '.cursorrules', 'COPILOT.md'):
            self.assertIn(f'Unique mandatory rule from {name}.', ctx)
        self.assertIn('MANDATORY', ctx)
        self.assertIn('Project Journal — JOURNAL.md', ctx)

    def test_long_project_rules_are_not_clipped(self):
        rules = '# Rules\n' + ('Keep this instruction.\n' * 2000) + 'FINAL MANDATORY RULE'
        (self.proj / 'AGENTS.md').write_text(rules)
        set_project_paths([self.path])
        self.assertIn(rules, get_context_for_prompt(self.path))

    def test_readonly_primary_warns_without_maintenance_reminder(self):
        set_project_paths([self.path], readonly_paths=[self.path])
        ctx = get_context_for_prompt(self.path)
        self.assertIn('READ-ONLY primary root', ctx)
        self.assertNotIn('append a dated entry', ctx)
        self.assertFalse((self.proj / 'JOURNAL.md').exists())

    def test_conversation_roots_determine_readonly_and_locator_scope(self):
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        set_project_paths([self.path])
        cfg._conv_roots['journal-conv'] = {
            'primary': {'path': self.path, 'access': 'ro'},
            'reference': {'path': str(other), 'access': 'ro'},
        }
        ctx = get_context_for_prompt(self.path, conv_id='journal-conv')
        self.assertIn('READ-ONLY primary root', ctx)
        self.assertIn('READ-ONLY roots are attached for reference only', ctx)
        self.assertIn(str(self.proj / 'JOURNAL.md'), ctx)
        self.assertNotIn(str(other / 'JOURNAL.md'), ctx)
        self.assertNotIn('append a dated entry', ctx)

    def test_explicit_gitignore_helper_still_works(self):
        (self.proj / '.git').mkdir()
        for _ in range(2):
            _ensure_gitignored(self.path, _TOFU_ARTIFACT_IGNORES, 'Runtime artifacts')
        body = (self.proj / '.gitignore').read_text()
        self.assertEqual(body.count('.tofu*'), 1)


class AgentArtifactRegistryTest(unittest.TestCase):
    def test_known_and_future_names_are_recognised(self):
        from lib.agent_artifacts import KNOWN_ARTIFACT_NAMES, is_agent_artifact
        for name in (*KNOWN_ARTIFACT_NAMES, '.tofu_cache', '.tofu_whatever_2027',
                     '/some/project/.tofu_trash', '.tofu/'):
            self.assertTrue(is_agent_artifact(name), name)
        for name in ('lib', 'src', '.git', '.gitignore', '', 'tofu', '.venv'):
            self.assertFalse(is_agent_artifact(name), name)

    def test_gitignore_pattern_is_a_single_glob(self):
        from lib.agent_artifacts import ARTIFACT_PREFIX, GITIGNORE_PATTERN
        self.assertEqual(GITIGNORE_PATTERN, ARTIFACT_PREFIX + '*')


if __name__ == '__main__':
    unittest.main()
