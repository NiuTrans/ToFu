"""The installer must produce the exact, functional PDF stack we declare."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _pins(text: str) -> dict[str, str]:
    out = {}
    for name in ('pymupdf', 'pymupdf_layout', 'pymupdf4llm'):
        match = re.search(
            rf'^\s*["\']?{name}\s*==\s*([^\s"\']+)', text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        assert match, f'{name} is not exact-pinned'
        out[name] = match.group(1)
    return out


def test_conda_fallback_pdf_trio_matches_requirements_exactly():
    requirements = _pins((ROOT / 'requirements.txt').read_text(encoding='utf-8'))
    installer = (ROOT / 'install.sh').read_text(encoding='utf-8')
    start = installer.index('PDF_STACK_PKGS=(')
    end = installer.index('\n)', start) + 2
    assert _pins(installer[start:end]) == requirements
    assert '_safe_pip_install --upgrade "${PDF_STACK_PKGS[@]}"' in installer, (
        'the exact trio exists as comments/data but is never installed')


def test_both_install_backends_run_the_functional_pdf_verifier():
    installer = (ROOT / 'install.sh').read_text(encoding='utf-8')
    assert installer.count('scripts/verify_pdf_stack.py') >= 3, (
        'uv and conda paths must both execute the verifier and surface its '
        'manual recovery command')
    uv_section = installer[installer.index('_try_uv_install()'):]
    uv_section = uv_section[:uv_section.index('# rg / fd')]
    assert 'verify_pdf_stack.py' in uv_section
    conda_section = installer[installer.index('# ── Harmonize the exact PDF trio'):]
    assert 'verify_pdf_stack.py' in conda_section


def test_functional_verifier_passes_current_environment():
    from scripts.verify_pdf_stack import verify_pdf_stack

    versions = verify_pdf_stack()
    assert len(set(versions.values())) == 1
