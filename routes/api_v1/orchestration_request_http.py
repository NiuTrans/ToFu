"""Typed request-preparation result shared by orchestration HTTP adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast


_ValueT = TypeVar('_ValueT')
_ResponseT = TypeVar('_ResponseT')


@dataclass(frozen=True, eq=False)
class OrchestrationHttpPreparation(Generic[_ValueT]):
    """One explicit accepted/rejected ingress result.

    ``accepted`` is intentionally independent from ``value``: an omitted
    optional precondition is a valid ``None`` value, while a rejected request
    carries a framework response in ``failure``. Iteration preserves the
    former ``(value, failure)`` contract for rolling callers and tests.
    """

    accepted: bool
    value: _ValueT | None = None
    failure: Any | None = None
    __hash__ = None

    def __post_init__(self) -> None:
        if self.accepted and self.failure is not None:
            raise ValueError('accepted HTTP preparation cannot have a failure')
        if not self.accepted and self.failure is None:
            raise ValueError('rejected HTTP preparation requires a failure')

    @classmethod
    def accept(cls, value: _ValueT) -> OrchestrationHttpPreparation[_ValueT]:
        return cls(accepted=True, value=value)

    @classmethod
    def reject(cls, failure: Any) -> OrchestrationHttpPreparation[_ValueT]:
        return cls(accepted=False, failure=failure)

    def require(self) -> _ValueT:
        if not self.accepted:
            raise RuntimeError('cannot require a rejected HTTP preparation')
        return cast(_ValueT, self.value)

    def __iter__(self) -> Iterator[Any]:
        yield self.value if self.accepted else None
        yield None if self.accepted else self.failure

    def __eq__(self, other: object) -> bool:
        if isinstance(other, OrchestrationHttpPreparation):
            return (
                self.accepted == other.accepted
                and self.value == other.value
                and self.failure == other.failure
            )
        if isinstance(other, tuple) and len(other) == 2:
            return tuple(self) == other
        return NotImplemented


def _coerce_preparation(
    preparation: OrchestrationHttpPreparation[_ValueT]
    | tuple[_ValueT | None, Any | None],
) -> OrchestrationHttpPreparation[_ValueT]:
    """Adopt a rolling caller's legacy ``(value, failure)`` pair once."""
    if isinstance(preparation, OrchestrationHttpPreparation):
        return preparation
    value, failure = preparation
    if failure is not None:
        return OrchestrationHttpPreparation.reject(failure)
    return OrchestrationHttpPreparation.accept(cast(_ValueT, value))


def orchestration_request_response(
    preparation: OrchestrationHttpPreparation[_ValueT]
    | tuple[_ValueT | None, Any | None],
    handler: Callable[[_ValueT], _ResponseT],
) -> _ResponseT | Any:
    """Dispatch an accepted request once or return its prepared failure."""
    preparation = _coerce_preparation(preparation)
    if not preparation.accepted:
        return preparation.failure
    return handler(preparation.require())


__all__ = [
    'OrchestrationHttpPreparation',
    'orchestration_request_response',
]
