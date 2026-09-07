#!/usr/bin/env python3
"""Regression guard: the opensource-export secret scan must NOT be blinded by
the repo's dev-facing ``.ignore`` file.

Background — the .ignore blindness class:

  The repo root ships an ``.ignore`` that hides generated delivery bundles
  (``frontend/src/runtime/*.generated.js``, ``app-runtime.js`` …) from
  ordinary ``rg`` discovery so model-facing searches land on authoring
  sources. rg honours ``.ignore`` EVEN WITH ``--no-ignore-vcs`` — that flag
  only disables the gitignore family. The export tar-copies ``.ignore`` into
  the dest tree, so the sanitize candidate scan (previously run with just
  ``--no-ignore-vcs``) could not SEE any generated bundle: a trigger that
  survived only inside one — sanitized in the section source, but the
  composed bundle in the worktree still verbatim — shipped unsanitized.
  ``frontend/src/runtime/settings-presenters.generated.js`` leaked the
  internal gateway hostname exactly this way; only the python-based publish
  gate caught it (4 findings, export aborted).

Fix: the scan now passes ``--no-ignore`` — ignore files are a discovery aid,
never a security boundary.

Internal tokens here are assembled from fragments (never a contiguous
literal) because this guard file is itself shipped in the exported tree. See
tests/test_export_conf_path_sanitize.py's docstring.

Runs the REAL scan / sanitize over a synthetic temp tree; no DB, no network.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# export.py is the maintainer's release tool; not shipped in opensource builds.
pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit

# Fragment-assembled internal markers (never contiguous literals in-file).
_INTERNAL_HOST = 'secret-host.' + 'sankuai' + '.com'
_MNT = '/mnt/' + 'dolphin' + 'fs'


class IgnoreFileLeakScanTest(unittest.TestCase):

    def _make_tree(self, tmp: str) -> Path:
        root = Path(tmp)
        # Mirror the repo's .ignore contract: generated delivery bundles are
        # hidden from ordinary rg discovery.
        (root / '.ignore').write_text('generated/bundle.js\n', encoding='utf-8')
        gen = root / 'generated'
        gen.mkdir()
        (gen / 'bundle.js').write_text(
            f'var endpoint = "https://{_INTERNAL_HOST}/api";\n'
            f'var stateDir = "{_MNT}/state";\n', encoding='utf-8')
        # Control: an ordinary (non-ignored) source with the same markers.
        (root / 'section_source.js').write_text(
            f'var endpoint = "https://{_INTERNAL_HOST}/api";\n', encoding='utf-8')
        return root

    def test_ignore_listed_bundle_enters_candidate_set(self):
        """A trigger inside an .ignore-listed generated bundle MUST still
        enter the sanitize candidate set — ignore files steer discovery,
        not security."""
        from export import _rg_files_with_matches
        import re as _re
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(tmp)
            pat = _re.escape('.' + 'sankuai' + '.com')
            hits = _rg_files_with_matches(root, [pat])
        self.assertIn('section_source.js', hits,
                      'control: ordinary sources are scanned')
        self.assertIn('generated/bundle.js', hits,
                      'an .ignore-listed generated bundle carrying a trigger '
                      'must be sanitized, not silently shipped')

    def test_end_to_end_sanitize_rewrites_ignored_bundle(self):
        """The full opensource post-copy sanitize must REWRITE a well-known
        trigger inside an .ignore-listed bundle, not just list the file."""
        from export import _post_copy_sanitize
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(tmp)
            _post_copy_sanitize(root, 'opensource')
            self.assertNotIn(
                _MNT, (root / 'generated' / 'bundle.js').read_text(
                    encoding='utf-8'),
                'the internal mount prefix must be rewritten even inside an '
                '.ignore-listed generated bundle')


if __name__ == '__main__':
    unittest.main()
