"""Cross-test isolation: known polluters leave no process-global state behind.

Every case below once left process-global state for the tests that followed
it on its pytest worker, so their results depended on xdist assignment:

* the explicit ``TOFU_NUMERIC_THREADS`` spec exported four numeric-library
  ceilings through ``os.environ``; ``monkeypatch.delenv`` recorded nothing for
  the absent names, so undo kept them;
* the preflight rejection spec left ``TOFU_EPHEMERAL_PREFLIGHT_TIMEOUT=1``;
* every self-hosted ephemeral mint registered its host's proxy exemption for
  the process lifetime (registry and ``no_proxy``), even when the pre-flight
  probe rejected the mint; disposal now returns a lease;
* the child route-ownership spec bound the child's conversation affinity on
  the test thread and cleared only the provider pin, so a later research
  worker spec found a foreign affinity where it requires none.

The spec reruns the cases in one dedicated, serial pytest session with
``tests.support.process_state_probe`` and requires each case's teardown to
return the watched state (environment, proxy bypass registry, provider pin and
conversation affinity) to what its setup saw. The research worker spec runs
last as the observable consequence of a leaked affinity.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from tests.meta.test_configured_slot_switch_isolation import (
    _standalone_session_environment,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_POLLUTERS = (
    'tests/tasks_pkg/test_worker_budget_defaults.py::'
    'test_server_numeric_threads_explicit_operator_ceiling_is_honored',
    'tests/llm_dispatch/test_ephemeral_slot.py::EphemeralSlotTest::'
    'test_preflight_rejects_unreachable_self_hosted',
    'tests/llm_dispatch/test_ephemeral_slot.py::EphemeralSlotTest::'
    'test_mint_and_dispose',
    'tests/llm_dispatch/test_ephemeral_slot.py::EphemeralSlotTest::'
    'test_private_host_bypasses_proxy_only_while_minted',
    'tests/providers/test_provider_pin.py::ProviderPinTest::'
    'test_ephemeral_provider_id_unique_per_handle',
    'tests/tasks_pkg/test_child_invocation.py::'
    'test_native_child_owns_route_after_parent_settles',
)
_AFFINITY_CONSUMER = ('tests/paper/test_paper_ideate_cost_bounds.py::'
                      'test_research_worker_uses_task_scoped_dispatch_affinity')
# The leaks above happened only when these were absent before the polluter.
_ABSENT_BEFORE_POLLUTERS = (
    'OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
    'NUMEXPR_NUM_THREADS', 'TOFU_NUMERIC_THREADS',
    'TOFU_EPHEMERAL_PREFLIGHT', 'TOFU_EPHEMERAL_PREFLIGHT_TIMEOUT',
)


def _state_changes(before: dict, after: dict) -> dict:
    environ_before, environ_after = before['environ'], after['environ']
    changes = {
        f'environ[{name}]': (environ_before.get(name), environ_after.get(name))
        for name in sorted(set(environ_before) | set(environ_after))
        if environ_before.get(name) != environ_after.get(name)
    }
    changes.update({
        name: (value, after[name]) for name, value in before.items()
        if name != 'environ' and after[name] != value
    })
    return changes


def test_known_polluters_restore_process_state(tmp_path):
    report_path = tmp_path / 'process-state.json'
    environment = _standalone_session_environment()
    for name in _ABSENT_BEFORE_POLLUTERS:
        environment.pop(name, None)
    completed = subprocess.run(
        [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider',
         '-p', 'no:randomly', '-n', '0', '-q',
         '-p', 'tests.support.process_state_probe',
         f'--process-state-report={report_path}',
         # Importing the server exports its native library search path.
         '--process-state-preimport=server',
         *_POLLUTERS, _AFFINITY_CONSUMER],
        cwd=_ROOT, env=environment, capture_output=True, text=True)

    output = completed.stdout[-6000:] + completed.stderr[-2000:]
    assert completed.returncode == 0, output
    records = {row['nodeid']: row
               for row in json.loads(report_path.read_text(encoding='utf-8'))}
    assert set(records) == {*_POLLUTERS, _AFFINITY_CONSUMER}, output
    leaks = {nodeid: _state_changes(records[nodeid]['before'],
                                    records[nodeid]['after'])
             for nodeid in _POLLUTERS}
    assert not any(leaks.values()), leaks
