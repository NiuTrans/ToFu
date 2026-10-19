"""Trusted SDK/application selection of the installed native repository scanner.

Request JSON and model tool arguments cannot construct this owner. Limits come
from trusted explicit overrides or an already-issued launch policy. A None grammar
directory explicitly selects the installed pinned bundle; a path remains trusted
external configuration. Full performance and catalog coverage remain separate.
"""
from dataclasses import dataclass
import math
import os


@dataclass(frozen=True)
class NativeRepositoryConfiguration:
    grammar_directory: str | None
    limits: tuple[int, int, int, int]
    timeout: float = 60.0

    def __post_init__(self):
        from lib.repo_map._native_limits import StreamLimits
        directory = os.fsdecode(self.grammar_directory) if self.grammar_directory is not None else None
        if directory is not None and (not directory or '\0' in directory):
            raise ValueError('native repository requires an explicit grammar directory')
        limits = tuple(self.limits)
        StreamLimits(*limits)
        if isinstance(self.timeout, bool) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('native repository timeout must be positive and finite')
        object.__setattr__(self, 'grammar_directory', os.path.abspath(directory) if directory is not None else None)
        object.__setattr__(self, 'limits', limits)

    def create_runtime(self):
        from lib.repo_map.native_runtime import NativeRepositoryRuntime
        return NativeRepositoryRuntime(self.grammar_directory, self.limits, timeout=self.timeout)

    @classmethod
    def from_launch_policy(cls, *, launch_policy, grammar_directory=None, timeout=60.0):
        """Compile lazy repository configuration from the existing launch probe."""
        from lib.repo_map._native_limits import StreamLimits
        limits = StreamLimits.from_launch_policy(launch_policy)
        return cls(grammar_directory=grammar_directory, limits=limits.arguments(), timeout=timeout)
