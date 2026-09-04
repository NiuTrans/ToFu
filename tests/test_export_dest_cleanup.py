#!/usr/bin/env python3
"""tests/test_export_dest_cleanup.py — dest-cleanup mirror semantics for the
opensource (publish) export mode.

WHY
---
``promo`` entered ``ALWAYS_EXCLUDE_DIRS`` on 2026-06-10, yet promo/ (~26MB:
a 17.8MB font + slide PNGs) was STILL on the GitHub mirror in 2026-08 —
doubling the update tarball users download. Root cause: the pre-tar dest
cleanup treated export-EXCLUDED dirs as PRESERVED (an FUSE-I/O optimisation
meant for live-install dests), so excluded content committed once rode every
subsequent ``git add -A`` forever.

Fix: ``_dest_cleanup_targets`` is mode-aware. A live-install dest (personal /
internal) keeps the old behaviour (preserve user data + excluded dirs +
non-source items). An opensource dest is a PUBLISH MIRROR of the export set:
preserve ONLY operator/runtime state (``_OPENSOURCE_DEST_PRESERVE``), delete
everything else — excluded content dirs AND stale entries no longer in
source.

Also guards the static/images audit: the 8 marketing assets there (~12.4MB,
zero runtime references — live icons come from static/icons/) must stay in
OPENSOURCE_EXTRA_EXCLUDE_FILES.

Behavioural control included (internal mode keeps promo) + shipped-source
needle (export_project must call the helper), so a bypass regresses red.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# export.py is the maintainer's release tool; not shipped in opensource builds.
export = pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit

_IMAGES = [
    'tofu-cache-article-cover.png',
    'tofu-cache-article-cover-zh.png',
    'tofu-poster-core-strength.png',
    'tofu-poster-v2.png',
    'attach-icon.png',
    'attach-icon.svg',
    'onigiri-icon.png',
    'onigiri-icon.svg',
]


def _make_dest(tmp: str, names) -> Path:
    dest = Path(tmp)
    for name in names:
        p = dest / name
        if name.endswith('.txt') or '.' in name and not name.startswith('.'):
            p.write_text('x')
        else:
            p.mkdir(exist_ok=True)
    return dest


class DestCleanupTargetsTest(unittest.TestCase):

    def test_opensource_deletes_excluded_content_and_stale(self):
        """THE fix: promo/ (excluded but in source) and a stale dest-only
        file are BOTH deleted from a publish mirror; .git/data/uploads stay."""
        with tempfile.TemporaryDirectory() as d:
            dest = _make_dest(d, ['promo', 'lib', '.git', 'data', 'uploads',
                                  'stale_gone.txt'])
            src_names = {'lib', 'promo', 'routes'}
            got = sorted(x.name for x in export._dest_cleanup_targets(
                dest, 'opensource', src_names))
        self.assertEqual(got, ['lib', 'promo', 'stale_gone.txt'])

    def test_opensource_preserves_runtime_state(self):
        """Operator/runtime dirs in a dest doubling as a live install survive
        even the mirror cleanup; excluded content does not."""
        with tempfile.TemporaryDirectory() as d:
            dest = _make_dest(d, ['.tofu', 'pgdata', 'pg_backups', 'logs',
                                  '.chatui', 'promo', 'overleaf_cache'])
            got = sorted(x.name for x in export._dest_cleanup_targets(
                dest, 'opensource', {'promo', 'overleaf_cache', 'lib'}))
        self.assertEqual(got, ['overleaf_cache', 'promo'])

    def test_internal_keeps_old_preserve_semantics(self):
        """CONTROL: a live-install (internal) dest still preserves excluded
        dirs (FUSE I/O optimisation) and non-source items — proving the
        opensource branch above is the load-bearing difference, not a
        constant assertion."""
        with tempfile.TemporaryDirectory() as d:
            dest = _make_dest(d, ['promo', 'lib', 'data', 'stale_gone.txt'])
            got = sorted(x.name for x in export._dest_cleanup_targets(
                dest, 'internal', {'lib', 'promo'}))
        self.assertEqual(got, ['lib'])

    def test_force_strip_survives(self):
        """.tofu_env.json is stripped in BOTH mode families (wrong-interpreter
        guard)."""
        with tempfile.TemporaryDirectory() as d:
            dest = _make_dest(d, ['.tofu_env.json', 'lib'])
            for mode in ('opensource', 'internal', 'personal'):
                got = [x.name for x in export._dest_cleanup_targets(
                    dest, mode, {'lib'})]
                self.assertIn('.tofu_env.json', got, mode)

    def test_export_project_wires_the_helper(self):
        """Shipped-source needle: export_project must route its cleanup
        through _dest_cleanup_targets (a hand-rolled reimplementation or a
        dropped call regresses the mirror semantics invisibly)."""
        import inspect
        src = inspect.getsource(export.export_project)
        self.assertIn('targets = _dest_cleanup_targets(dest, mode, source_names)',
                      src)


class StaticImagesExclusionTest(unittest.TestCase):

    def test_unreferenced_images_excluded_from_opensource(self):
        for name in _IMAGES:
            self.assertIn(name, export.OPENSOURCE_EXTRA_EXCLUDE_FILES, name)
            reason = export._should_exclude(f'static/images/{name}', name,
                                            'opensource')
            self.assertIsNotNone(reason, f'{name} must be excluded (opensource)')

    def test_images_still_ship_in_personal(self):
        """Personal backups keep the marketing masters — the exclusion is
        opensource-only."""
        for name in _IMAGES[:2]:
            reason = export._should_exclude(f'static/images/{name}', name,
                                            'personal')
            self.assertIsNone(reason, f'{name} must survive personal exports')


class ToolchainSelfExclusionTest(unittest.TestCase):
    """The export toolchain itself + the internal journal must never reach
    the public tree: export_pkg/_lists.py carries private markers (real API
    keys, internal usernames/domains) as sanitization patterns, and
    JOURNAL.md is the internal evolution log (ops incidents, internal
    hosts/ids). The 2026-08 export of a stale ref proved the failure mode:
    export_pkg shipped and the sanitizer then rewrote _sanitize.py's own
    pattern literals in the dest tree."""

    def test_export_infra_excluded_from_opensource(self):
        self.assertIn('export.py', export.ALWAYS_EXCLUDE_FILES)
        self.assertIn('export_pkg', export.OPENSOURCE_EXTRA_EXCLUDE_DIRS)
        self.assertIn('JOURNAL.md', export.OPENSOURCE_EXTRA_EXCLUDE_FILES)
        for rel, name in (('export.py', 'export.py'),
                          ('export_pkg/_lists.py', '_lists.py'),
                          ('JOURNAL.md', 'JOURNAL.md')):
            reason = export._should_exclude(rel, name, 'opensource')
            self.assertIsNotNone(reason, f'{rel} must be excluded (opensource)')

    def test_export_infra_still_ships_in_personal(self):
        """Personal backups keep the toolchain + journal — the exclusion is
        opensource/internal-only."""
        for rel, name in (('export_pkg/_lists.py', '_lists.py'),
                          ('JOURNAL.md', 'JOURNAL.md')):
            reason = export._should_exclude(rel, name, 'personal')
            self.assertIsNone(reason, f'{rel} must survive personal exports')


class ExportedPySyntaxGateTest(unittest.TestCase):
    """The whole-tree ``ast.parse`` gate must fail the export when the
    sanitizer corrupts any ``.py`` (twin of the JS ``node --check`` gate)."""

    def test_corrupt_py_fails_the_gate(self):
        from export_pkg._verify import (
            ExportSyntaxError, _verify_exported_py_syntax)
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            (dest / 'ok.py').write_text('x = 1\n', encoding='utf-8')
            (dest / 'broken.py').write_text('def broken(:\n', encoding='utf-8')
            with self.assertRaises(ExportSyntaxError):
                _verify_exported_py_syntax(dest)

    def test_clean_tree_passes_the_gate(self):
        from export_pkg._verify import _verify_exported_py_syntax
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            (dest / 'ok.py').write_text('x = 1\n', encoding='utf-8')
            _verify_exported_py_syntax(dest)

if __name__ == '__main__':
    unittest.main()
