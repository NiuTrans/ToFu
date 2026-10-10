"""Typed resource declarations and owned-release protocol; no runtime imports."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import json
from pathlib import Path
import threading
from typing import Callable


@lru_cache(maxsize=1)
def catalog_document() -> dict:
    return json.loads((Path(__file__).resolve().parents[2]
                       / 'contracts/resource_policy_v1.json').read_text())


@dataclass(frozen=True)
class ResourceSample:
    capacity: int
    used: int = 0
    waiting: int = 0
    completed: int = 0
    errors: int = 0
    blocked: int = 0
    pressure: bool = False
    ceiling: int | None = None


@dataclass(frozen=True)
class ResourceOwner:
    owner_user_id: int
    task_id: str

    def __post_init__(self):
        if (type(self.owner_user_id) is not int or self.owner_user_id <= 0
                or not isinstance(self.task_id, str) or not self.task_id.strip()):
            raise ValueError('resource ownership requires a user and task identity')


@dataclass
class ResourcePort:
    """The original resource owner remains the sole accounting authority."""
    read: Callable[[], ResourceSample]
    resize: Callable[[int], None]
    minimum: int
    maximum: int
    acquire: Callable | None = None
    automatic: bool = True
    growth_cost_bytes: int = 0


@dataclass(frozen=True)
class ResourcePlan:
    revision: int
    changes: tuple[tuple[str, int], ...]
    reason: str
    controller_id: str


class ResourceLease:
    """Exactly one release of the underlying owner's token, with no counter."""
    def __init__(self, resource_id: str, owner: ResourceOwner, release: Callable):
        self.resource_id = resource_id
        self.owner = owner
        self._release = release
        self._lock = threading.Lock()
        self._released = False

    def release(self):
        with self._lock:
            if self._released:
                return False
            result = self._release()
            self._released = True
            return result

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


@dataclass
class ResourceState:
    port: ResourcePort
    desired: int
    effective: int
    reason: str = 'launch'
    status: str = 'applied'
    last_change: float = float('-inf')
    backoff_until: float = 0
    healthy_samples: int = 0
    previous_sample: ResourceSample | None = None
    previous_time: float = 0
    completion_rate: float = 0
    trial: tuple[int, float, int, float] | None = None
    pressure_capacity: int | None = None
    sample: ResourceSample = field(default_factory=lambda: ResourceSample(0))
