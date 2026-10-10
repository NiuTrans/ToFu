"""Native process-tree ownership for the MCP Git supervisor.

POSIX workers stay in one session. Windows workers wait for admission into a
kill-on-close Job Object before dispatch, so descendants share its lifetime.
"""
from __future__ import annotations

import os
import signal

import psutil


class ProcessTree:
    def __init__(self, process, memory_bytes):
        self.process = process
        self.job = None
        self.observed = {}
        if os.name == 'nt':
            self._windows_job(memory_bytes)

    def _windows_job(self, memory_bytes):
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
                        ('max_working_set', ctypes.c_size_t), ('process_count', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x200 | 0x2000  # JOB_MEMORY | KILL_ON_JOB_CLOSE
        limits.job_memory = memory_bytes
        if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(self.process._handle)):
            if job:
                kernel.CloseHandle(job)
            raise OSError('Cannot own the Git worker process tree')
        self.job = (kernel, job)

    def sample(self):
        self.observed = {pid: process for pid, process in self.observed.items() if process.is_running()}
        try:
            parent = psutil.Process(self.process.pid)
            processes = [parent, *parent.children(recursive=True)]
        except psutil.NoSuchProcess:
            processes = []
        rss = 0
        for process in processes:
            try:
                rss += process.memory_info().rss
                self.observed[process.pid] = process
            except psutil.NoSuchProcess:
                pass
        return rss, len(processes)

    def close(self):
        if self.job:
            kernel, job = self.job
            kernel.CloseHandle(job)
            self.job = None
        elif os.name != 'nt':
            # Also retire children if the worker exited before its Git command.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            self.process.kill()
        for process in self.observed.values():
            try:
                process.kill()  # psutil guards against PID reuse.
            except psutil.NoSuchProcess:
                pass
        self.process.wait(timeout=5)
        psutil.wait_procs(list(self.observed.values()), timeout=1)
