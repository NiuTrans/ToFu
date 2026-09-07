#!/usr/bin/env python3
"""Regression guard: the opensource-export secret scan must NOT skip a leak
that lives in a text file LARGER than rg's ``--max-filesize`` cap.

Background — the oversized-file leak class:

  ``_rg_files_with_matches`` (and the verify scan) drive the opensource
  sanitize/verify passes with ``rg --max-filesize 5M``. rg SILENTLY SKIPS any
  file above that size, so a >5 MB text file carrying an API key / internal
  hostname / ``/mnt/...`` path never entered the sanitize candidate set and
  was tar-copied to the public mirror VERBATIM — and the verify scan (also
  5M-capped) wouldn't even flag it.

Root-cause fix: ``_scan_oversized_text_files`` supplements the rg hit set by
scanning, in-process, every text file strictly ABOVE the cap; the verify pass
gained the same supplement. So an oversized leak is now sanitized (and, if it
somehow survives, flagged by verify) instead of shipped.

Internal tokens here are assembled from fragments (never a contiguous literal)
because this guard file is itself shipped in the exported tree — a raw literal
would reintroduce the very leak it guards against. See
tests/test_export_conf_path_sanitize.py's docstring.

Runs the REAL scan over a synthetic temp tree; no DB, no network.
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

# Fragment-assembled internal hostname (never a contiguous literal in-file).
_INTERNAL_HOST = 'secret-host.' + 'example-corp' + '.com'
_MNT = '/mnt/' + 'dolphin' + 'fs'


class OversizedLeakScanTest(unittest.TestCase):

    def _make_tree(self, tmp: str, *, oversized: bool) -> Path:
        root = Path(tmp)
        # A large text file: padding + the internal hostname on its own line.
        # >5 MB when oversized=True, well under when False.
        pad_mb = 6 if oversized else 1
        payload = ('# harmless padding line\n' * (pad_mb * 40000))
        content = payload + f'endpoint = https://{_INTERNAL_HOST}/api\n'
        f = root / 'big_config.py'
        f.write_text(content, encoding='utf-8')
        # Sanity: assert the size relationship we intend to exercise.
        return root

    def test_oversized_text_file_leak_is_detected(self):
        """A >5 MB text file containing an internal hostname MUST appear in the
        opensource candidate set (rg would skip it; the supplement catches it)."""
        from export import _RG_MAX_FILESIZE_BYTES, _rg_files_with_matches
        import re as _re
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(tmp, oversized=True)
            big = root / 'big_config.py'
            self.assertGreater(big.stat().st_size, _RG_MAX_FILESIZE_BYTES,
                               'test file must exceed the rg cap to be meaningful')
            # Pattern the opensource sanitizer uses for .internal.example.com hostnames.
            pat = _re.escape('.' + 'example-corp' + '.com')
            hits = _rg_files_with_matches(root, [pat])
        self.assertIn('big_config.py', hits,
                      'an oversized text file with an internal hostname must be '
                      'flagged for sanitization, not silently shipped')

    def test_undersized_file_still_detected_via_rg(self):
        """Control: a small file with the same leak is caught by the normal rg
        path — proving the pattern matches and the oversized branch is the ONLY
        thing the big-file case adds."""
        from export import _rg_files_with_matches
        import re as _re
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(tmp, oversized=False)
            pat = _re.escape('.' + 'example-corp' + '.com')
            hits = _rg_files_with_matches(root, [pat])
        self.assertIn('big_config.py', hits)

    def test_oversized_scanner_direct(self):
        """Directly exercise _scan_oversized_text_files: it returns only files
        ABOVE the cap that match, and skips at-or-below-cap files (rg's job)."""
        from export import _scan_oversized_text_files, _RG_MAX_FILESIZE_BYTES
        import re as _re
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(tmp, oversized=True)
            pat = _re.escape('.' + 'example-corp' + '.com')
            out = _scan_oversized_text_files(root, f'(?:{pat})')
            self.assertIn('big_config.py', out)
            # A below-cap sibling with the leak must NOT be returned here (the
            # oversized scanner intentionally leaves those to rg).
            small = root / 'small.py'
            small.write_text(f'x = "{_INTERNAL_HOST}"\n', encoding='utf-8')
            self.assertLessEqual(small.stat().st_size, _RG_MAX_FILESIZE_BYTES)
            out2 = _scan_oversized_text_files(root, f'(?:{pat})')
            self.assertNotIn('small.py', out2,
                             'below-cap files are rg territory, not the '
                             'oversized supplement')


if __name__ == '__main__':
    unittest.main()
