"""Pytest plugin: record the process-global state each test leaves behind.

A dedicated serial session loads it with
``-p tests.support.process_state_probe --process-state-report=PATH``. For
every test the JSON report holds the watched state before its setup and after
its teardown, so a spec can name exactly what a test left for the next test
on its worker: the process environment, the proxy bypass registry, and the
running thread's provider pin and conversation affinity.

``--process-state-preimport=MODULE[,MODULE...]`` imports modules whose import
publishes process defaults by design (``server`` exports its native library
search path) before the first test, so that publication is not reported as
the first importing test's leak.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

# The pytest runner sets this for the duration of each test phase.
_RUNNER_OWNED_ENVIRONMENT = frozenset({'PYTEST_CURRENT_TEST'})
_records: list[dict] = []


def pytest_addoption(parser):
    group = parser.getgroup('process-state-probe')
    group.addoption('--process-state-report', default='',
                    help='Write per-test process-state snapshots to this JSON file.')
    group.addoption('--process-state-preimport', default='',
                    help='Comma-separated modules to import before the first test.')


def pytest_sessionstart(session):
    # Project modules load here, after tests/conftest.py isolated the worker.
    raw = session.config.getoption('--process-state-preimport')
    for module in filter(None, (name.strip() for name in raw.split(','))):
        importlib.import_module(module)


def _watched_state() -> dict:
    from lib import proxy
    from lib.llm_dispatch.conv_affinity import get_conv_affinity
    from lib.llm_dispatch.provider_pin import get_pinned_provider

    with proxy._lock:
        proxy_bypass = {'registered': sorted(proxy._registered_hosts),
                        'leased': dict(sorted(proxy._host_leases.items()))}
    return {
        'environ': {name: value for name, value in os.environ.items()
                    if name not in _RUNNER_OWNED_ENVIRONMENT},
        'proxy_bypass': proxy_bypass,
        'provider_pin': get_pinned_provider(),
        'conv_affinity': get_conv_affinity(),
    }


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    before = _watched_state()
    yield
    _records.append({'nodeid': item.nodeid, 'before': before,
                     'after': _watched_state()})


def pytest_sessionfinish(session):
    report = session.config.getoption('--process-state-report')
    if report:
        Path(report).write_text(json.dumps(_records), encoding='utf-8')
