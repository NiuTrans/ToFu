"""Agent-facing imports of the shared dependency-free native image owner.

The implementation lives in lib.native_binary so SQL Sidecar composition can
verify/seal code without importing the task execution package.
"""
from lib.native_binary import (
    NativeProcessError, NativeConfigurationError, MEMFD_CDEF,
    _memfd, _seal, _ADD_SEALS, _GET_SEALS, _WRITE_SEAL, _ALL_SEALS,
)
