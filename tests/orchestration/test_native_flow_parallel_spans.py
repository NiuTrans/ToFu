"""Borrowed span publication remains atomic, exact and graph-scoped."""
import ctypes as c
import pytest
from lib.orchestration.graph import FlowExecutionError
from lib.orchestration.native_flow import _Span
from lib.orchestration.native_flow_contract import SPEC
from tests.orchestration.test_native_flow_engine import native_library as native_library
from tests.orchestration.test_native_flow_parallel import _graph

pytestmark=pytest.mark.unit


def _spans(parts):return (_Span*len(parts))(*((value,len(value)) for value in parts))


def test_all_span_validation_precedes_any_publication(native_library):
    graph=_graph(native_library,['a','b'])
    f=native_library._library.tofu_flow_parallel_merge_spans
    try:
        output=c.create_string_buffer(b'untouched payload',32);before=output.raw;written=c.c_size_t(987)
        for parts in ((b'seed',b'a',b'\xff'),(b'seed',b'a'),(b'seed',b'a',b'b',b'extra')):
            spans=_spans(parts)
            assert f(graph.handle,graph.index('fanout'),spans,len(spans),output,len(output),c.byref(written))==SPEC['statuses']['invalid_input']
            assert output.raw==before and written.value==987
        spans=_spans([b'seed',b'a',b'b'])
        spans[2].data=None;spans[2].length=1
        assert f(graph.handle,graph.index('fanout'),spans,len(spans),output,len(output),c.byref(written))==SPEC['statuses']['invalid_input']
        assert output.raw==before and written.value==987
        spans[2].length=c.c_size_t(-1).value
        assert f(graph.handle,graph.index('fanout'),spans,len(spans),output,len(output),c.byref(written))==SPEC['statuses']['invalid_input']
        assert output.raw==before and written.value==987
        assert f(graph.handle,graph.index('fanout'),None,3,output,len(output),c.byref(written))==SPEC['statuses']['invalid_input']
        assert output.raw==before and written.value==987
    finally:graph.close()


def test_span_short_output_reports_only_required_bytes_then_exact_retry(native_library):
    graph=_graph(native_library,['a','b'])
    f=native_library._library.tofu_flow_parallel_merge_spans
    try:
        spans=_spans([b'seed',b'a',b'b']);output=c.create_string_buffer(b'untouched payload',32);before=output.raw;written=c.c_size_t(987)
        assert f(graph.handle,graph.index('fanout'),spans,3,output,9,c.byref(written))==SPEC['statuses']['buffer_small']
        assert output.raw==before and written.value==10
        assert f(graph.handle,graph.index('fanout'),spans,3,output,10,c.byref(written))==0
        assert output.raw[:10]==b'seed\n\na\n\nb' and output.raw[10:]==before[10:]
        assert written.value==10
        spans=_spans([b'',b'',b''])
        assert f(graph.handle,graph.index('fanout'),spans,3,None,0,c.byref(written))==0
        assert written.value==0
        assert f(graph.handle,graph.index('a'),spans,3,output,32,c.byref(written))==SPEC['statuses']['invalid_input']
    finally:graph.close()
    written.value=987
    assert f(graph.handle,graph.index('fanout'),spans,3,output,32,c.byref(written))==SPEC['statuses']['unknown_handle']
    assert written.value==987


def test_selected_span_failure_cannot_try_json_or_python(native_library,monkeypatch):
    graph=_graph(native_library,['a'])
    try:
        monkeypatch.setattr(native_library._library,'tofu_flow_parallel_merge_spans',lambda *args:SPEC['statuses']['invalid_state'])
        monkeypatch.setattr(native_library._library,'tofu_flow_parallel_merge',lambda *args:pytest.fail('selected span failure entered JSON merge'))
        with pytest.raises(FlowExecutionError,match='invalid_state'):graph.parallel_merge('fanout','seed',['a'])
    finally:graph.close()


def test_span_input_stays_borrowed_through_synchronous_return(native_library,monkeypatch):
    graph=_graph(native_library,['a','b'])
    try:
        original=native_library._library.tofu_flow_parallel_merge_spans
        def inspect_spans(graph_id,node,spans,count,output,capacity,written):
            import gc
            gc.collect()
            assert [c.string_at(c.c_void_p.from_address(c.addressof(spans[i])+_Span.data.offset).value,spans[i].length) for i in range(count)] == [value.encode() for value in ('中\x00🙂','x\x00z','🙂'*6000)]
            return original(graph_id,node,spans,count,output,capacity,written)
        monkeypatch.setattr(native_library._library,'tofu_flow_parallel_merge_spans',inspect_spans)
        monkeypatch.setattr('lib.orchestration.native_flow._LOOP_ENCODER',None)
        assert graph.parallel_merge('fanout','中\x00🙂',['x\x00z','🙂'*6000])=='中\x00🙂\n\nx\x00z\n\n'+'🙂'*6000
    finally:graph.close()


def test_span_pointer_alignment_and_output_preflight_refuse_without_publication(native_library):
    graph=_graph(native_library,['a','b'])
    f=native_library._library.tofu_flow_parallel_merge_spans
    try:
        spans=_spans([b'seed',b'a',b'b']);output=c.create_string_buffer(b'untouched',32);before=output.raw;written=c.c_size_t(987)
        assert f(graph.handle,graph.index('fanout'),spans,3,None,1,c.byref(written))==SPEC['statuses']['invalid_input']
        assert written.value==987
        assert f(graph.handle,graph.index('fanout'),spans,3,output,32,None)==SPEC['statuses']['invalid_input']
        storage=c.create_string_buffer(c.sizeof(spans)+1)
        misaligned=c.cast(c.addressof(storage)+1,c.POINTER(_Span))
        assert f(graph.handle,graph.index('fanout'),misaligned,3,output,32,c.byref(written))==SPEC['statuses']['invalid_input']
        assert written.value==987 and output.raw==before
    finally:graph.close()
