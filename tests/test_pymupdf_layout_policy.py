"""Regression coverage for the import-time PyMuPDF layout resource storm."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[1]


def _run(code: str, **env_overrides) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop('TOFU_ENABLE_PYMUPDF_LAYOUT', None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        check=False,
    )


def test_default_policy_blocks_only_the_optional_layout_submodule():
    proc = _run(
        "import sys; "
        "from runtime_guards import install_pymupdf_classic_policy as f; "
        "print(f(), 'pymupdf.layout' in sys.modules, "
        "sys.modules.get('pymupdf.layout'))"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == 'True True None'


def test_explicit_layout_opt_in_leaves_import_state_untouched():
    proc = _run(
        "import sys; "
        "from runtime_guards import install_pymupdf_classic_policy as f; "
        "print(f(), 'pymupdf.layout' in sys.modules)",
        TOFU_ENABLE_PYMUPDF_LAYOUT='1',
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == 'False False'


def test_tofu_search_import_keeps_classic_pdf_support_without_onnx_pool():
    proc = _run(
        "from runtime_guards import install_pymupdf_classic_policy as f; f(); "
        "import tofu_search; "
        "from tofu_search.fetch import pdf_extract as p; "
        "s={x.split(':',1)[0]:x.split(':',1)[1].strip() "
        "for x in open('/proc/self/status') if ':' in x}; "
        "print(p.HAS_PYMUPDF, p.HAS_PYMUPDF4LLM, "
        "callable(p._to_markdown_classic), s.get('Threads'))"
    )
    assert proc.returncode == 0, proc.stderr
    assert 'pthread_setaffinity_np failed' not in proc.stderr
    fields = proc.stdout.strip().split()
    assert fields[-4:-1] == ['True', 'True', 'True']
    # No host-sized native pool should be created merely by importing search.
    assert int(fields[-1]) <= 8
