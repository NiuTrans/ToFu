"""Unit tests for pymupdf4llm import-failure diagnosis.

Regression guard for the "not installed" misdiagnosis: when pymupdf4llm IS
installed but pins a newer pymupdf than is present, its ``__init__`` raises an
ImportError carrying a version message. The old code logged that as
"not installed", sending us chasing a phantom missing dependency when the real
fix is a version pin. ``_diagnose_pymupdf4llm_failure`` must distinguish the
two causes deterministically.

Run:  pytest tests/test_pdf_pymupdf4llm_diagnosis.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.unit
class TestPymupdf4llmDiagnosis:
    """_diagnose_pymupdf4llm_failure classifies the ImportError cause."""

    def test_version_mismatch_when_installed(self):
        from lib.pdf_parser._common import _diagnose_pymupdf4llm_failure
        # Exactly the message this environment produces.
        exc = ImportError(
            "Requires PyMuPDF VERSION='1.27.2.3', but you have "
            "pymupdf.__version__='1.27.2.2'")
        reason = _diagnose_pymupdf4llm_failure(exc, installed=True)
        assert reason.startswith('version/ABI mismatch')
        assert '1.27.2.3' in reason
        # Must NOT be misreported as absent.
        assert 'not installed' not in reason

    def test_not_installed_when_absent(self):
        from lib.pdf_parser._common import _diagnose_pymupdf4llm_failure
        exc = ImportError("No module named 'pymupdf4llm'")
        reason = _diagnose_pymupdf4llm_failure(exc, installed=False)
        assert reason.startswith('not installed')
        assert not reason.startswith('version/ABI mismatch')

    def test_reason_constant_is_string(self):
        """The module-level reason is always a str (empty when available)."""
        from lib.pdf_parser._common import (
            HAS_PYMUPDF4LLM,
            PYMUPDF4LLM_UNAVAILABLE_REASON,
        )
        assert isinstance(PYMUPDF4LLM_UNAVAILABLE_REASON, str)
        # Invariant: available ⇔ empty reason.
        if HAS_PYMUPDF4LLM:
            assert PYMUPDF4LLM_UNAVAILABLE_REASON == ''
        else:
            assert PYMUPDF4LLM_UNAVAILABLE_REASON != ''
            assert (PYMUPDF4LLM_UNAVAILABLE_REASON.startswith('version')
                    or PYMUPDF4LLM_UNAVAILABLE_REASON.startswith('not installed'))


def _tiny_pdf_bytes() -> bytes:
    """Build a 1-page PDF with a little text, in memory, via pymupdf."""
    import pymupdf  # installed on this host (HAS_PYMUPDF)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), 'Hello world. This is a test paragraph.')
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.unit
class TestParsePdfWarningReflectsCause:
    """parse_pdf's user-facing warnings must state the real cause — exercised
    through the REAL parse_pdf code path, not a re-implementation."""

    def _require_pymupdf(self):
        from lib.pdf_parser._common import HAS_PYMUPDF
        if not HAS_PYMUPDF:
            pytest.skip('pymupdf not installed — cannot build a test PDF')

    def test_version_mismatch_warning_text(self, monkeypatch):
        self._require_pymupdf()
        import lib.pdf_parser.core as core
        # Force the "installed but version-incompatible" state regardless of
        # the host's actual pymupdf4llm state.
        monkeypatch.setattr(core, 'HAS_PYMUPDF4LLM', False)
        monkeypatch.setattr(core, 'PYMUPDF4LLM_UNAVAILABLE_REASON',
                            'version/ABI mismatch: Requires PyMuPDF 1.27.2.3')
        result = core.parse_pdf(_tiny_pdf_bytes(), max_images=0)
        joined = ' '.join(result['warnings'])
        assert 'version/ABI mismatch' in joined
        assert 'pymupdf4llm not installed;' not in joined

    def test_not_installed_warning_text(self, monkeypatch):
        self._require_pymupdf()
        import lib.pdf_parser.core as core
        monkeypatch.setattr(core, 'HAS_PYMUPDF4LLM', False)
        monkeypatch.setattr(core, 'PYMUPDF4LLM_UNAVAILABLE_REASON',
                            "not installed: No module named 'pymupdf4llm'")
        result = core.parse_pdf(_tiny_pdf_bytes(), max_images=0)
        joined = ' '.join(result['warnings'])
        assert 'pymupdf4llm not installed;' in joined
        assert 'version/ABI mismatch' not in joined
