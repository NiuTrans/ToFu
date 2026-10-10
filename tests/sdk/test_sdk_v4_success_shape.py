"""Public synchronous/asynchronous SDK methods preserve the pre-cutover shape."""
import asyncio
from copy import deepcopy
from pathlib import Path
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def sdk_source(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'clients/python'))


@pytest.mark.parametrize('data,expected', [
    (None, None), ([1, 2], [1, 2]),
    ({'status':'connected'}, {'ok':True,'status':'connected'}),
    ({'ok':False,'reason':'declined'}, {'ok':False,'reason':'declined'}),
    ({'data':{'value':1},'meta':{'domain':True}}, {'ok':True,'data':{'value':1},'meta':{'domain':True}}),
])
def test_sdk_success_shape_for_sync_and_async(data, expected, requests_mock):
    from tofu_sdk import Tofu
    from tofu_sdk.async_client import AsyncTofu
    import httpx
    wire={'data':data,'meta':{'requestId':'test','serverTimeMs':1}}
    original = deepcopy(wire)
    requests_mock.get('http://sdk.invalid/api/v4/capabilities',json=wire, headers={'Content-Type': 'application/json'})
    assert Tofu(base_url='http://sdk.invalid').capabilities() == expected
    async def run():
        def respond(request):
            assert request.url.path == '/api/v4/capabilities'
            return httpx.Response(200,json=wire)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            sdk=AsyncTofu(base_url='http://sdk.invalid',client=http)
            assert await sdk.capabilities() == expected
    asyncio.run(run())
    assert wire == original, 'wire envelope must not be mutated'


def test_sdk_error_body_is_never_normalized_into_success(requests_mock):
    from tofu_sdk import Tofu,TofuError
    body={'status':403,'code':'permission_denied','data':{'private':'shape'},'meta':{}}
    requests_mock.get('http://sdk.invalid/api/v4/capabilities',status_code=403,json=body)
    with pytest.raises(TofuError) as error:
        Tofu(base_url='http://sdk.invalid').capabilities()
    assert error.value.body == body

    from tofu_sdk.async_client import AsyncTofu
    import httpx
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json=body))) as http:
            with pytest.raises(TofuError) as error:
                await AsyncTofu(base_url='http://sdk.invalid', client=http).capabilities()
            assert error.value.body == body
    asyncio.run(run())
