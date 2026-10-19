"""Native visibility borrows task authority and joins preparation on Stop."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import pytest
from lib.agent_core.abort_signal import AbortSignal
from lib.tasks_pkg.native_tool_projection import NativeToolProjection, prepared_execution_tools

pytestmark=pytest.mark.unit


def tool(name):
    return {'type':'function','function':{'name':name,'description':'fixture','parameters':{'type':'object'}}}


def task():
    return dict(id='wire',_userId=7,status='running',abort_event=AbortSignal(),
        config={'tools':{'programmaticCalling':'off'}},_toolContractDocumentsByName={})


def test_execution_catalog_keeps_hidden_authority_and_owned_gateway_contracts():
    owner=task(); visible=[tool('read')]; owner['_executable_tool_catalog']=visible+[tool('hidden')]
    frozen=prepared_execution_tools(owner,visible)
    assert {t['function']['name'] for t in frozen}=={'read','hidden','search_tools','execute_tools'}
    assert owner['_executable_tool_catalog']==visible+[tool('hidden')]
    assert 'execute_tools' in owner['_toolContractDocumentsByName']
    assert {'read','hidden'} <= owner['_toolContractDocumentsByName'].keys()
    frozen[0]['function']['parameters']['changed']=True
    assert 'changed' not in visible[0]['function']['parameters']
    with pytest.raises(ValueError):prepared_execution_tools(owner,[tool('foreign')])


def test_explicit_preparation_completes_contracts_without_replacing_authoritative_documents():
    from lib.tools.contracts import compile_execution_contract_documents, validate_tool_arguments_from_documents
    owner=task(); first=tool('first'); second=tool('second')
    second['function']['parameters'].update(required=['count'],properties={'count':{'type':'integer','minimum':2}})
    existing=compile_execution_contract_documents([first])['first']
    owner['_toolContractDocumentsByName']['first']=existing
    owner['_executable_tool_catalog']=[first,second]
    prepared_execution_tools(owner,[first])
    documents=owner['_toolContractDocumentsByName']
    assert documents['first'] is existing
    assert validate_tool_arguments_from_documents(documents,'second',{'count':2})=={'count':2}
    second['function']['parameters']['properties']['count']['minimum']=0
    with pytest.raises(ValueError):validate_tool_arguments_from_documents(documents,'second',{'count':1})
    with pytest.raises(ValueError):validate_tool_arguments_from_documents(documents,'foreign',{})


def test_present_invalid_contract_is_not_repaired_into_execution_authority():
    from lib.tools.contracts import validate_tool_arguments_from_documents
    owner=task(); definition=tool('read'); invalid={'name':'read','contractVersion':'invalid'}
    owner['_toolContractDocumentsByName']['read']=invalid
    prepared_execution_tools(owner,[definition])
    assert owner['_toolContractDocumentsByName']['read'] is invalid
    with pytest.raises(ValueError):validate_tool_arguments_from_documents(owner['_toolContractDocumentsByName'],'read',{})


def test_empty_authority_grants_no_gateway_or_other_tool():
    owner=task(); owner['_executable_tool_catalog']=[]
    assert prepared_execution_tools(owner,[])==[]


def test_projected_gateway_visibility_uses_declared_native_execution_contracts():
    from lib.tools.gateway import gateway_tool_schemas
    owner=task(); authority=[tool('read')]; owner['_executable_tool_catalog']=authority
    visible=authority+gateway_tool_schemas(include_search=True)
    frozen=prepared_execution_tools(owner,visible)
    assert {t['function']['name'] for t in frozen}=={'read','search_tools','execute_tools'}
    assert owner['_executable_tool_catalog']==authority
    # Existing declarations are the capability boundary, including disabled
    # search and zero-authority invocations. Visibility cannot reopen either.
    owner['_tool_gateway_names']=['execute_tools']
    with pytest.raises(ValueError):prepared_execution_tools(owner,visible)
    owner['_executable_tool_catalog']=[]
    with pytest.raises(ValueError):prepared_execution_tools(owner,visible[1:])


def test_cancelled_wire_preparation_joins_started_worker_without_a_late_result():
    async def run():
        owner=task(); request=SimpleNamespace(task=owner)
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            projection=NativeToolProjection(request,executor)
            def blocked(*args):
                entered.set()
                release.wait(3)
                finished.set()
                return []
            projection._prepare=blocked
            pending=asyncio.create_task(projection.prepare({}, {}, {}))
            assert await asyncio.to_thread(entered.wait,2)
            pending.cancel()
            await asyncio.sleep(.01)
            assert not pending.done() and owner['abort_event'].is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):await pending
            assert finished.is_set()
    asyncio.run(run())


def test_owner_type_change_or_stop_refuses_before_worker_entry():
    async def run():
        owner=task(); request=SimpleNamespace(task=owner)
        with ThreadPoolExecutor(max_workers=1) as executor:
            projection=NativeToolProjection(request,executor)
            owner['_userId']=True
            with pytest.raises(ValueError):await projection.prepare({}, {}, {})
            owner['_userId']=7;owner['abort_event'].set()
            with pytest.raises(asyncio.CancelledError):await projection.prepare({}, {}, {})
    asyncio.run(run())
