"""Memory observations retain evidence without keeping cohort record objects."""
import os
import weakref

import pytest

from benchmarks.repository_index.record_journal import RecordJournal

pytestmark = pytest.mark.unit


def test_records_are_released_before_materialization_and_file_is_closed():
    class Record(dict):
        pass
    references = []
    with RecordJournal() as journal:
        descriptor = journal._file.fileno()
        for index in range(1000):
            record = Record(index=index, values=[None, True, '代码', index * 2**64])
            references.append(weakref.ref(record))
            journal.append(record)
            del record
        assert all(reference() is None for reference in references)
        rows = journal.read()
        assert len(rows) == 1000 and rows[-1]['values'] == [None, True, '代码', 999 * 2**64]
        assert journal.receipt()['records'] == 1000
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_incomplete_journal_cannot_be_admitted():
    with RecordJournal() as journal:
        journal.append({'complete': True})
        os.ftruncate(journal._file.fileno(), 0)
        with pytest.raises(ValueError, match='incomplete'):
            journal.read()


def test_short_writes_preserve_the_record_and_failure_retires_the_file():
    class PartialWriter:
        def __init__(self, source):
            self.source = source
        def write(self, data):
            return self.source.write(data[:3])
        def __iter__(self):
            return iter(self.source)
        def __getattr__(self, name):
            return getattr(self.source, name)
    with RecordJournal() as journal:
        journal._file = PartialWriter(journal._file)
        journal.append({'values': list(range(20))})
        assert journal.read() == [{'values': list(range(20))}]
    with pytest.raises(LookupError, match='caller failure'):
        with RecordJournal() as journal:
            descriptor = journal._file.fileno()
            journal.append({'done': True})
            raise LookupError('caller failure')
    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.parametrize('post_exec', [False, True])
def test_real_memory_worker_freezes_counters_before_rebuilding_records(tmp_path, post_exec):
    import shlex
    import subprocess
    import sys
    script = '''
from types import SimpleNamespace
from benchmarks.repository_index import native_domain_worker as worker
from benchmarks.repository_index import native_discovery_measure as experiment
state = {'materialized': False}
original_read = worker.RecordJournal.read
original_usage = worker.resource.getrusage
def read(self):
    state['materialized'] = True
    return original_read(self)
def usage(kind):
    if kind == worker.resource.RUSAGE_SELF:
        return SimpleNamespace(ru_maxrss=999999 if state['materialized'] else 1234)
    return original_usage(kind)
worker.RecordJournal.read = read
worker.resource.getrusage = usage
# The executable entry imports the collector as a top-level module as well.
experiment.worker.RecordJournal.read = read
experiment.worker.resource.getrusage = usage
original_status = experiment.worker.process_status
def status(pid):
    result = original_status(pid)
    result['VmHWM'] = 999999 if state['materialized'] else 4321
    return result
experiment.worker.process_status = status
row = experiment.measure('git', 'python_extraction', 'memory', 2, transport='process', post_exec_memory=POST_EXEC)
assert state['materialized']
assert row['parent_peak_rss_bytes'] == (4321 if POST_EXEC else 1234 * 1024)
assert row['process_lifetime_peak_rss_bytes'] == 1234 * 1024
assert row['clock_records_materialized_after_memory_counters']
assert row['clock_record_journal']['records'] == 3
assert row['child_records_materialized_after_worker']
assert row['child_record_journal']['records'] == 24
assert len(row['raw_clocks']) == 2 and len(row['child_memory_receipts']) == 24
assert row['joined_cleanup'] and row['audit_queue_settled']
'''
    script = script.replace('POST_EXEC', repr(post_exec))
    launcher = shlex.split(os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable))
    result = subprocess.run([*launcher, '-c', script], capture_output=True, text=True, timeout=60,
                            env=dict(os.environ, TOFU_DATA_DIR=str(tmp_path / 'data')))
    assert result.returncode == 0, result.stdout + result.stderr


def test_concurrent_records_are_complete():
    from concurrent.futures import ThreadPoolExecutor
    with RecordJournal() as journal:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda index: journal.append({'index': index}), range(500)))
        assert sorted(row['index'] for row in journal.read()) == list(range(500))


def test_cleanup_error_does_not_replace_measurement_error():
    class FailingClose:
        def __init__(self, source):
            self.source = source
        def close(self):
            self.source.close()
            raise OSError('injected close failure')
    primary = LookupError('measurement failed')
    with pytest.raises(LookupError) as caught:
        with RecordJournal() as journal:
            descriptor = journal._file.fileno()
            journal._file = FailingClose(journal._file)
            raise primary
    assert caught.value is primary
    assert any('journal cleanup failed: OSError' in note for note in primary.__notes__)
    with pytest.raises(OSError):
        os.fstat(descriptor)
