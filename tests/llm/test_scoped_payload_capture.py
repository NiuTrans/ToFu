"""Opt-in payload capture stays bounded, detached and context-isolated."""
import json
import threading
import pytest
from lib.llm.payload_capture import capture_provider_payloads, current_payload_capture
from tests.sse.test_sse_core_parity import _run_sync, _run_async
pytestmark = pytest.mark.unit


def scope(**changes):
    return capture_provider_payloads(owner_user_id=7,request_label='owned probe',
        max_bytes=changes.get('max_bytes',4096),max_events=changes.get('max_events',16))


@pytest.mark.parametrize('run', [_run_sync,_run_async])
def test_real_shell_observes_original_payload_without_changing_delivery(run):
    frames=[{'choices':[{'delta':{'content':'json'},'finish_reason':'stop'}]}]
    raw=json.dumps(frames[0]);wire=('data: '+raw+'\n\ndata: [DONE]\n\n').encode()
    with scope() as capture:
        result=run([],raw_chunks=[wire])
        assert result.message['content']=='json'
    snapshot=capture.snapshot()
    assert snapshot['coverage_complete']
    assert snapshot['events'][0]['data']==raw
    assert snapshot['events'][-1]['data']=='[DONE]'
    assert current_payload_capture() is None
    snapshot['events'].clear();assert capture.snapshot()['events']


@pytest.mark.parametrize('bound', ['bytes','events'])
def test_clipping_never_claims_complete_or_retains_partial_frame(bound):
    with scope(max_bytes=3 if bound=='bytes' else 4096,max_events=1) as capture:
        stream=capture.new_stream('model','trace')
        capture.record(stream,'éé' if bound=='bytes' else 'first')
        capture.record(stream,'second');capture.finish(stream,'finished',0)
    result=capture.snapshot();assert not result['coverage_complete']
    assert result['retained_bytes']<=result['max_bytes'] and len(result['events'])<=1
    if bound=='bytes':assert result['events']==[]


def test_unrelated_threads_and_unfinished_streams_are_not_complete():
    seen=[]
    with scope() as capture:
        worker=threading.Thread(target=lambda:seen.append(current_payload_capture()))
        worker.start();worker.join()
        stream=capture.new_stream('model','trace');capture.record(stream,'partial')
    assert seen==[None] and not capture.snapshot()['coverage_complete']
    before=capture.snapshot()['events'];capture.record(stream,'late')
    assert capture.snapshot()['events']==before


def test_scope_exception_and_nested_attempt_restore_context():
    with pytest.raises(RuntimeError):
        with scope():
            with pytest.raises(ValueError):
                with scope():pass
            raise RuntimeError('owned failure')
    assert current_payload_capture() is None


def test_optional_observer_failure_does_not_change_provider_delivery(monkeypatch):
    raw=json.dumps({'choices':[{'delta':{'content':'preserved'},'finish_reason':'stop'}]})
    with scope() as capture:
        def fail(*args):raise MemoryError('injected capture failure')
        monkeypatch.setattr(capture,'record',fail)
        result=_run_sync([],raw_chunks=[('data: '+raw+'\n\ndata: [DONE]\n\n').encode()])
        assert result.message['content']=='preserved'
    assert not capture.snapshot()['coverage_complete']


def test_parallel_async_contexts_do_not_cross_capture():
    import asyncio
    async def job(label):
        with capture_provider_payloads(owner_user_id=7,request_label=label,max_bytes=100,max_events=4) as capture:
            stream=capture.new_stream('model','trace');await asyncio.sleep(0)
            current_payload_capture().record(stream,label);capture.finish(stream,'finished',0)
        return capture.snapshot()
    async def both():return await asyncio.gather(job('alpha'),job('beta'))
    rows=asyncio.run(both())
    assert [row['events'][0]['data'] for row in rows]==['alpha','beta']
    assert all(row['coverage_complete'] for row in rows)


def test_bounded_stream_labels_mark_truncation():
    with scope() as capture:
        stream=capture.new_stream('m'*300,'t'*300);capture.record(stream,'payload')
        capture.finish(stream,'finished',0)
    row=capture.snapshot()['streams'][0]
    assert len(row['model'])==len(row['trace_id'])==256
    assert row['model_truncated'] and row['trace_id_truncated']
