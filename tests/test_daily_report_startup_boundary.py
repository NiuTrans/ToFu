"""Executable contracts for request-loaded daily-report services."""

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
def test_package_import_keeps_daily_report_services_dormant():
    proc = _run_isolated(
        'import sys; import lib.daily_report as daily; '
        'print("DAILY-PACKAGE", len(daily.__all__), '
        'set(daily.__all__) == set(daily._EXPORT_MODULES), '
        '"lib.daily_report.storage" in sys.modules, '
        '"lib.daily_report.cost" in sys.modules, '
        '"lib.daily_report.conversations" in sys.modules, '
        '"lib.daily_report.scheduler" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'DAILY-PACKAGE 51 True False False False False' in proc.stdout


@pytest.mark.unit
def test_storage_export_does_not_initialize_analysis_or_scheduler():
    proc = _run_isolated(
        'import sys; import lib.daily_report as daily; '
        'valid = daily._is_report_date("2026-08-27"); '
        'from lib.daily_report import storage; '
        'print("DAILY-STORAGE", valid, storage.__name__, '
        '"lib.daily_report.storage" in sys.modules, '
        '"lib.daily_report.cost" in sys.modules, '
        '"lib.daily_report.conversations" in sys.modules, '
        '"lib.daily_report.llm" in sys.modules, '
        '"lib.daily_report.scheduler" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert (
        'DAILY-STORAGE True lib.daily_report.storage True False False False False'
        in proc.stdout
    )


@pytest.mark.unit
def test_server_boot_keeps_daily_report_business_modules_dormant():
    proc = _run_isolated(
        'import sys; import server; '
        'print("SERVER-DAILY", "routes.api_v1.daily_report" in sys.modules, '
        '"lib.daily_report.storage" in sys.modules, '
        '"lib.daily_report.cost" in sys.modules, '
        '"lib.daily_report.todos" in sys.modules, '
        '"lib.daily_report.conversations" in sys.modules, '
        '"lib.daily_report.llm" in sys.modules, '
        '"lib.daily_report.generator" in sys.modules, '
        '"lib.daily_report.scheduler" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'SERVER-DAILY True False False False False False False False' \
        in proc.stdout
