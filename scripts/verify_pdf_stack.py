#!/usr/bin/env python3
"""Verify that the installed PDF stack matches Tofu's pinned contract.

An import-only check is too weak: PyMuPDF4LLM can be present while its exact
PyMuPDF/Layout companions are split across versions, and the first real paper
then fails far away from installation.  This script verifies the pins declared
in ``requirements.txt`` and performs one in-memory Markdown extraction through
the classic implementation Tofu actually uses.
"""

from __future__ import annotations

import importlib.metadata
import contextlib
import io
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / 'requirements.txt'
PACKAGES = ('pymupdf', 'pymupdf_layout', 'pymupdf4llm')
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _declared_pins() -> dict[str, str]:
    text = REQUIREMENTS.read_text(encoding='utf-8')
    pins: dict[str, str] = {}
    for package in PACKAGES:
        match = re.search(
            rf'^\s*{re.escape(package)}\s*==\s*([^\s#]+)', text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if not match:
            raise RuntimeError(
                f'{REQUIREMENTS.name} must exact-pin {package} with ==')
        pins[package] = match.group(1)
    return pins


def verify_pdf_stack() -> dict[str, str]:
    """Raise on a missing/mismatched/non-functional stack; return versions."""
    pins = _declared_pins()
    installed: dict[str, str] = {}
    for package, expected in pins.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(f'{package} is not installed') from exc
        if actual != expected:
            raise RuntimeError(
                f'{package} version mismatch: expected {expected}, got {actual}')
        installed[package] = actual

    # Must run before importing pymupdf4llm: Tofu deliberately uses its classic
    # path, not the optional ONNX layout backend.
    from runtime_guards import install_pymupdf_classic_policy
    install_pymupdf_classic_policy()

    import pymupdf
    # The helper emits an upstream promotional message on import even though
    # the classic policy is intentional. Keep install output actionable.
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        from pymupdf4llm.helpers import pymupdf_rag

    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), 'Tofu PDF stack installation check')
        chunks = pymupdf_rag.to_markdown(
            doc, pages=[0], page_chunks=True, show_progress=False,
            table_strategy='lines_strict', hdr_info=False,
        )
        text = (chunks[0].get('text', '')
                if chunks and isinstance(chunks[0], dict) else '')
        if 'Tofu PDF stack installation check' not in text:
            raise RuntimeError('Markdown smoke extraction returned no test text')
    finally:
        doc.close()
    return installed


def main() -> int:
    try:
        versions = verify_pdf_stack()
    except Exception as exc:
        print(f'PyMuPDF stack FAILED: {type(exc).__name__}: {exc}',
              file=sys.stderr)
        return 1
    joined = ', '.join(f'{name}={version}'
                       for name, version in versions.items())
    print(f'PyMuPDF stack OK: {joined}; Markdown smoke passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
