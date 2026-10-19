"""Measurement admission rejects drift without hiding actual library identity."""
from types import SimpleNamespace
import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from benchmarks.knowledge_html import native_ingest_provenance as provenance

pytestmark = pytest.mark.unit


def test_artifact_pin_bounds_reads_without_changing_sha256(tmp_path, monkeypatch):
    path = tmp_path/'artifact'
    content = bytes(range(256)) * 8200
    path.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()
    original = Path.open
    reads = []
    class Reader:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): return self.stream.__exit__(*args)
        def read(self, size=-1):
            assert 0 < size <= 1 << 20, 'artifact verification must bound read buffers'
            reads.append(size)
            return self.stream.read(size)
    def open_path(target, *args, **kwargs):
        stream = original(target, *args, **kwargs)
        return Reader(stream) if target == path else stream
    monkeypatch.setattr(Path, 'open', open_path)
    assert provenance.file_sha256(path) == expected
    assert len(reads) >= 3


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    source = {'digest': 'a'*64}
    monkeypatch.setattr(provenance, 'source_fingerprint', lambda _: source['digest'])
    engine = tmp_path/'tofu-db'
    engine.write_bytes(b'engine')
    engine.with_name('libtofu_db.so').write_bytes(b'library')
    return provenance.DatabaseProvenance.capture(tmp_path, engine), engine, source


@pytest.mark.parametrize('changed', ['source', 'engine', 'library'])
def test_post_cohort_check_rejects_source_or_artifact_drift(admitted, changed):
    original, engine, source = admitted
    decoded = provenance.DatabaseProvenance.from_receipt(original.receipt())
    decoded.verify(engine.parent, engine)
    if changed == 'source': source['digest'] = 'b'*64
    elif changed == 'engine': engine.write_bytes(b'changed engine')
    else: engine.with_name('libtofu_db.so').write_bytes(b'changed library')
    with pytest.raises(RuntimeError, match='changed'):
        decoded.verify(engine.parent, engine)


def test_receipt_cannot_be_reused_for_another_engine(admitted):
    original, engine, _ = admitted
    with pytest.raises(RuntimeError, match='different engine'):
        original.expected_identity(engine.parent/'other')


@pytest.mark.parametrize('pin', [None, '', 'f'*63, 'z'*64])
def test_missing_or_malformed_source_pin_refuses(admitted, pin):
    receipt = admitted[0].receipt()
    receipt['source_sha256'] = pin
    with pytest.raises(ValueError, match='SHA256'):
        provenance.DatabaseProvenance.from_receipt(receipt)


@pytest.mark.parametrize('frozen', [False, True])
@pytest.mark.parametrize('actual_source', ['a'*64, 'b'*64])
def test_each_open_checks_actual_export_and_closes_rejected_runtime(admitted, monkeypatch, frozen, actual_source):
    from benchmarks.knowledge_html import durable
    original, engine, _ = admitted
    runtimes = []

    class Runtime:
        def __init__(self, *args, **kwargs):
            self.closed = False
            runtimes.append(self)

        def build_identity(self): return {'source_sha256': actual_source}
        def close(self): self.closed = True

    def source_check(_):
        assert not frozen, 'the worker must bracket measured opens with full checks'
        return 'a'*64

    monkeypatch.setattr(durable, 'EmbeddedRuntime', Runtime)
    monkeypatch.setattr(durable, 'source_fingerprint', source_check)
    monkeypatch.setattr(durable, 'EmbeddedStorageV2Client', lambda *a, **k: SimpleNamespace(
        health=lambda **kw: dict(ready=True, backend='tofudb', resourceBudget={}), close=lambda: None))
    corpus = object.__new__(durable.DurableHtmlCorpus)
    corpus.root = engine.parent
    corpus.engine = engine
    corpus.data_dir = engine.parent/'authority'
    corpus.owner_user_id = 900071
    corpus.runtime = corpus.client = None
    corpus.database_provenance = original if frozen else None
    if actual_source != original.source_sha256:
        with pytest.raises(RuntimeError, match='not built from this checkout'):
            corpus.open()
        assert corpus.runtime is None and runtimes[-1].closed
    else:
        try:
            corpus.open()
            assert corpus.build_identity == original.expected_identity(engine)
            assert corpus.library_sha256 == original.library_sha256
        finally: corpus.close()
        assert runtimes[-1].closed


def test_memory_captures_provenance_child_waits_outside_operation_clocks(tmp_path, monkeypatch):
    from benchmarks.knowledge_html import native_ingest_worker as worker
    source = provenance.DatabaseProvenance('a'*64, '/fixture/tofu-db', 'b'*64,
                                         '/fixture/libtofu_db.so', 'c'*64)
    spawned = []
    calls = []
    timing = {'active': False}

    def verify(self, *args):
        assert not timing['active'], 'provenance must stay outside upload clocks'
        process = subprocess.Popen(['/bin/true'])
        process.wait(timeout=10)
        spawned.append(process.pid)
        return source

    monkeypatch.setattr(provenance.DatabaseProvenance, 'verify', verify)
    actual_clock = worker.clock
    def clock(operation, index):
        timing['active'] = True
        try: return actual_clock(operation, index)
        finally: timing['active'] = False
    monkeypatch.setattr(worker, 'clock', clock)

    class Workload:
        closed = False
        profile = None
        calls = 0
        db_identity = {'source_sha256': 'a'*64}

        def __init__(self, *args, **kwargs): pass
        def operation(self, index): calls.append(index); return []
        def verify(self, index, result): return 'd'*64
        def retire(self): pass
        def reopen(self): pass
        def close(self): self.closed = True

    monkeypatch.setattr(worker, 'Workload', Workload)
    pdf = tmp_path/'fixture.pdf'
    pdf.write_bytes(b'fixture')
    row = worker.measure(dict(variant='reference',case='html',mode='memory',samples=2,
        pdf=str(pdf),engine='/fixture/tofu-db',binding={'bytes': 0},
        python_command=[sys.executable],database_provenance=source.receipt()))
    assert len(spawned) == 2
    assert [receipt['pid'] for receipt in row['child_memory_receipts']] == spawned
    assert all(receipt['returncode'] == 0 for receipt in row['child_memory_receipts'])
    assert row['joined_cleanup'] and row['clock_records_materialized_after_memory_counters']
    assert len(calls) == 23 and len(row['raw_clocks']) == 2
