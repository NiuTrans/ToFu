"""Bounded event prefixes cannot prove final wire or absence of a round."""
import pytest

from tests.diagnostics.test_request_inspector_read_cache import (
    _EventListStorage, _snap, event_storage as event_storage,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_inspector(monkeypatch):
    from lib.tasks_pkg import request_inspector as ri
    ri._EVENTS_CACHE.clear()
    monkeypatch.setattr(ri, '_child_snapshot_sources', lambda _: ([], True, False))
    yield
    ri._EVENTS_CACHE.clear()


def seed_long(storage, task_id, count):
    storage.seed(task_id, [_snap(i, 1) for i in range(1, count + 1)])


@pytest.mark.parametrize('count,partial', [(9999, False), (10000, False), (10001, True)])
def test_exact_boundary_and_real_overflow(event_storage, count, partial):
    from lib.tasks_pkg import request_inspector as ri
    seed_long(event_storage, 'long', count)
    folded = ri.fold_request_log('long')
    assert folded['requestCount'] == min(count, 10000)
    assert folded['coverage'] == ('partial' if partial else 'full')
    assert 'readError' not in folded
    if partial:
        assert folded['coverageReason'] == 'event-limit'
        assert folded['eventReadBounds'] == [{'taskId': 'long', 'reason': 'event-limit',
            'afterSequence': 9999, 'structuralCount': 10000, 'scannedCount': 10000}]
    else:
        assert 'eventReadBounds' not in folded
        assert ri.get_request_payload('long', count)['roundNum'] == count


def test_cached_prefix_never_confirms_early_or_missing_round(event_storage):
    from lib.tasks_pkg import request_inspector as ri
    seed_long(event_storage, 'long', 10001)
    first = ri.fold_request_log('long');reads = event_storage.reads
    for address in [1, 9999, 10001, 20000]:
        with pytest.raises(ri.RequestInspectorReadError):
            ri.get_request_payload('long', address, fold=True)
    assert event_storage.reads == reads
    assert ri.fold_request_log('long')['eventReadBounds'] == first['eventReadBounds']
    rows, ok = ri._read_events('long')
    assert ok and ri._event_read_bound(rows, 'long') == first['eventReadBounds'][0]


def test_child_overflow_is_source_scoped(event_storage, monkeypatch):
    from lib.tasks_pkg import request_inspector as ri
    child = 'parent#agent:worker';seed_long(event_storage, child, 10001)
    event_storage.seed('parent', [_snap(1, 1)])
    monkeypatch.setattr(ri, '_child_snapshot_sources', lambda _: ([child], True, False))
    folded = ri.fold_request_log('parent')
    assert folded['coverage'] == 'partial' and folded['requestCount'] == 10001
    assert folded['eventReadBounds'][0]['taskId'] == child
    assert ri.get_request_payload('parent', 1)['taskId'] == 'parent'
    with pytest.raises(ri.RequestInspectorReadError):ri.get_request_payload(child, 1)


@pytest.mark.parametrize('child', [False, True])
def test_partial_wire_never_promotes_old_final_or_expiry(event_storage, monkeypatch, child):
    from lib.tasks_pkg import request_inspector as ri
    seed_long(event_storage, 'parent#agent:worker' if child else 'parent', 10001)
    if child:
        event_storage.seed('parent', [_snap(1, 1)])
        monkeypatch.setattr(ri, '_child_snapshot_sources', lambda _: (['parent#agent:worker'], True, False))
    monkeypatch.setattr(ri, 'list_conv_tasks', lambda *a, **k: {'tasks': [
        {'taskId': 'parent', 'turnId': 'turn', 'hasEvents': True}], 'hasMore': False})
    wire = ri.get_conv_inspector_wire('conv', user_id=1)
    assert wire['readError'] and wire['coverage'] == 'partial'
    assert wire['eventReadBounds'][0]['taskId'] == ('parent#agent:worker' if child else 'parent')
    assert wire['messages'] == ([_snap(1, 1)['messages'][0]] if child else [])
    assert not any(row.get('expired') for row in wire['turns'] + wire['sections'])
    assert not any(row['type'] == 'agent' for row in wire['sections'])


def test_scan_boundary_is_not_complete_when_filter_is_ignored(monkeypatch):
    from lib.tasks_pkg import request_inspector as ri
    import lib.storage
    calls=[]
    class NoiseStorage:
        def query(self, operation, args, **kwargs):
            calls.append(args)
            first=args['after_sequence'] + 1
            if first >= 200001:return []
            return [{'sequence': sequence, 'event': {'type': 'delta'}, 'created_at_ms': 0}
                    for sequence in range(first, min(200001, first + args['limit']))]
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda: NoiseStorage())
    folded=ri.fold_request_log('noise')
    assert folded['eventsAvailable'] is False and folded['requests'] == []
    assert folded['coverageReason'] == 'scan-limit'
    assert folded['eventReadBounds'][0]['scannedCount'] == 200000
    assert len(calls)==201 and calls[-1]['limit']==1


def test_failed_lookahead_does_not_cache_a_complete_prefix(event_storage, monkeypatch):
    from lib.tasks_pkg import request_inspector as ri
    seed_long(event_storage, 'long', 10001)
    original=event_storage.query
    def query(operation, args, **kwargs):
        if args['after_sequence']==9999:raise RuntimeError('lookahead unavailable')
        return original(operation,args,**kwargs)
    monkeypatch.setattr(event_storage,'query',query)
    folded=ri.fold_request_log('long')
    assert folded['readError'] is True
    assert ('long',False) not in ri._EVENTS_CACHE
    monkeypatch.setattr(event_storage,'query',original)
    assert ri.fold_request_log('long')['coverageReason']=='event-limit'


def test_nonadvancing_page_is_read_failure(monkeypatch):
    from lib.tasks_pkg import request_inspector as ri
    import lib.storage
    class BadStorage:
        def query(self,*args,**kwargs):return [{'sequence':0,'event':_snap(1,1)}] * 1000
    monkeypatch.setattr(lib.storage,'get_storage_client',lambda:BadStorage())
    assert ri.fold_request_log('bad')['readError'] is True
