"""Actual HTTP client preparation preserves admitted wire bytes and isolation."""
import json
import asyncio
import importlib
from types import SimpleNamespace

import httpx
import pytest
import requests

from lib.llm.request import seal_request, transport_body_arguments, transport_payload

pytestmark = pytest.mark.unit


def admitted(source):
    return seal_request(source,model='fixture',input_tokens=1,semantic_id='fixture')


@pytest.mark.parametrize('sealed', [False, True])
@pytest.mark.parametrize('transport', ['requests', 'httpx'])
def test_http_clients_preserve_json_and_never_expose_internal_fields(transport,sealed):
    source={'messages':[{'role':'user','content':'中文 😀',
                         'metadata':{'integer':2**70,'empty':None,'flag':False}}]}
    request=admitted(dict(source,_private='not on wire')) if sealed else source
    expected=json.loads(json.dumps(source))
    if sealed:
        source['messages'][0]['content']='caller mutation'
    arguments=transport_body_arguments(request,transport=transport)
    headers={'Content-Type':'application/json','X-Fixture':'preserved'}
    if transport=='requests':
        prepared=requests.Request('POST','https://fixture.invalid',headers=headers,
                                  **arguments).prepare()
        payload=prepared.body
        assert prepared.headers['X-Fixture']=='preserved'
    else:
        seen=[]
        def handle(http_request):
            seen.append(http_request.content)
            assert http_request.headers['X-Fixture']=='preserved'
            return httpx.Response(200,json={'ok':True})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            assert client.post('https://fixture.invalid',headers=headers,**arguments).status_code==200
        payload=seen[0]
    assert json.loads(payload)==expected
    if sealed:
        assert payload==request.payload
        assert 'json' not in arguments


def test_byte_egress_uses_snapshot_and_preserves_legacy_encoding():
    source={'messages':[{'role':'user','content':'unchanged'}]}
    request=admitted(source)
    original=request.payload
    source['messages'].clear()
    assert transport_payload(request) is original
    assert transport_payload(source)==json.dumps(source).encode()


def test_real_async_stream_sends_snapshot_after_budget_dispatch_and_closes(monkeypatch):
    module=importlib.import_module('lib.llm.astream')
    sealed=admitted({'messages':[{'role':'user','content':'async 中文'}]})
    events=[]
    class Lease:
        def prepare_input(self,body):
            assert body is sealed
            events.append('prepared')
        def mark_dispatched(self):
            events.append('dispatched')
    def handle(request):
        assert events==['prepared','dispatched']
        assert request.content==sealed.payload
        assert request.headers['Content-Type']=='application/json'
        events.append('sent')
        return httpx.Response(200,content=b'data: [DONE]\n\n')
    plan=SimpleNamespace(body=sealed,url='https://fixture.invalid',
                         hdrs={'Content-Type':'application/json'},model_budget_lease=Lease())
    monkeypatch.setattr('lib.proxy.subscription_route_candidates',lambda url:[])
    monkeypatch.setattr(module,'_resolve_async_route',lambda url:(None,{'mode':'fixture'}))
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(module,'get_async_client',lambda proxy:client)
            async with module._open_server_stream(plan) as (response,route):
                assert route is None
                assert await response.aread()==b'data: [DONE]\n\n'
            assert response.is_closed
    asyncio.run(execute())
    assert events==['prepared','dispatched','sent']
