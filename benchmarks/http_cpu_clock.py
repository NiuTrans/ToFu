"""Unprofiled Linux CPU accounting for an explicitly owned live server PID.

Concurrent requests share process CPU; sample at cohort barriers, never assign
one overlapping interval repeatedly to individual requests. PID lifetime belongs
to the caller, which must keep its subprocess alive until sampling ends.
"""
import ctypes
import os
import sys
import time


class OwnedProcessCpuClock:
    def __init__(self,pid):
        if type(pid) is not int or pid<=0:raise ValueError('explicit positive owned PID required')
        self.pid=pid
        if not sys.platform.startswith('linux'):
            raise RuntimeError('remote owned-process CPU clock requires Linux')
        function=ctypes.CDLL(None).clock_getcpuclockid
        function.argtypes=[ctypes.c_int,ctypes.POINTER(ctypes.c_int)]
        function.restype=ctypes.c_int
        clock_id=ctypes.c_int()
        error=function(pid,ctypes.byref(clock_id))
        if error:raise OSError(error,os.strerror(error))
        self.clock_id=clock_id.value
        self.read_ns()
    def read_ns(self):
        return time.clock_gettime_ns(self.clock_id)
