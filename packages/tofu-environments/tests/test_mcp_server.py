"""Real stdio MCP handshake, catalog gating and fail-closed write invocation."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import pytest

pytestmark = pytest.mark.unit


def test_stdio_server_uses_sdk_schemas_and_refuses_unapproved_write(tmp_path):
    pytest.importorskip('mcp', exc_type=ImportError)  # independently installed optional extra
    from test_host import SPEC
    factory=tmp_path/'trusted_factory.py'
    factory.write_text('''from tofu_environments import Actor, EnvironmentService, MemoryRepository
from tofu_environments.mcp_adapter import MCPBinding
from test_service import FakeProvider
from pathlib import Path

def binding():
    return MCPBinding(Actor(11, 'tenant', '''+repr(str(tmp_path))+'''),
        environment_service=EnvironmentService(MemoryRepository(), lambda *_: FakeProvider()),
        close=lambda: Path('__CLOSE_PATH__').write_text('closed'))
'''.replace('__CLOSE_PATH__', str(tmp_path/'closed.txt')))
    environment=dict(os.environ)
    environment['PYTHONPATH']=os.pathsep.join([str(tmp_path), str(Path(__file__).parent), environment.get('PYTHONPATH','')])
    proc=subprocess.Popen([sys.executable,'-m','tofu_environments.mcp_server','--factory','trusted_factory:binding'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=environment)
    def send(payload):
        proc.stdin.write(json.dumps(payload)+'\n');proc.stdin.flush()
    def receive():
        assert select.select([proc.stdout],[],[],15)[0], 'MCP response deadline exceeded'
        line=proc.stdout.readline(262145)
        assert line and len(line)<=262144, 'MCP response missing or oversized'
        return json.loads(line)
    try:
        send({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'boundary-test','version':'1'}}})
        assert receive()['result']['serverInfo']['name']=='tofu-environments-mcp'
        send({'jsonrpc':'2.0','method':'notifications/initialized'})
        send({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
        tools={row['name']:row for row in receive()['result']['tools']}
        assert set(tools)=={'environment_catalog','environment_plan','environment_apply','environment_status'}
        assert tools['environment_catalog']['annotations']['readOnlyHint'] is True
        assert tools['environment_apply']['annotations']['readOnlyHint'] is False
        assert 'inputSchema' in tools['environment_plan']
        send({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{
            'name':'environment_plan','arguments':{'provider':'test','spec':SPEC,'request_id':'stdio-plan'}}})
        result=receive()['result'];assert not result.get('isError')
        plan=json.loads(result['content'][0]['text'])
        args={k:plan[k] for k in ('operation_id','plan_hash','plan')}
        send({'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'environment_apply','arguments':args}})
        result=receive()['result'];assert result['isError'] is True
        assert json.loads(result['content'][0]['text'])['code']=='approval_required'
    finally:
        proc.stdin.close()
        try:proc.wait(timeout=10)
        except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=10)
        proc.stdout.close();proc.stderr.close()
        assert (tmp_path/'closed.txt').read_text() == 'closed'
