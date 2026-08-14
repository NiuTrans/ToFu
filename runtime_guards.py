"""Dependency-light process policies that must run before optional imports.

This module intentionally uses only the Python standard library.  Importing a
``lib.*`` helper first executes ``lib/__init__.py`` and is therefore too late
for policies that protect the very beginning of ``server.py`` / healthcheck /
pytest collection.
"""

from __future__ import annotations

import os
import sys

__all__ = ['install_pymupdf_classic_policy']


def _env_true(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in {
        '1', 'true', 'yes', 'on',
    }


def install_pymupdf_classic_policy() -> bool:
    """Keep pymupdf4llm on its supported classic Markdown implementation.

    pymupdf4llm automatically activates the optional ``pymupdf.layout``
    backend merely because that module is installed.  In Tofu's supported
    dependency set the backend is not usable: its OCR adapter expects the old
    ``RapidOCR.text_detector`` API, while current RapidOCR exposes
    ``text_det``.  Tofu and tofu-search consequently call the classic
    ``helpers.pymupdf_rag`` implementation explicitly.

    Letting the unused layout backend activate is still expensive: it creates
    ONNX sessions at import time, retaining a host-sized native thread pool and
    tens of MiB before a PDF is opened.  Marking the optional submodule as
    unavailable makes pymupdf4llm select its own documented classic fallback;
    PyMuPDF and pymupdf4llm remain available.

    Set ``TOFU_ENABLE_PYMUPDF_LAYOUT=1`` to opt back into the upstream layout
    backend for controlled compatibility experiments.  The policy is
    idempotent.  It deliberately refuses to replace a backend that was already
    imported, because mutating a live module would be unsafe (and its ONNX
    sessions would already exist).

    Returns True when the classic policy is active, False when explicitly
    opted out or installed too late.
    """
    if _env_true('TOFU_ENABLE_PYMUPDF_LAYOUT'):
        return False

    module_name = 'pymupdf.layout'
    if module_name in sys.modules:
        return sys.modules[module_name] is None

    # A None entry is Python's standard import blocker: ``import
    # pymupdf.layout`` raises ModuleNotFoundError, which pymupdf4llm already
    # catches to select ``use_layout(False)``.
    sys.modules[module_name] = None
    return True
