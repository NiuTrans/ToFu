"""Executable form of the owner decision "hardware never limits work" (2026-10-24).

Authority: AGENTS.md design principle 3 and docs/RESOURCE_CONTROL.md section
"Hardware never limits work". CPU count, affinity/cgroup CPU quota, memory
capacity or headroom, process RSS and cgroup pressure never size, cap, queue,
refuse, shrink, recycle or kill user work. The launch probe may only size
evict-only retention (caches, TTLs, logs, replay windows, disk budgets).

Every test here guards a limit that previously existed and was reintroduced
by successive "scale to the machine" iterations: probe-derived worker and
task ceilings, an RSS-derived task ceiling, memory-headroom request refusal
that shrank context and ended turns as ``server_busy``, in-process and
lifecycle-manager RSS recycling that aborted every running task, and a
memory-pressure feedback controller; a packaged search reranker later regrew a
CPU/memory call gate, memory refusal and RSS kill. Weakening a test needs a new
explicit owner decision recorded in the authority doc, never an agent's
judgment.
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
from pathlib import Path

import pytest

import runtime_guards
from runtime_guards import (
    MAX_TASK_CONCURRENCY,
    UNLIMITED,
    SystemResourceSnapshot,
    deployment_resource_default,
    resolve_resource_budget,
    resource_budget_manifest,
    task_concurrency_hard_ceiling,
)
from tests._source_scan import strip_comments


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = json.loads(
    (ROOT / 'contracts/resource_policy_v1.json').read_text(encoding='utf-8'),
)['launch_settings']

# ``unlimited``  admission, concurrency and fan-out ceilings; always UNLIMITED.
# ``fixed``      hardware-independent contract constants: idle-reuse hints of
#                demand-driven pools (never an execution ceiling), retry
#                budgets, deadlines and measured tuning windows.
# ``retention``  evict-only caches, TTLs, logs, replay and disk budgets; the
#                only class that may follow the launch probe.
CLASSES = frozenset({'unlimited', 'fixed', 'retention'})
HARDWARE_INDEPENDENT = frozenset({'unlimited', 'fixed'})

MODES = {
    'personal': {},
    'distributed': {'TOFU_DEPLOYMENT_MODE': 'distributed'},
}


def _host(cpus: int, capacity_mb: int | None,
          available_mb: int | None) -> SystemResourceSnapshot:
    return SystemResourceSnapshot(
        host_cpu_count=cpus,
        affinity_cpu_count=cpus,
        cgroup_cpu_count=None,
        effective_cpu_count=cpus,
        host_memory_total_mb=capacity_mb,
        host_memory_available_mb=available_mb,
        cgroup_memory_limit_mb=None,
        cgroup_memory_current_mb=None,
        effective_memory_capacity_mb=capacity_mb,
        effective_memory_available_mb=available_mb,
        disk_total_mb=500 * 1024,
        disk_free_mb=100 * 1024,
    )


HOSTS = {
    'probe_failed': _host(1, None, None),
    'tiny': _host(1, 512, 128),
    'laptop': _host(4, 8 * 1024, 4 * 1024),
    'huge': _host(512, 4 * 1024 * 1024, 3 * 1024 * 1024),
}


def _refuse_probe(*_args, **_kwargs):
    raise AssertionError(
        'an unlimited or fixed launch default consulted the hardware probe; '
        'only evict-only retention may (docs/RESOURCE_CONTROL.md, '
        '"Hardware never limits work")')


def _names(*classes: str) -> list[str]:
    return sorted(name for name, setting in SETTINGS.items()
                  if setting.get('class') in classes)


def test_every_launch_setting_declares_its_limit_class():
    undeclared = sorted(name for name, setting in SETTINGS.items()
                        if setting.get('class') not in CLASSES)
    assert not undeclared, (
        'each launch setting must declare class unlimited, fixed or '
        f'retention: {undeclared}')


# A setting shaped like a concurrency or admission knob can never be filed as
# retention, the only class allowed to follow the hardware probe.
_EXECUTION_SHAPED = re.compile(
    r'(_WORKERS|_PARALLEL|_FANOUT|_PROCESSES|_THREADS|_INFLIGHT_TASKS'
    r'|_CONCURRENCY|_JOBS|_POOL|_ARENA_MAX|_PER_PRINCIPAL|_LIVE_CAP'
    r'|_PER_WAVE|_PER_SESSION|_RPC_CAPACITY|_QUEUE_CAPACITY)$')


def test_concurrency_shaped_settings_are_execution_classes():
    misfiled = sorted(
        name for name in SETTINGS
        if _EXECUTION_SHAPED.search(name)
        and SETTINGS[name].get('class') not in HARDWARE_INDEPENDENT)
    assert not misfiled, misfiled


def test_unlimited_settings_are_declared_unlimited_in_every_mode():
    wrong = sorted(
        name for name in _names('unlimited')
        if SETTINGS[name]['defaults'] != {
            'personal': 'unlimited', 'distributed': 'unlimited'})
    assert not wrong, wrong
    assert runtime_guards.UNLIMITED_SETTINGS == frozenset(_names('unlimited'))


@pytest.mark.parametrize('mode', sorted(MODES))
def test_execution_defaults_never_consult_the_hardware_probe(monkeypatch, mode):
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', _refuse_probe)
    environment = dict(MODES[mode])
    for name in _names('unlimited'):
        assert deployment_resource_default(name, environment) == UNLIMITED, name
        assert resolve_resource_budget(
            name, environment, maximum=UNLIMITED) == UNLIMITED, name
    for name in _names('fixed'):
        expected = SETTINGS[name]['defaults'][mode]
        assert deployment_resource_default(name, environment) == expected, name


@pytest.mark.parametrize('mode', sorted(MODES))
def test_launch_manifest_execution_values_are_identical_on_every_host(mode):
    manifests = {
        label: resource_budget_manifest(dict(MODES[mode]), snapshot=host)['defaults']
        for label, host in HOSTS.items()
    }
    varying = sorted(
        name for name in SETTINGS
        if SETTINGS[name]['class'] in HARDWARE_INDEPENDENT
        and len({defaults[name] for defaults in manifests.values()}) != 1)
    assert not varying, varying


@pytest.mark.parametrize('mode', sorted(MODES))
def test_no_launch_default_depends_on_cpu_count(mode):
    one, many = (
        resource_budget_manifest(
            dict(MODES[mode]),
            snapshot=_host(cpus, 64 * 1024, 48 * 1024))['defaults']
        for cpus in (1, 512))
    assert {name for name in one if one[name] != many[name]} == set()


@pytest.mark.parametrize('environment', [
    {},
    {'TOFU_DEPLOYMENT_MODE': 'distributed'},
    # Retired RSS knobs left in an old environment must not shrink it.
    {'TOFU_PROCESS_RSS_RECYCLE_MB': '512', 'TOFU_TASK_RSS_RESERVE_MB': '4096'},
])
def test_task_concurrency_ceiling_is_the_protocol_constant(monkeypatch, environment):
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', _refuse_probe)
    assert task_concurrency_hard_ceiling(environment) == MAX_TASK_CONCURRENCY


def test_memory_cannot_refuse_shrink_or_recycle_work():
    import lib.cgroup_guard as cgroup_guard
    import lib.resource_control.runtime as resource_runtime
    import server_manager
    from lib.resource_control.controller import ResourceController

    for name in ('MemoryPressureError', 'check_request_headroom',
                 'approx_body_bytes', 'process_rss_admission_snapshot'):
        assert not hasattr(cgroup_guard, name), name
    for function in (cgroup_guard.start_monitor, cgroup_guard.run_monitor_once):
        assert 'recycle_callback' not in inspect.signature(function).parameters
    assert not hasattr(server_manager, 'worker_rss_recycle_limit_bytes')
    tick = inspect.signature(ResourceController.tick).parameters
    assert 'memory_pressure' not in tick and 'headroom_bytes' not in tick
    assert not hasattr(resource_runtime, 'memory_observation')


def _tracked(*pathspecs: str) -> list[str]:
    output = subprocess.check_output(
        ['git', 'ls-files', '-z', '--', *pathspecs], cwd=ROOT)
    return sorted(path for path in output.decode().split('\0')
                  if path and (ROOT / path).is_file())


RUNTIME_SOURCES = _tracked(
    'lib/*.py', 'routes/*.py', 'tofu_agent/*.py', 'tofu_workers/*.py',
    'bootstrap_pkg/*.py', 'serverctl_pkg/*.py', 'automations/*.py',
    'runtime_guards.py', 'server.py', 'server_manager.py', 'serverctl.py',
    'supervisor.py',
    # Packages, plugins and bundles ship runtime code outside the root tree.
    'packages/*.py', 'plugins/*.py', 'bundles/*.py',
    ':(exclude)packages/*/tests/*', ':(exclude)packages/*/examples/*',
    ':(exclude)plugins/*/tests/*', ':(exclude)bundles/*/tests/*')


def _runtime_text(path: str) -> str:
    return strip_comments(
        (ROOT / path).read_text(encoding='utf-8', errors='replace'),
        lang='python')


RETIRED_KNOBS = (
    'TOFU_TASK_RSS_RESERVE_MB',
    'TOFU_PROCESS_RSS_RELIEF_MB',
    'TOFU_PROCESS_RSS_RECYCLE_MB',
    'TOFU_PROCESS_RSS_COOLDOWN_SEC',
    'TOFU_RESOURCE_PROFILE',
    'TOFU_CGROUP_REQUEST_PCT',
    'TOFU_CGROUP_REQUEST_MIN_BYTES',
    'TOFU_CGROUP_REQUEST_GUARD',
    'TOFU_ADMISSION_CGROUP_PCT',
    'TOFU_ADMISSION_PROCESS_RSS',
)


def test_memory_admission_knobs_stay_retired():
    assert not set(RETIRED_KNOBS) & set(SETTINGS)
    advertised = [ROOT / '.env.example', ROOT / 'docker-compose.yml',
                  ROOT / 'contracts/config_knobs_v1.json']
    # One read and comment strip per source, not one per knob: ten passes over
    # every runtime file on the FUSE checkout outlasted the per-test timeout.
    texts = {path: _runtime_text(path) for path in RUNTIME_SOURCES}
    hits = sorted(
        f'{path}: {knob}'
        for path, text in texts.items()
        for knob in RETIRED_KNOBS
        if knob in text
    ) + sorted(
        f'{path.relative_to(ROOT)}: {knob}'
        for path in advertised
        for knob in RETIRED_KNOBS
        if knob in path.read_text(encoding='utf-8')
    )
    assert not hits, hits


_CPU_COUNT = re.compile(
    r'\bos\.(?:process_)?cpu_count\(|\bsched_getaffinity\(|'
    r'\bmultiprocessing\.cpu_count\(|\beffective_cpu_count\b|\blogical_cpus\b')

# Each entry states why that reader does not limit work. Adding an entry is a
# policy decision: a reader that caps, queues or refuses work is never allowed.
CPU_COUNT_READERS = {
    'tofu_workers/resources.py': 'the launch probe itself',
    'serverctl.py': 'prints the probe snapshot',
    'lib/local_serve/_probe.py': 'local model server threads use every allowed core',
    'lib/onnx_thread_guard.py': 'ONNX threads use every allowed core',
    'lib/pdf_parser/_common.py': 'PDF parser threads use every allowed core',
    'lib/pdf_parser/docling.py': 'Docling threads use every allowed core',
    'lib/project_mod/worktree_checkout_policy.py': 'git checkout workers use every allowed core',
    'lib/research/source_index.py': 'sizes an evict-only cache only',
    'lib/vite_assets.py': 'frontend build check idle-thread hint',
    'lib/storage/supervisor.py': 'host-overload diagnosis for storage liveness',
    'bootstrap_pkg/runtime_repair.py': 'native build job count',
    'serverctl_pkg/native_engine.py': 'native build job count',
    'packages/tofu-search/tofu_search/generated_resources.py': 'generated copy of the launch probe',
    'packages/tofu-search/tofu_search/local_rerank.py':
        'default threads inside one scoring call; never a call gate',
    'packages/tofu-overleaf-mcp/tofu_overleaf_mcp/git_resources.py':
        'pins untrusted Git to one CPU, a fixed sandbox bound',
}


def test_cpu_count_is_read_only_by_declared_non_limiting_owners():
    readers = {path for path in RUNTIME_SOURCES
               if _CPU_COUNT.search(_runtime_text(path))}
    undeclared = sorted(readers - set(CPU_COUNT_READERS))
    assert not undeclared, (
        'CPU count must not size, cap or queue work; if this reader really '
        f'does neither, declare it with a reason: {undeclared}')
    stale = sorted(set(CPU_COUNT_READERS) - readers)
    assert not stale, f'remove stale entries: {stale}'


_MEMORY_READING = re.compile(
    r'\b(?:effective|host|cgroup)_memory_\w+_mb\b|\bvirtual_memory\(|'
    r'[\'"]MemAvailable[\'"]')

# Each entry states why that reader does not limit work. Adding an entry is a
# policy decision: memory that gates, queues, refuses or stops work never is.
MEMORY_READERS = {
    'tofu_workers/resources.py': 'the launch probe itself',
    'runtime_guards.py': 'sizes evict-only retention settings only',
    'serverctl.py': 'prints the probe snapshot',
    'bootstrap_pkg/runtime_repair.py': 'native build job count',
    'serverctl_pkg/native_engine.py': 'native build job count',
    'lib/desktop_agent/_gui.py': 'reports device memory in system info',
    'lib/desktop_agent/_runtime.py': 'RAM-versus-file spill threshold for pending output',
    'lib/evolution/receipt_validation.py': 'sizes an evict-only proof cache only',
    'lib/http_transport/request_body.py': 'RAM-versus-file spill hint for request bodies',
    'lib/local_serve/_probe.py': 'local-model placement hints, never a fit refusal',
    'lib/research/source_index.py': 'sizes an evict-only cache only',
    'lib/vite_assets.py': 'frontend build check idle-thread hint',
    'packages/tofu-search/tofu_search/generated_resources.py': 'generated copy of the launch probe',
    'packages/tofu-overleaf-mcp/tofu_overleaf_mcp/git_resources.py':
        'diagnostic report only; the untrusted-Git memory bound is fixed',
}


def test_memory_is_read_only_by_declared_non_limiting_owners():
    readers = {path for path in RUNTIME_SOURCES
               if _MEMORY_READING.search(_runtime_text(path))}
    undeclared = sorted(readers - set(MEMORY_READERS))
    assert not undeclared, (
        'memory capacity or headroom must not gate, queue, refuse or stop '
        f'work; if this reader really does neither, declare it with a reason: {undeclared}')
    stale = sorted(set(MEMORY_READERS) - readers)
    assert not stale, f'remove stale entries: {stale}'


# A new memory gate starts by importing the cgroup reader. Only the relief
# monitor's owner and the boot self-check may; neither blocks work.
CGROUP_GUARD_IMPORTERS = {
    'lib/cgroup_guard.py': 'the module itself',
    'lib/production_lifecycle.py': 'starts the best-effort relief monitor',
    'lib/server/server_shutdown.py': 'stops the relief monitor',
    'lib/file_history/store.py': 'drops page cache of files it wrote',
    'lib/motion_video/engine.py': 'drops page cache of render outputs',
    'lib/server/server_runtime_probes.py': 'stall diagnostics text only',
    'lib/local_serve/_probe.py': 'local-model placement hints, never a fit refusal',
    'serverctl.py': 'doctor diagnostics',
}
_CGROUP_GUARD_IMPORT = re.compile(
    r'^\s*(?:from\s+lib(?:\s+import\s+[^\n]*\bcgroup_guard\b|\.cgroup_guard\s+import)'
    r'|import\s+lib\.cgroup_guard\b)', re.MULTILINE)


def test_only_declared_owners_read_the_cgroup_memory_guard():
    importers = {path for path in RUNTIME_SOURCES
                 if _CGROUP_GUARD_IMPORT.search(_runtime_text(path))}
    undeclared = sorted(importers - set(CGROUP_GUARD_IMPORTERS))
    assert not undeclared, (
        'memory observations must not gate, shrink or recycle work; declare a '
        f'non-limiting reader with its reason: {undeclared}')
    stale = sorted(set(CGROUP_GUARD_IMPORTERS) - importers - {'lib/cgroup_guard.py'})
    assert not stale, f'remove stale entries: {stale}'


_CAPACITY_REFUSAL = re.compile(
    r'at capacity|too many concurrent|capacity is occupied', re.IGNORECASE)

# Refusals that are not local hardware capacity, each with its reason.
CAPACITY_MESSAGE_OWNERS = {
    'lib/llm/llm_errors.py': "classifies a provider's own 'model is at capacity' reply",
    'tofu_agent/runtime.py': 'only an explicit caller max_inflight; the default is unlimited',
    'packages/tofu-mtsso-auth/src/tofu_mtsso_auth/__init__.py':
        'fixed bound on distinct in-flight SSO exchanges, never host-derived',
}


def test_no_local_capacity_refusal_messages():
    offenders = sorted(
        path for path in RUNTIME_SOURCES
        if path not in CAPACITY_MESSAGE_OWNERS
        and _CAPACITY_REFUSAL.search(_runtime_text(path)))
    assert not offenders, offenders


def test_legacy_delegation_bound_is_optional_receipt_data():
    schema = json.loads(
        (ROOT / 'contracts/project_dispatch_v1.schema.json').read_text(
            encoding='utf-8'))
    policy = schema['$defs']['workerPolicy']
    bound = policy['properties']['maxConcurrent']
    assert 'maxConcurrent' not in policy['required']
    assert bound['deprecated'] is True
    assert bound['maximum'] == MAX_TASK_CONCURRENCY
    assert task_concurrency_hard_ceiling() - 1 <= bound['maximum']
