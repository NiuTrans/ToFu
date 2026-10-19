"""Paused memory snapshots of an explicitly owned disposable process tree.

Entry StoppedTree.snapshot. pidfds bind signals to identity; all threads stop
before KCMP_VM and procfs reads. Every exit resumes only our stops and closes
handles. Unsupported/denied observations raise, never infer shared memory.
Kernel authority: https://man7.org/linux/man-pages/man2/kcmp.2.html
"""
import ctypes
from contextlib import contextmanager
import os
from pathlib import Path
import platform
import signal
import time


class VmSamplingError(RuntimeError):
    pass


class LinuxMemoryKernel:
    # Linux libc syscall numbers for the native artifact's two admitted targets.
    NUMBERS={'x86_64':(312,434,424),'aarch64':(272,434,424)}
    def __init__(self):
        if platform.system()!='Linux' or platform.machine() not in self.NUMBERS:
            raise VmSamplingError('paused VM sampling requires supported Linux syscall ABI')
        self.compare_number,self.open_number,self.signal_number=self.NUMBERS[platform.machine()]
        self.lib=ctypes.CDLL(None,use_errno=True);self.lib.syscall.restype=ctypes.c_long

    def call(self,number,*args):
        ctypes.set_errno(0);result=self.lib.syscall(number,*args)
        if result<0:raise OSError(ctypes.get_errno(),os.strerror(ctypes.get_errno()))
        return result

    def open(self,pid):return self.call(self.open_number,pid,0)
    def send(self,fd,value):return self.call(self.signal_number,fd,value,ctypes.c_void_p(),0)
    def same_vm(self,first,second):return self.call(self.compare_number,first,second,1,0,0)==0


class OwnedProcess:
    def __init__(self,process,kernel):
        import psutil
        self.process,self.kernel=process,kernel
        self.identity=(process.pid,process.create_time())
        self.fd=kernel.open(process.pid);self.stopped=False
        try:
            if (process.pid,psutil.Process(process.pid).create_time())!=self.identity:
                raise VmSamplingError('process changed while acquiring pidfd')
        except BaseException:
            os.close(self.fd);raise

    def stop(self):
        state=self.process.status()
        if state in ('zombie','dead'):return
        if state in ('stopped','tracing-stop'):
            raise VmSamplingError('owned test process was already stopped by another owner')
        self.kernel.send(self.fd,signal.SIGSTOP);self.stopped=True

    def close(self):
        try:
            if self.stopped:
                try:self.kernel.send(self.fd,signal.SIGCONT)
                except ProcessLookupError:pass
        finally:os.close(self.fd)


def all_threads_stopped(process):
    """Check the entire thread group, not just its leader's procfs state."""
    if process.status() in ('zombie','dead'):return True
    directory=Path('/proc')/str(process.pid)/'task'
    entries=list(directory.iterdir())
    if not entries:return False
    for entry in entries:
        try:text=(entry/'status').read_text()
        except FileNotFoundError:return False
        state=next((line.split()[1] for line in text.splitlines() if line.startswith('State:')),None)
        if state not in ('T','t'):return False
    return {p.name for p in directory.iterdir()}=={p.name for p in entries}


class StoppedTree:
    def __init__(self,root,*,kernel=None,timeout=1):
        if root.ppid()!=os.getpid():raise VmSamplingError('sampler requires its own direct worker child')
        self.root=root;self.identity=(root.pid,root.create_time())
        self.kernel=kernel or LinuxMemoryKernel();self.timeout=timeout

    @contextmanager
    def stopped(self):
        import psutil
        handles={};deadline=time.monotonic()+self.timeout
        try:
            if (self.root.pid,self.root.create_time())!=self.identity:
                raise VmSamplingError('worker identity changed')
            root=OwnedProcess(self.root,self.kernel);handles[root.identity]=root;root.stop()
            while True:
                if time.monotonic()>=deadline:raise VmSamplingError('test tree did not quiesce before memory deadline')
                if not all_threads_stopped(self.root):time.sleep(.001);continue
                processes=[self.root,*self.root.children(recursive=True)]
                for process in processes:
                    try:
                        identity=(process.pid,process.create_time())
                        if identity not in handles:
                            handle=OwnedProcess(process,self.kernel);handles[identity]=handle;handle.stop()
                    except psutil.NoSuchProcess:continue
                if any(not all_threads_stopped(h.process) for h in handles.values()):
                    time.sleep(.001);continue
                current={(p.pid,p.create_time()) for p in [self.root,*self.root.children(recursive=True)]}
                if current<=handles.keys():break
            yield handles
        finally:
            failures=[]
            for handle in reversed(list(handles.values())):
                try:handle.close()
                except BaseException as error:failures.append(error)
            if failures:raise BaseExceptionGroup('owned memory sampling cleanup failed',failures)

    def snapshot(self):
        started=time.monotonic_ns()
        with self.stopped() as handles:
            rows=[];groups=[]
            for handle in handles.values():
                process=handle.process
                if process.status() in ('zombie','dead'):continue
                memory=process.memory_full_info();group=None
                for index,representative in enumerate(groups):
                    if self.kernel.same_vm(process.pid,representative):group=index;break
                if group is None:group=len(groups);groups.append(process.pid)
                rows.append(dict(pid=process.pid,created_at=handle.identity[1],name=process.name(),
                    vm_group=group,rss=memory.rss,pss=memory.pss,uss=memory.uss))
            if not rows or any(not all_threads_stopped(h.process) for h in handles.values()):
                raise VmSamplingError('test tree changed during memory observation')
            totals={metric:sum(max(row[metric] for row in rows if row['vm_group']==group)
                for group in range(len(groups))) for metric in ('rss','pss','uss')}
            return dict(all_threads_stopped=True,kernel_vm_comparison=True,members=rows,
                raw_process_rss_bytes=sum(r['rss'] for r in rows),raw_process_pss_bytes=sum(r['pss'] for r in rows),
                unique_vm_rss_bytes=totals['rss'],unique_vm_pss_bytes=totals['pss'],unique_vm_uss_bytes=totals['uss'],
                vm_groups=len(groups),pause_ns=time.monotonic_ns()-started)
