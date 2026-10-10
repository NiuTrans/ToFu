"""Transport-level fidelity for code identifiers and UTF-8 inside JSON content.

Uses actual sync/async SSE shells over controlled network bytes. It does not
claim fidelity of an upstream provider or diagnose a response-format grammar.
"""
import json
import pytest
from tests.sse.test_sse_core_parity import _run_sync, _run_async
from lib.llm.stream_result import ProviderStreamState
pytestmark = pytest.mark.unit


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('network_chunk', [1, 3, 7, 64, 100000])
@pytest.mark.parametrize('content_chunks', ['characters', 'identifier'])
def test_json_wrapped_python_survives_sse_content_and_network_boundaries(run, network_chunk, content_chunks):
    source = 'import json; print(json.dumps({"标识": "json", "escape": "\\n"}, ensure_ascii=False))'
    expected = json.dumps({'python': source}, ensure_ascii=False)
    if content_chunks == 'characters':
        pieces = list(expected)
    else:
        # The bare word json is a valid content delta, never a framing hint.
        split = expected.index('json')
        pieces = [expected[:split], 'json', expected[split+4:]]
    frames = [{'choices':[{'delta':{'content':piece},'finish_reason':None}]} for piece in pieces]
    frames.append({'choices':[{'delta':{},'finish_reason':'stop'}]})
    wire = b''.join(b'data: '+json.dumps(frame,ensure_ascii=False).encode()+b'\r\n\r\n' for frame in frames)
    wire += b'data: [DONE]\r\n\r\n'
    result = run([], model='glm-5.3-flash', raw_chunks=[wire[i:i+network_chunk] for i in range(0,len(wire),network_chunk)])
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message['content'] == expected
    assert json.loads(result.message['content'])['python'] == source
    assert result.evidence.malformed_frame_count == 0
