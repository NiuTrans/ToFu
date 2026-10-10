"""Owned checkpoint C ABI calls never replay one-shot load after buffer failure."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from types import SimpleNamespace
from pathlib import Path
import pytest
from tofu_db.embedded import EmbeddedRuntime, EmbeddedTransportError, _ffi
from tofu_db.generated_checkpoint_control import FORMAT, RESPONSE_HEADROOM_BYTES, OPERATIONS
pytestmark=pytest.mark.unit

def runtime(native):
 value=EmbeddedRuntime.__new__(EmbeddedRuntime);value._handle=_ffi.cast('void*',1)
 value._lifecycle=threading.Condition();value._active_calls=0;value._closing=False
 value._lib=SimpleNamespace(tofudb_embed_checkpoint_supported=lambda:1,
     tofudb_embed_checkpoint_execute=native,tofudb_embed_close=lambda _:None)
 return value

def test_checkpoint_buffer_failure_never_reexecutes_one_shot_call():
 calls=[]
 def native(handle,request,length,out,capacity):
  calls.append(json.loads(bytes(request)))
  return -(capacity+1)
 value=runtime(native)
 with pytest.raises(EmbeddedTransportError,match='not retried'):
  value.checkpoint_control('load',{'format':FORMAT,'capability_id':1},deadline_unix_ms=1,response_bytes=RESPONSE_HEADROOM_BYTES)
 assert len(calls)==1 and value._active_calls==0
 value.close()

def test_checkpoint_close_joins_entered_calls_and_refuses_late_calls():
 entered,release,closed=threading.Event(),threading.Event(),threading.Event()
 def native(handle,request,length,out,capacity):
  entered.set();assert release.wait(5);assert not closed.is_set()
  response=json.dumps(dict(format=FORMAT,accepted=True,capability_id=1,state=None,error=None)).encode()
  _ffi.memmove(out,response,len(response));return len(response)
 value=runtime(native);value._lib.tofudb_embed_close=lambda _:closed.set()
 with ThreadPoolExecutor(max_workers=2) as executor:
  call=executor.submit(value.checkpoint_control,'load',{'format':FORMAT,'capability_id':1},deadline_unix_ms=1,response_bytes=RESPONSE_HEADROOM_BYTES)
  assert entered.wait(5);closing=executor.submit(value.close)
  until=time.monotonic()+5
  while not value._closing:
   assert time.monotonic()<until;time.sleep(.001)
  assert not closed.is_set()
  with pytest.raises(EmbeddedTransportError,match='closed'):
   value.checkpoint_control('load',{'format':FORMAT,'capability_id':1},deadline_unix_ms=1,response_bytes=RESPONSE_HEADROOM_BYTES)
  release.set();assert call.result(5)['accepted'];closing.result(5)
 assert closed.is_set() and value.closed

def test_old_library_refuses_checkpoint_without_native_dispatch():
 value=runtime(lambda *args:pytest.fail('unsupported checkpoint reached native dispatch'))
 del value._lib.tofudb_embed_checkpoint_supported
 assert not value.checkpoint_supported
 with pytest.raises(EmbeddedTransportError,match='not enabled'):
  value.checkpoint_control('load',{'format':FORMAT,'capability_id':1},deadline_unix_ms=1,response_bytes=RESPONSE_HEADROOM_BYTES)
 value.close()

def test_generated_checkpoint_constants_match_the_authority():
 root=Path(__file__).resolve().parents[2];spec=json.loads((root/'contracts/native_checkpoint_control_v1.schema.json').read_text())
 assert FORMAT==spec['$id'] and RESPONSE_HEADROOM_BYTES==spec['limits']['responseHeadroomBytes']
 assert set(OPERATIONS)==set(spec['$defs'])-{'binding','response'}


def test_actual_checkpoint_sdk_library_keeps_pause_and_close_fences(tmp_path):
 import os
 import subprocess
 library=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY');binary=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY')
 if not library or not binary:pytest.skip('explicit private native checkpoint library/binary required')
 directory=tmp_path/'authority';directory.mkdir()
 subprocess.run([binary,'init','--data-dir',str(directory)],check=True,capture_output=True)
 def opened():return EmbeddedRuntime(Path(library),directory,owner_id=7,tenant_id=3,secret=b'checkpoint-sdk-test-secret-0000001')
 capacity=1048576+RESPONSE_HEADROOM_BYTES
 params=dict(owner_user_id=7,task_id='sdk-task',epoch='epoch',binding='model-tools-account',token='caller',header_bytes=4096,snapshot_bytes=1048576,identity_bytes=1024)
 state=dict(format='tofu.native-agent-checkpoint/v1',owner_user_id=7,task_id='sdk-task',epoch='epoch',binding='model-tools-account',next_round=1,tool_calls=1,
   messages=[dict(role='user',content='question'),dict(role='tool',content='x'*65536)],usage=[{}],round_limit=3,tool_limit=3,no_progress_limit=0,failure_limit=0,
   progress=dict(codec='sha256-token-sequence/v1',last_calls=None,last_world='',last_evidence=None,last_failure=None,no_progress_streak=0,failure_streak=0))
 with opened() as native:
  assert native.checkpoint_supported
  expected=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
  if expected:assert native.build_identity()['source_sha256']==expected
  def call(op,**fields):return native.checkpoint_control(op,dict(format=FORMAT,**fields),deadline_unix_ms=time.time_ns()//1000000+30000,response_bytes=capacity)
  created=call('create',parameters=params);assert created['accepted'];key=created['capability_id']
  assert call('load',capability_id=key)['state'] is None
  assert call('begin',capability_id=key,round=0)['accepted']
  assert call('complete',capability_id=key,state=state)['accepted']
  assert call('release',capability_id=key)['accepted']
  assert call('forget',capability_id=key)['accepted']
 with opened() as native:
  def call(op,**fields):return native.checkpoint_control(op,dict(format=FORMAT,**fields),deadline_unix_ms=time.time_ns()//1000000+30000,response_bytes=capacity)
  created=call('create',parameters=params);assert created['accepted'];key=created['capability_id']
  assert call('load',capability_id=key)['state']==state
  assert call('begin',capability_id=key,round=1)['accepted']
  assert call('forget',capability_id=key)['accepted']
 with opened() as native:
  result=native.checkpoint_control('create',dict(format=FORMAT,parameters=params),deadline_unix_ms=time.time_ns()//1000000+30000,response_bytes=capacity)
  assert result['accepted'] is False and result['error']=='ineligible'
