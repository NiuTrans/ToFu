"""Transaction boundary owned by the plugin's sidecar repository seam."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


@contextmanager
def write_transaction(db, *, label: str = 'write') -> Iterator[object]:
    """Run one atomic, bounded sidecar mutation batch.

    ``label`` remains part of the call contract for diagnostic readability;
    command IDs and error classification are owned by the repository.
    """
    del label
    if db is None:
        raise TypeError('write_transaction() requires a data-layer connection')
    db.begin()
    try:
        yield db
    except BaseException:
        db.rollback()
        raise
    else:
        db.commit()


__all__ = ['write_transaction']
