"""Request-local admission policy for already-observed billing stops.

Interactive dispatch preserves the historical Settings contract: a manual
``override=True`` is user supremacy and may retry a key after a quota error.
Optional background work can enter :func:`strict_billing_stop_admission` to
spend only capacity that has not already reported a key- or model-level
billing stop.  A ContextVar keeps that narrower policy local to the current
request and makes nested callers restore their owner cleanly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


_STRICT_BILLING_STOP_ADMISSION: ContextVar[bool] = ContextVar(
    'strict_billing_stop_admission', default=False)


def is_strict_billing_stop_admission() -> bool:
    """Return whether this execution context must honor recorded stops."""
    return _STRICT_BILLING_STOP_ADMISSION.get()


@contextmanager
def strict_billing_stop_admission() -> Iterator[None]:
    """Temporarily prevent optional work from challenging billing stops."""
    token = _STRICT_BILLING_STOP_ADMISSION.set(True)
    try:
        yield
    finally:
        _STRICT_BILLING_STOP_ADMISSION.reset(token)
