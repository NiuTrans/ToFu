"""Concurrent first imports through different tofu_search entry points complete.

A host may import the package facade on one thread while another thread first
imports a submodule directly. Tofu's readiness import of its search bridge once
raced a task thread's ``from tofu_search.search.vertical import describe_domains``
and both threads received a partially initialized module.

Each case runs in a fresh interpreter. The facade thread is held as the named
module starts executing: ``tofu_search.config`` (no subpackage loaded yet),
``tofu_search.fetch`` (``tofu_search.search`` not yet imported, the window of
that incident) or ``tofu_search.search`` (its outer package locks owned). It
resumes once the other entry point has finished or waits for a lock the facade
thread owns; both imports must then succeed. Entries are the host's direct
submodule imports plus a direct engine import. ``test_import_graph.py`` states
the structural rules.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_CHILD = r'''
import importlib._bootstrap as bootstrap
import importlib.abc
import sys
import threading
import time
import traceback

HOLD, ENTRY = sys.argv[1], sys.argv[2]
held = threading.Event()
release = threading.Event()
outcome = {}


class _HoldingLoader:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        held.set()
        release.wait(120)
        self._inner.exec_module(module)


class HoldFacadeThread(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != HOLD or threading.current_thread().name != 'facade':
            return None
        for finder in sys.meta_path:
            if finder is not self and hasattr(finder, 'find_spec'):
                spec = finder.find_spec(fullname, path, target)
                if spec is not None:
                    spec.loader = _HoldingLoader(spec.loader)
                    return spec
        return None


def run(name, statement):
    try:
        exec(statement, {})
        outcome[name] = 'OK'
    except BaseException as error:
        outcome[name] = ''.join(traceback.format_exception(error))


def waits_for(thread, owner):
    """CPython's deadlock-detection state: is ``thread`` blocked on ``owner``'s module lock?"""
    locks = (getattr(bootstrap, '_blocking_on', None) or {}).get(thread.ident) or ()
    if not isinstance(locks, list):
        locks = [locks]
    return any(getattr(lock, 'owner', None) == owner.ident for lock in list(locks))


sys.meta_path.insert(0, HoldFacadeThread())
facade = threading.Thread(target=run, args=('facade', 'import tofu_search'), name='facade')
facade.start()
if not held.wait(120):
    sys.exit(f'facade never reached {HOLD}')
entry = threading.Thread(target=run, args=('entry', ENTRY), name='entry')
entry.start()
deadline = time.monotonic() + 60
while entry.is_alive() and not waits_for(entry, facade) and time.monotonic() < deadline:
    time.sleep(0.01)
release.set()
for thread in (facade, entry):
    thread.join(120)
    if thread.is_alive():
        print(thread.name.upper(), 'HUNG', flush=True)
        import os
        os._exit(3)
for name in ('facade', 'entry'):
    print(name.upper(), 'OK' if outcome[name] == 'OK' else 'FAILED')
    if outcome[name] != 'OK':
        sys.stderr.write(outcome[name])
facade_module = sys.modules.get('tofu_search')
print('FACADE-COMPLETE', callable(getattr(facade_module, 'describe_domains', None)))
'''


_CASES = [
    ('tofu_search.fetch', 'from tofu_search.search.vertical import describe_domains'),
    ('tofu_search.fetch', 'from tofu_search.fetch.http_response import read_response_body'),
    ('tofu_search.search', 'from tofu_search.search.vertical import describe_domains'),
    ('tofu_search.search', 'from tofu_search.search.vertical.arxiv import build_query'),
    ('tofu_search.search', 'from tofu_search.verify import verify_references'),
    ('tofu_search.search', 'from tofu_search.search.orchestrator import perform_web_search'),
    ('tofu_search.search', 'from tofu_search.search.engine_selection import select_engines'),
    ('tofu_search.search', 'from tofu_search.search.ranking_phase import rank_search_candidates'),
    ('tofu_search.search', 'from tofu_search.search.diagnostics import attach_search_diagnostics'),
    ('tofu_search.search', 'from tofu_search.search.candidates import CandidateAccumulator'),
    ('tofu_search.fetch', 'from tofu_search.search.engines import xhs'),
    ('tofu_search.search', 'from tofu_search.search.engines.ddg import parse_ddg_html_text'),
    ('tofu_search.config',
     'from tofu_search.fetch.content_filter import filter_web_contents_batch'),
]


@pytest.mark.parametrize(('hold', 'entry'), _CASES, ids=[
    f'{hold}-{entry.split()[1]}' for hold, entry in _CASES])
def test_direct_submodule_import_concurrent_with_facade_import(hold, entry):
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, '-c', _CHILD, hold, entry], env=environment,
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr[-4000:]
    assert 'FACADE OK' in result.stdout, result.stdout + result.stderr[-4000:]
    assert 'ENTRY OK' in result.stdout, result.stdout + result.stderr[-4000:]
    assert 'FACADE-COMPLETE True' in result.stdout, result.stdout
