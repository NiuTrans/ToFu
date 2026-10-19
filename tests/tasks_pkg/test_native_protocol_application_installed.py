"""Ordinary HTTP native ROOT preserves provider protocol, usage and durable Turns."""
import json
import pytest
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)
from tests.model_routing import test_native_route
from benchmarks import native_agent_root

pytestmark=pytest.mark.api

@pytest.fixture
def application_storage_owner_id():return 1


def stream_events(protocol,text,*,tools=False):
    arguments={'todos':[{'content':'Verify native gateway','status':'completed'}]}
    calls=[('direct-todo','todo_write',arguments),('gateway-todo','execute_tools',{'calls':[{'name':'todo_write','call_id':'nested-todo','arguments':arguments}]})]
    if tools and protocol=='openai_responses':
        items=[dict(type='function_call',id=identity,call_id=identity,name=name,arguments=json.dumps(value)) for identity,name,value in calls]
        return [{'type':'response.output_item.added','output_index':index,'item':dict(item,arguments='')} for index,item in enumerate(items)]+[
            {'type':'response.completed','response':{'output':items,'usage':{'input_tokens':3,'output_tokens':2}}}]
    if tools:
        events=[{'type':'message_start','message':{'id':'message','type':'message','role':'assistant','content':[],'model':'alpha-wire-a','usage':{'input_tokens':3,'output_tokens':0}}}]
        for index,(identity,name,value) in enumerate(calls):
            events.extend([{'type':'content_block_start','index':index,'content_block':{'type':'tool_use','id':identity,'name':name,'input':{}}},
                {'type':'content_block_delta','index':index,'delta':{'type':'input_json_delta','partial_json':json.dumps(value)}},
                {'type':'content_block_stop','index':index}])
        return events+[{'type':'message_delta','delta':{'stop_reason':'tool_use','stop_sequence':None},'usage':{'output_tokens':2}},{'type':'message_stop'}]
    if protocol=='openai_responses':
        return [{'type':'response.completed','response':{'output':[{'type':'message','role':'assistant',
            'content':[{'type':'output_text','text':text}]}], 'usage':{'input_tokens':3,'output_tokens':2}}}]
    return [
        {'type':'message_start','message':{'id':'message','type':'message','role':'assistant','content':[],
            'model':'alpha-wire-a','usage':{'input_tokens':3,'output_tokens':0}}},
        {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
        {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}},
        {'type':'content_block_stop','index':0},
        {'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':2}},
        {'type':'message_stop'},
    ]


@pytest.mark.parametrize('scenario',['text','mixed_direct_first'])
@pytest.mark.parametrize('protocol,profile',[
    ('anthropic',None),('openai_responses','compatible'),('openai_responses','openai'),('openai_responses','codex')])
def test_native_application_provider_protocol_reaches_durable_completion(application_storage,tmp_path,monkeypatch,caplog,protocol,profile,scenario):
    seen=[]
    repository_base=test_native_route.Repository
    class Repository(repository_base):
        def __init__(self):
            super().__init__()
            self.document['connections'][0]['protocol']=protocol
            if profile:self.document['connections'][0]['responses_profile']=profile
    workload_base=native_agent_root.RootWorkload
    class Workload(workload_base):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            workload=self
            def respond(request):
                body=json.loads(request.rfile.read(int(request.headers['Content-Length'])))
                workload.bodies.append(body)
                seen.append((request.path,{key.lower():value for key,value in request.headers.items()},body))
                wire=''.join('data: '+json.dumps(row)+'\n\n' for row in stream_events(protocol,'durable fixture answer',tools=scenario!='text' and len(workload.bodies)==1)).encode()
                request.send_response(200);request.send_header('Content-Type','text/event-stream')
                request.send_header('Content-Length',str(len(wire)));request.end_headers();request.wfile.write(wire)
            self.server.RequestHandlerClass.do_POST=respond
    monkeypatch.setattr(test_native_route,'Repository',Repository)
    monkeypatch.setattr(native_agent_root,'RootWorkload',Workload)
    native_case(application_storage,tmp_path,monkeypatch,caplog,scenario=scenario,
        prebind_failure=False,resume_operation=None)
    assert len(seen)==(1 if scenario=='text' else 2)
    for path,headers,body in seen:
        assert body['model']=='alpha-wire-a'
        assert body['stream'] is True
        if protocol=='anthropic':
            assert path=='/v1/messages'
            assert headers['x-api-key']=='private-fixture-token'
            assert headers['anthropic-version']=='2023-06-01'
            assert body['messages'] and body['max_tokens']>0
        else:
            assert path=='/v1/responses'
            assert headers['authorization']=='Bearer private-fixture-token'
            assert body['input'] and 'messages' not in body
    if scenario!='text':
        continuation=seen[1][2]
        if protocol=='anthropic':
            results=[part for message in continuation['messages'] for part in message['content']
                if isinstance(part,dict) and part.get('type')=='tool_result']
            assert {part['tool_use_id'] for part in results}=={'direct-todo','gateway-todo'}
        else:
            results=[item for item in continuation['input'] if item.get('type')=='function_call_output']
            assert {item['call_id'] for item in results}=={'direct-todo','gateway-todo'}
        assert len(results)==2
