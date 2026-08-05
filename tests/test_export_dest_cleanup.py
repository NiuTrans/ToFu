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
        src = Path(export.__file__).read_text(encoding='utf-8')
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


if __name__ == '__main__':
    unittest.main()
