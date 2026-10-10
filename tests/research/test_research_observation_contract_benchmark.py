"""Benchmark contract preserves production projection and restores its loader."""
import pytest
from benchmarks.research_observation_contract import measure
from lib.research import observation_audit

pytestmark = pytest.mark.unit


def test_contract_benchmark_records_identity_and_restores_production_loader():
    before = observation_audit._observation_schema
    report = measure(trials=3, iterations=1)
    assert observation_audit._observation_schema is before
    assert report['equivalent'] and len(report['pairs']) == 3
    assert report['first_projection_seconds'] >= 0
    assert 'empty Python schema cache' in report['first_projection_scope']
    assert [row['order'] for row in report['pairs']] == [['fresh','cached'],['cached','fresh'],['fresh','cached']]
    assert all(len(report[key]) == 64 for key in ('harness_sha256','source_sha256','normalizer_sha256','contract_sha256','protocol_sha256'))
    assert report['protocol']['observation_format'] == observation_audit.IDENTIFIED_FORMAT


def test_contract_benchmark_restores_loader_when_projection_fails(monkeypatch):
    before = observation_audit._observation_schema
    original = observation_audit.observation_contract
    calls = [0]
    def broken(protocol):
        calls[0] += 1
        if calls[0] == 2: raise RuntimeError('injected projection failure')
        return original(protocol)
    monkeypatch.setattr(observation_audit, 'observation_contract', broken)
    with pytest.raises(RuntimeError, match='injected'):
        measure(trials=3, iterations=1)
    assert observation_audit._observation_schema is before


def test_benchmark_records_shared_probe_and_preserves_unknown_resources(monkeypatch):
    from types import SimpleNamespace
    import runtime_guards
    calls = []
    resources = {'host_cpu_count':64, 'effective_cpu_count':8,
                 'effective_memory_capacity_mb':None, 'disk_free_mb':123456}
    def probe():
        calls.append(True)
        return SimpleNamespace(as_dict=lambda:dict(resources))
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', probe)
    report = measure(trials=3, iterations=1)
    assert report['harness_version'] == 3
    assert report['launch_resources'] == resources
    assert calls == [True]


def test_benchmark_first_projection_starts_with_empty_schema_cache(monkeypatch):
    original = observation_audit.observation_contract
    observation_audit._observation_schema()
    entries = []
    def observed(protocol):
        if not entries: entries.append(observation_audit._observation_schema.cache_info().currsize)
        return original(protocol)
    monkeypatch.setattr(observation_audit, 'observation_contract', observed)
    measure(trials=3, iterations=1)
    assert entries == [0]
