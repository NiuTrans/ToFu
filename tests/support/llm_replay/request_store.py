"""Content-addressed store of the normalized requests behind recorded digests.

``script.jsonl`` steps carry digests only.  The recorder also writes each
normalized request field to ``<store>/<field>-<digest prefix>.json`` so a
drift can be explained field by field (``diff.py``) and a re-record reviews
as an ordinary JSON diff (``git diff -M`` pairs a replaced value with its
predecessor).  Identity never comes from this store: the replayer still
matches the wholesale digest, and every load is verified against the step's
recorded field digest.  Identical values are stored once for all scenarios.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from tests.support.llm_replay.normalize import json_digest
from tests.support.llm_replay.script import ScriptError

__all__ = [
    'REQUEST_STORE_DIRNAME',
    'RequestStoreError',
    'load_recorded_field',
    'load_recorded_request',
    'store_request',
    'stored_request_path',
]

#: Shared store directory beside the scenario directories.
REQUEST_STORE_DIRNAME = 'requests'

_DIGEST_PREFIX_LENGTH = 16


class RequestStoreError(ScriptError):
    """A recorded request field is missing or no longer matches its digest."""


def stored_request_path(store_dir, field: str, digest: str) -> Path:
    return Path(store_dir) / f'{field}-{digest[:_DIGEST_PREFIX_LENGTH]}.json'


def load_recorded_field(store_dir, field: str, digest: str | None):
    """The normalized value recorded for ``field``, verified by digest."""
    if not digest:
        raise RequestStoreError(f'the step records no digest for {field!r}')
    path = stored_request_path(store_dir, field, digest)
    if not path.is_file():
        raise RequestStoreError(f'{path.name} is not stored in {path.parent}')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as error:
        raise RequestStoreError(f'{path} is not valid JSON: {error}') from error
    if json_digest(value) != digest:
        raise RequestStoreError(
            f'{path} does not hash to the recorded {field} digest {digest}')
    return value


def load_recorded_request(store_dir, field_digests: dict[str, str]) -> dict:
    """Reassemble one recorded normalized request from its field digests."""
    return {field: load_recorded_field(store_dir, field, digest)
            for field, digest in field_digests.items()}


def store_request(store_dir, request: dict,
                  field_digests: dict[str, str]) -> None:
    """Write every field of one normalized request under its digest.

    An existing entry is verified, never rewritten.  New entries are written
    atomically because parallel scenario workers may record the same value.
    """
    for field, digest in field_digests.items():
        path = stored_request_path(store_dir, field, digest)
        if path.exists():
            load_recorded_field(store_dir, field, digest)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(
            f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
        staging.write_text(
            json.dumps(request[field], ensure_ascii=False, sort_keys=True,
                       indent=1) + '\n',
            encoding='utf-8')
        os.replace(staging, path)
