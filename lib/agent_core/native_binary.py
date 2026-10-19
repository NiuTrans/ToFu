"""Agent-facing imports of the shared dependency-free native image owner.

The implementation lives in lib.native_binary so SQL Sidecar composition can
verify/seal code without importing the task execution package.
"""
from lib.native_binary import (
    NativeProcessError as NativeProcessError,
    NativeConfigurationError as NativeConfigurationError,
    MEMFD_CDEF as MEMFD_CDEF,
    _memfd as _memfd,
    _seal as _seal,
    _ADD_SEALS as _ADD_SEALS,
    _GET_SEALS as _GET_SEALS,
    _WRITE_SEAL as _WRITE_SEAL,
    _ALL_SEALS as _ALL_SEALS,
)
