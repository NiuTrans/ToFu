"""Demand reuse, complete identity scope and exact process/image teardown."""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch
import pytest
from lib.agent_core.native_worker_pool import NativeWorkerPool
from lib.agent_core.native_content_contract import NATIVE_WORKER_CONTRACT
from lib.agent_core.native_process import Budget,NativeConfigurationError,NativeProcessError
from lib.identity import PrincipalContext

pytestmark=pytest.mark.unit

class Image:
    def __init__(self):self.descriptors=[]
    async def duplicate(self,_limit):
        value=open('/dev/null','rb');self.descriptors.append(value);return value

class Process:
    next_pid=7000
    def __init__(self,owner):
        self.owner=owner;self.stdout=asyncio.StreamReader();self.stdin=self;self.returncode=None
        self.exit=asyncio.Event();self.frames=[];Process.next_pid+=1;self.pid=Process.next_pid
        self.emit('worker_ready',0,cached_encodings=0)
    def emit(self,kind,sequence,**fields):
        self.stdout.feed_data((json.dumps(dict(version=NATIVE_WORKER_CONTRACT,type=kind,owner_user_id=self.owner,sequence=sequence,**fields))+'\n').encode())
    def write(self,raw):
        frame=json.loads(raw);self.frames.append(frame)
        if frame['type']=='worker_run':
            self.emit('worker_event',frame['sequence'],frame={'type':'done','status':'done'})
            self.emit('worker_ready',frame['sequence'],cached_encodings=1)
        elif frame['type']=='worker_shutdown':
            self.emit('worker_closed',frame['sequence']);self.finish(0)
    async def drain(self):pass
    def close(self):self.finish(0)
    def kill(self):self.finish(-9)
    def finish(self,code):
        if self.returncode is None:self.returncode=code;self.stdout.feed_eof();self.exit.set()
    async def wait(self):await self.exit.wait();return self.returncode


def budget(frame=65536):return Budget(1,16<<20,65536,frame,1)
def principal(**kwargs):return PrincipalContext.user(subject_id='native-pool',owner_user_id=7,**kwargs)
async def complete(pool,lease):
    lease.stdin.write(b'{"version":"tofu.native-agent-stdio/v1","type":"configure","owner_user_id":7}\n')
    await lease.stdin.drain();assert json.loads(await lease.stdout.readline())['type']=='done'
    assert await lease.wait()==0;assert await lease.stdout.readline()==b'';lease.stdin.close()
    await pool.release(lease,healthy=True)


def test_exclusive_scope_reuses_only_after_ready_and_final_release(tmp_path):
    async def run():
        image=Image();created=[]
        async def spawn(*args,**kwargs):created.append(Process(int(args[3])));return created[-1]
        pool=NativeWorkerPool(image=image,idle_workers=1)
        with patch('asyncio.create_subprocess_exec',side_effect=spawn):
            first=await pool.acquire(principal=principal(),budget=budget(),working_directory=tmp_path)
            await complete(pool,first)
            second=await pool.acquire(principal=principal(),budget=budget(),working_directory=tmp_path)
            assert second.pid==first.pid and second.sequence==2
            with pytest.raises(BrokenPipeError):first.stdin.write(b'{}\n')
            await complete(pool,second);await pool.close()
        assert len(created)==1 and created[0].frames[-1]['type']=='worker_shutdown'
        assert created[0].frames[-1]['sequence']==3 and all(fd.closed for fd in image.descriptors)
        assert not pool.workers and not pool.idle
    asyncio.run(run())


@pytest.mark.parametrize('difference',['tenant','scopes','directory','budget','replacement'])
def test_reuse_key_preserves_principal_workspace_and_transport(difference,tmp_path):
    async def run():
        image=Image();created=[]
        async def spawn(*args,**kwargs):created.append(Process(int(args[3])));return created[-1]
        directory=tmp_path/'work';directory.mkdir();pool=NativeWorkerPool(image=image,idle_workers=2)
        with patch('asyncio.create_subprocess_exec',side_effect=spawn):
            first=await pool.acquire(principal=principal(),budget=budget(),working_directory=directory);await complete(pool,first)
            identity=principal();limits=budget();target=directory
            if difference=='tenant':identity=principal(tenant_id='other')
            elif difference=='scopes':identity=principal(scopes={'other'})
            elif difference=='budget':limits=budget(65537)
            elif difference=='directory':target=tmp_path/'other';target.mkdir()
            else:directory.rename(tmp_path/'old');directory.mkdir()
            second=await pool.acquire(principal=identity,budget=limits,working_directory=target)
            assert second.pid!=first.pid;await complete(pool,second);await pool.close()
        assert all(fd.closed for fd in image.descriptors)
    asyncio.run(run())


def test_idle_hint_does_not_serialize_active_work(tmp_path):
    async def run():
        image=Image();created=[]
        async def spawn(*args,**kwargs):created.append(Process(int(args[3])));return created[-1]
        pool=NativeWorkerPool(image=image,idle_workers=1)
        with patch('asyncio.create_subprocess_exec',side_effect=spawn):
            first,second=await asyncio.gather(*(pool.acquire(principal=principal(),budget=budget(),working_directory=tmp_path) for _ in range(2)))
            assert first.pid!=second.pid
            await complete(pool,first);await complete(pool,second)
            assert len(pool.idle)==1 and len(pool.workers)==1
            await pool.close()
        assert len(created)==2 and all(p.returncode==0 for p in created)
    asyncio.run(run())


def test_unconfirmed_invocation_is_retired_and_close_refuses_new_work(tmp_path):
    async def run():
        image=Image()
        async def spawn(*args,**kwargs):return Process(int(args[3]))
        pool=NativeWorkerPool(image=image,idle_workers=2)
        with patch('asyncio.create_subprocess_exec',side_effect=spawn):
            lease=await pool.acquire(principal=principal(),budget=budget(),working_directory=tmp_path)
            lease.kill();await lease.wait();await pool.release(lease,healthy=False)
            assert not pool.idle and not pool.workers
            await pool.close()
            with pytest.raises(NativeConfigurationError):await pool.acquire(principal=principal(),budget=budget(),working_directory=tmp_path)
        assert all(fd.closed for fd in image.descriptors)
    asyncio.run(run())


@pytest.mark.parametrize('fault',['spawn','ready_owner','ready_version'])
def test_startup_refusal_joins_and_releases_owned_image(fault,tmp_path):
    async def run():
        image=Image();created=[]
        async def spawn(*args,**kwargs):
            if fault=='spawn':raise OSError('injected spawn failure')
            process=Process(int(args[3]));process.stdout=asyncio.StreamReader()
            value={'version':NATIVE_WORKER_CONTRACT,'type':'worker_ready','owner_user_id':7,'sequence':0,'cached_encodings':0}
            if fault=='ready_owner':value['owner_user_id']=8
            else:value['version']='foreign'
            process.stdout.feed_data((json.dumps(value)+'\n').encode());created.append(process);return process
        pool=NativeWorkerPool(image=image,idle_workers=1)
        with patch('asyncio.create_subprocess_exec',side_effect=spawn):
            with pytest.raises((OSError,NativeProcessError)):
                await pool.acquire(principal=principal(),budget=budget(),working_directory=tmp_path)
            assert not pool.workers and not pool.idle
            await pool.close()
        assert all(fd.closed for fd in image.descriptors) and all(p.returncode is not None for p in created)
    asyncio.run(run())


def test_close_joins_startup_before_releasing_new_workers(tmp_path):
    async def run():
        image=Image();entered=asyncio.Event();release=asyncio.Event();created=[]
        async def spawn(*args,**kwargs):
            entered.set();await release.wait();created.append(Process(int(args[3])));return created[-1]
        pool=NativeWorkerPool(image=image,idle_workers=1)
        with patch('asyncio.create_subprocess_exec',side_effect=spawn):
            acquire=asyncio.create_task(pool.acquire(principal=principal(),budget=budget(),working_directory=tmp_path))
            await entered.wait();closing=asyncio.create_task(pool.close());await asyncio.sleep(0)
            assert not closing.done();release.set()
            with pytest.raises(NativeConfigurationError):await acquire
            await closing
        assert not pool.workers and all(fd.closed for fd in image.descriptors)
        assert all(process.returncode==0 for process in created)
    asyncio.run(run())


@pytest.mark.parametrize('operation',['release','kill'])
@pytest.mark.parametrize('new_invocation',[False,True])
def test_retired_invocation_cannot_control_worker_owned_by_another_scope(tmp_path,new_invocation,operation):
    async def run():
        image=Image();created=[]
        async def spawn(*args,**kwargs):created.append(Process(int(args[3])));return created[-1]
        pool=NativeWorkerPool(image=image,idle_workers=1)
        with patch('asyncio.create_subprocess_exec',side_effect=spawn):
            first=await pool.acquire(principal=principal(),budget=budget(),working_directory=tmp_path)
            await complete(pool,first)
            second=await pool.acquire(principal=principal(),budget=budget(),working_directory=tmp_path) if new_invocation else None
            assert len(created)==1
            with pytest.raises(NativeProcessError,match='invocation scope'):
                if operation=='release':await pool.release(first,healthy=False)
                else:first.kill()
            assert created[0].returncode is None
            if second is not None:
                assert second.worker.current is second
                await complete(pool,second)
            else:assert len(pool.idle)==1
            await pool.close()
        assert not pool.workers and not pool.idle and all(fd.closed for fd in image.descriptors)
    asyncio.run(run())


def test_current_invocation_kill_still_reaps_and_retires_worker(tmp_path):
    async def run():
        image=Image();created=[]
        async def spawn(*args,**kwargs):created.append(Process(int(args[3])));return created[-1]
        pool=NativeWorkerPool(image=image,idle_workers=1)
        with patch('asyncio.create_subprocess_exec',side_effect=spawn):
            lease=await pool.acquire(principal=principal(),budget=budget(),working_directory=tmp_path)
            lease.kill()
            assert await lease.wait()==-9
            assert await lease.stdout.readline()==b''
            lease.stdin.close();await pool.release(lease,healthy=False)
            assert not pool.workers and not pool.idle
            await pool.close()
        assert all(fd.closed for fd in image.descriptors)
    asyncio.run(run())
