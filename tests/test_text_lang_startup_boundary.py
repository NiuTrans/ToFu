"""Executable contracts for request-loaded text-language analysis."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_isolated(source: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != 'LD_PRELOAD'}
    return subprocess.run(
        [sys.executable, '-c', source], cwd=_REPO, env=env, timeout=240,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@pytest.mark.unit
def test_review_package_import_keeps_owners_dormant():
    proc = _run_isolated(
        'import sys; import lib.paper.review as review; '
        'print("REVIEW-PACKAGE", len(review.__all__), '
        'set(review.__all__) == set(review._EXPORT_MODULES), '
        '"lib.paper.review._lang" in sys.modules, '
        '"lib.paper.review._prompts" in sys.modules, '
        '"lib.paper.review._textproc" in sys.modules, '
        '"lib.text_lang" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'REVIEW-PACKAGE 23 True False False False False' in proc.stdout


@pytest.mark.unit
def test_language_key_export_does_not_load_text_processing():
    proc = _run_isolated(
        'import sys; import lib.paper.review as review; '
        'parsed = review.parse_report_lang("review:acl:zh"); '
        'print("REVIEW-LANG", parsed["kind"], parsed["venue"], '
        'parsed["ui_lang"], '
        '"lib.paper.review._lang" in sys.modules, '
        '"lib.paper.review._prompts" in sys.modules, '
        '"lib.paper.review._textproc" in sys.modules, '
        '"lib.text_lang" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'REVIEW-LANG review acl zh True False False False' in proc.stdout


@pytest.mark.unit
def test_server_boot_keeps_text_language_cascade_dormant():
    proc = _run_isolated(
        'import sys; import server; '
        'print("SERVER-TEXT-LANG", '
        '"routes.api_v1.logs" in sys.modules, '
        '"lib.paper.review._lang" in sys.modules, '
        '"lib.paper.review._prompts" in sys.modules, '
        '"lib.paper.review._textproc" in sys.modules, '
        '"lib.text_lang" in sys.modules, '
        '"lib.text_lang._fasttext" in sys.modules, '
        '"lib.text_lang._detect" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'SERVER-TEXT-LANG True True True False False False False' in proc.stdout
