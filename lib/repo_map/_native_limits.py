"""Native stream byte bounds, shared by configuration and transport.

The canonical outline owns source/reference admission sizes. This module loads
no optional native or parser package and creates no runtime resources.
"""
from dataclasses import dataclass
import sys
from . import outline


def source_read_minimum():
    return max(outline._MAX_SOURCE_BYTES, outline._MAX_REF_BYTES * 4 + 4)


@dataclass(frozen=True, init=False, repr=False, eq=False)
class StreamLimits:
    path_bytes: int
    request_bytes: int
    file_frame_bytes: int
    source_read_bytes: int

    # These four small value methods avoid compiling them during the first
    # native call. Keep dataclass fields, replacement/copy and frozen behavior.
    def __init__(self, path_bytes: int, request_bytes: int, file_frame_bytes: int, source_read_bytes: int) -> None:
        object.__setattr__(self, 'path_bytes', path_bytes)
        object.__setattr__(self, 'request_bytes', request_bytes)
        object.__setattr__(self, 'file_frame_bytes', file_frame_bytes)
        object.__setattr__(self, 'source_read_bytes', source_read_bytes)
        self.__post_init__()

    def __repr__(self):
        return (f'{type(self).__qualname__}(path_bytes={self.path_bytes!r}, '
                f'request_bytes={self.request_bytes!r}, file_frame_bytes={self.file_frame_bytes!r}, '
                f'source_read_bytes={self.source_read_bytes!r})')

    def __eq__(self, other):
        if type(other) is not type(self):
            return NotImplemented
        return self.arguments() == other.arguments()

    def __hash__(self):
        return hash(self.arguments())

    @classmethod
    def from_launch_policy(cls, launch_policy):
        """Reuse issued frame capacity; never probe or reserve resident memory.

        Path/request/output ceilings scale with available launch capacity.
        Source reads stop at canonical admission size, including the reference
        prefix allowance; a stat/read race cannot expand that semantic bound.
        """
        from collections.abc import Mapping
        capacity = launch_policy.get('frame_maximum') if isinstance(launch_policy, Mapping) else None
        minimum = source_read_minimum()
        if type(capacity) is not int or not minimum <= capacity < sys.maxsize:
            raise ValueError('native repository requires valid launch capacity for canonical source admission')
        return cls(capacity, capacity, capacity, minimum)

    def __post_init__(self):
        if any(type(value) is not int or not 0 < value < sys.maxsize
               for value in self.arguments()):
            raise ValueError('stream budgets must be positive bounded integers')
        if self.source_read_bytes < source_read_minimum():
            raise ValueError('source budget cannot hold canonical source/reference limits')

    def arguments(self):
        return self.path_bytes, self.request_bytes, self.file_frame_bytes, self.source_read_bytes
