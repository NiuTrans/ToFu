"""Native limits for the short-lived PDF validator; never imported by the UI.

POSIX uses address-space, CPU and wall limits. Windows uses a process-memory
Job Object plus a daemon wall deadline. The supervising build worker also
checks resident memory/cancellation and always reaps the child.
"""
from __future__ import annotations

import math
import os


def apply_limits(memory_bytes: int, seconds: float):
    if os.name != 'nt':
        import resource
        import signal
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        cpu = max(1, math.ceil(seconds))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        return None
    import ctypes
    from ctypes import wintypes
    import threading
    size = ctypes.c_size_t
    class BasicLimits(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                    ('flags', wintypes.DWORD), ('min_working_set', size), ('max_working_set', size),
                    ('process_count', wintypes.DWORD), ('affinity', size),
                    ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('basic', BasicLimits), ('io', ctypes.c_uint64 * 6),
                    ('process_memory', size), ('job_memory', size),
                    ('peak_process_memory', size), ('peak_job_memory', size)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    limits.basic.flags = 0x100 | 0x2000  # PROCESS_MEMORY | KILL_ON_JOB_CLOSE
    limits.process_memory = memory_bytes
    if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        raise OSError(ctypes.get_last_error(), 'Cannot bound the PDF validation process')
    deadline = threading.Timer(seconds, lambda: os._exit(3))
    deadline.daemon = True
    deadline.start()
    # Keep the job handle alive until process exit. There is no resident pool.
    return job, deadline


def observed_memory_bytes() -> int:
    """Measure the current executable, excluding pre-exec inherited rusage.

    Linux/Windows expose a current-image high-water value. Other platforms
    report current RSS; the supervisor combines it with its own RSS samples.
    Native allocation limits remain independent of this observation.
    """
    import psutil
    if os.name == 'nt':
        return psutil.Process().memory_info().peak_wset
    import sys
    if sys.platform == 'linux':
        try:
            with open('/proc/self/status', 'rb') as source:
                for line in source.read(16384).splitlines():
                    if line.startswith(b'VmHWM:'):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
    return psutil.Process().memory_info().rss
