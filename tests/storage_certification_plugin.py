"""Fail closed when an explicitly selected storage certification is weakened.

Loaded only by the certification runner. Ordinary developer pytest selections
remain unaffected. The node inventory and outcomes are retained as evidence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


class CertificationEvidence:
    def __init__(self):
        self.collected = []
        self.deselected = []
        self.reports = []

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_runtest_logreport(self, report):
        self.reports.append({'nodeid': report.nodeid, 'when': report.when,
                             'outcome': report.outcome,
                             'xfail': hasattr(report, 'wasxfail')})

    def pytest_sessionfinish(self, session, exitstatus):
        data = {'collected': self.collected, 'deselected': self.deselected,
                'reports': self.reports, 'pytest_exitstatus': int(exitstatus)}
        target = os.environ.get('TOFU_STORAGE_CERTIFICATION_EVIDENCE')
        if target:
            Path(target).write_text(json.dumps(data, indent=2) + '\n')
        if (not self.collected or self.deselected
                or any(r['outcome'] != 'passed' or r['xfail'] for r in self.reports)):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_configure(config):
    config.pluginmanager.register(CertificationEvidence(), 'storage-certification-evidence')
