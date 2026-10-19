"""App-owned demand native workers; scoped transport only, no task/account authority.

NativeWorkerPool acquires exclusive process views keyed by principal/cwd/budget.
One pump owns physical stdout, translates canonical envelopes, and observes joined
readiness. Idle hints never cap active work; host close joins workers before image.
"""
import asyncio
from collections import OrderedDict
import json
import os
from pathlib import Path

from lib.agent_core.native_content_contract import NATIVE_WORKER_CONTRACT
from lib.agent_core.native_process import (
    NativeConfigurationError, NativeProcessError, _EncodedObjectFrame, _joined,
)


def _wire(kind,owner,sequence,body=None):
    value={'version':NATIVE_WORKER_CONTRACT,'type':kind,'owner_user_id':owner,'sequence':sequence}
    if body is not None:value['configuration' if kind=='worker_run' else 'frame']=body
    return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()+b'\n'


class _Input:
    def __init__(self,lease):self.lease=lease;self.first=True;self.closed=False
    def write(self,raw):
        lease=self.lease
        if self.closed or lease.done.done():raise BrokenPipeError('native invocation input is closed')
        if not raw.endswith(b'\n'):raise NativeProcessError('native invocation frame incomplete')
        encoded=type(raw) is _EncodedObjectFrame
        value=json.loads(raw) if self.first or not encoded else None
        if (self.first or not encoded) and not isinstance(value,dict):
            raise NativeProcessError('native invocation frame is not an object')
        if self.first:
            if value.get('type')!='configure' or value.get('owner_user_id')!=lease.worker.owner:
                raise NativeProcessError('native worker requires owned initial configuration')
        kind='worker_run' if self.first else 'worker_control'
        limit=lease.worker.budget.configuration_bytes if self.first else lease.worker.budget.frame_bytes
        if len(raw)-1>limit:raise NativeProcessError('native invocation frame exceeds original budget')
        if encoded:
            header=_wire(kind,lease.worker.owner,lease.sequence)
            field=b',"configuration":' if self.first else b',"frame":'
            packet=b''.join((header[:-2],field,memoryview(raw)[:-1],b'}\n'))
        else:
            packet=_wire(kind,lease.worker.owner,lease.sequence,value)
        lease.worker.process.stdin.write(packet)
        self.first=False
    async def drain(self):await self.lease.worker.process.stdin.drain()
    def close(self):
        self.closed=True
        if not self.lease.ready:
            self.lease.worker.retired=True
            self.lease.worker.process.stdin.close()


class _Output:
    def __init__(self,lease):self.lease=lease;self.ended=False
    async def readline(self):
        if self.ended:return b''
        if self.lease.done.done() and self.lease.frames.empty():
            self.ended=True
            if self.lease.failure is not None:raise self.lease.failure
            return b''
        value=await self.lease.frames.get()
        if isinstance(value,BaseException):raise value
        if value is None:
            self.ended=True
            if self.lease.failure is not None:raise self.lease.failure
            return b''
        return value
    async def read(self,_size):return await self.readline()


class Invocation:
    """Exclusive logical process view; EOF/exit refer to joined invocation readiness."""
    def __init__(self,worker,sequence):
        self.worker=worker;self.sequence=sequence;self.ready=False
        self.done=asyncio.get_running_loop().create_future();self.frames=asyncio.Queue(maxsize=1);self.failure=None
        self.stdin=_Input(self);self.stdout=_Output(self)
    @property
    def pid(self):return self.worker.process.pid
    @property
    def returncode(self):return self.done.result() if self.done.done() else None
    async def wait(self):return await asyncio.shield(self.done)
    def kill(self):
        if self.worker.current is not self:raise NativeProcessError('native invocation scope is no longer owned')
        self.worker.retired=True
        if self.worker.process.returncode is None:self.worker.process.kill()


class _Worker:
    def __init__(self,pool,key,process,descriptor,owner,budget):
        self.pool=pool;self.key=key;self.process=process;self.descriptor=descriptor
        self.owner=owner;self.budget=budget;self.sequence=0;self.current=None
        self.retired=False;self.started=asyncio.get_running_loop().create_future();self.closing=None
        self.closed_frame=asyncio.get_running_loop().create_future()
        self.pump=asyncio.create_task(self._pump())
    def _validate(self,value,kind,sequence,extra=()):
        if (not isinstance(value,dict) or set(value)!={'version','type','owner_user_id','sequence',*extra}
                or value.get('version')!=NATIVE_WORKER_CONTRACT or value.get('type')!=kind
                or type(value.get('owner_user_id')) is not int or value['owner_user_id']!=self.owner
                or type(value.get('sequence')) is not int or value['sequence']!=sequence):
            raise NativeProcessError('native worker envelope authority differs')
    async def _pump(self):
        failure=None
        try:
            while True:
                line=await self.process.stdout.readline()
                if not line:break
                if not line.endswith(b'\n'):raise NativeProcessError('native worker output is incomplete')
                value=json.loads(line)
                if not isinstance(value,dict):raise NativeProcessError('native worker envelope is not an object')
                if not self.started.done():
                    self._validate(value,'worker_ready',0,('cached_encodings',))
                    if type(value['cached_encodings']) is not int or value['cached_encodings']!=0:
                        raise NativeProcessError('native worker initial cache differs')
                    self.started.set_result(None);continue
                lease=self.current
                if value.get('type')=='worker_closed' and self.closing is not None:
                    if self.closed_frame.done():raise NativeProcessError('duplicate native close acknowledgement')
                    self._validate(value,'worker_closed',self.sequence+1)
                    self.closed_frame.set_result(None);continue
                if lease is None:raise NativeProcessError('idle native worker emitted unsolicited output')
                if value.get('type')=='worker_ready':
                    if lease.done.done():raise NativeProcessError('duplicate native invocation readiness')
                    self._validate(value,'worker_ready',lease.sequence,('cached_encodings',))
                    if type(value['cached_encodings']) is not int or not 0<=value['cached_encodings']<=2:
                        raise NativeProcessError('native worker cache witness differs')
                    lease.ready=True;lease.done.set_result(0)
                    if lease.frames.empty():lease.frames.put_nowait(None)
                    continue
                self._validate(value,'worker_event',lease.sequence,('frame',))
                frame=value['frame']
                if not isinstance(frame,dict):raise NativeProcessError('native worker inner frame differs')
                raw=json.dumps(frame,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()+b'\n'
                if len(raw)-1>self.budget.frame_bytes:raise NativeProcessError('native worker original output budget exceeded')
                await lease.frames.put(raw)
        except (ValueError, OSError, NativeProcessError) as error:
            failure=error;self.retired=True
            if self.process.returncode is None:self.process.kill()
        finally:
            code=await self.process.wait()
            if not self.started.done():self.started.set_exception(failure or NativeProcessError('native worker exited before readiness'))
            if self.current is not None and not self.current.done.done():
                self.current.done.set_result(code if code else 1)
                self.current.failure=failure
                if self.current.frames.empty():self.current.frames.put_nowait(None)
            if self.closing is not None and not self.closed_frame.done():
                self.closed_frame.set_exception(failure or NativeProcessError('native worker close acknowledgement missing'))
    async def _close(self):
        self.retired=True
        try:
            if self.process.returncode is None:
                async with asyncio.timeout(self.budget.shutdown_seconds):
                    self.process.stdin.write(_wire('worker_shutdown',self.owner,self.sequence+1))
                    await self.process.stdin.drain();await self.closed_frame
                    await self.process.wait()
        except (TimeoutError,OSError,NativeProcessError):
            if self.process.returncode is None:self.process.kill()
            await self.process.wait()
        finally:
            if self.process.stdin:self.process.stdin.close()
            if not self.pump.done():self.pump.cancel()
            await asyncio.gather(self.pump,return_exceptions=True)
            self.descriptor.close()
    async def close(self):
        if self.closing is None:self.closing=asyncio.create_task(self._close())
        await _joined(self.closing)


class NativeWorkerPool:
    def __init__(self,*,image,idle_workers):
        if type(idle_workers) is not int or idle_workers<0:raise NativeConfigurationError('native idle reuse hint must be nonnegative')
        self.image=image;self.idle_workers=idle_workers;self.idle=OrderedDict();self.workers=set()
        self.starting=set();self.closed=False;self.closing=None;self.loop=None
    def _bind(self):
        loop=asyncio.get_running_loop()
        if self.loop is not None and self.loop is not loop:raise NativeConfigurationError('native worker pool belongs to another loop')
        self.loop=loop
    async def acquire(self,*,principal,budget,working_directory):
        self._bind()
        if self.closed:raise NativeConfigurationError('native worker owner is closed')
        owner=principal.require_owner(context='native persistent worker')
        directory=Path(working_directory or os.getcwd()).resolve();stat=directory.stat()
        key=(principal,str(directory),stat.st_dev,stat.st_ino,budget)
        worker=None
        for candidate in tuple(self.idle):
            if candidate.key==key:
                self.idle.pop(candidate)
                if candidate.retired or candidate.process.returncode is not None:
                    await candidate.close();self.workers.discard(candidate);continue
                worker=candidate;break
        if worker is None:
            async def start():
                descriptor=await self.image.duplicate(budget.binary_bytes);process=None;result=None
                try:
                    overhead=len(_wire('worker_run',owner,(1<<64)-1,{}))
                    process=await asyncio.create_subprocess_exec(f'/proc/self/fd/{descriptor.fileno()}',
                        'run-http-agent-worker',str(max(budget.configuration_bytes,budget.frame_bytes)+overhead),str(owner),
                        pass_fds=(descriptor.fileno(),),cwd=directory,env={'PATH':''},stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,limit=budget.frame_bytes+overhead+1)
                    result=_Worker(self,key,process,descriptor,owner,budget);self.workers.add(result)
                    await asyncio.wait_for(asyncio.shield(result.started),budget.shutdown_seconds)
                    return result
                except BaseException:
                    if result is not None:
                        await result.close();self.workers.discard(result)
                        if result.started.done() and not result.started.cancelled():result.started.exception()
                    elif process is not None:
                        if process.returncode is None:process.kill()
                        await process.wait()
                    descriptor.close();raise
            pending=asyncio.create_task(start());self.starting.add(pending)
            try:worker=await _joined(pending)
            finally:self.starting.discard(pending)
        if self.closed:
            await worker.close();self.workers.discard(worker);raise NativeConfigurationError('native worker owner is closing')
        if worker.sequence==(1<<64)-1:
            await worker.close();self.workers.discard(worker);raise NativeProcessError('native worker scope exhausted')
        worker.sequence+=1;lease=Invocation(worker,worker.sequence);worker.current=lease;return lease
    async def release(self,lease,*,healthy):
        worker=lease.worker
        if worker.pool is not self:raise NativeProcessError('native invocation owner differs')
        if worker.current is not lease:raise NativeProcessError('native invocation scope is no longer owned')
        reusable=(healthy and lease.stdin.closed and lease.frames.empty() and lease.ready and lease.done.done() and lease.returncode==0
            and not worker.retired and worker.process.returncode is None and not self.closed)
        worker.current=None
        if reusable and self.idle_workers:
            self.idle[worker]=None
            while len(self.idle)>self.idle_workers:
                old,_=self.idle.popitem(last=False);await old.close();self.workers.discard(old)
        else:await worker.close();self.workers.discard(worker)
    async def _close(self):
        if self.starting:await asyncio.gather(*tuple(self.starting),return_exceptions=True)
        results=await asyncio.gather(*(worker.close() for worker in tuple(self.workers)),return_exceptions=True)
        self.idle.clear()
        failures=[result for result in results if isinstance(result,BaseException)]
        if failures:raise BaseExceptionGroup('native workers did not close cleanly',failures)
        self.workers.clear()
    async def close(self):
        self._bind();self.closed=True
        if self.closing is None:self.closing=asyncio.create_task(self._close())
        await _joined(self.closing)
