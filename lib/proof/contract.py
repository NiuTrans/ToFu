"""Wire authority loader and validator for ``deliverable_proof_v1``.

Mirrors ``lib/paper/builds/contract.py``: only immutable contract data is
cached; jsonschema loads lazily at validation time so importing the package
stays dependency-free.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .errors import ProofError


@lru_cache(maxsize=1)
def schema() -> dict:
    return json.loads(
        (Path(__file__).resolve().parents[2]
         / 'contracts/deliverable_proof_v1.schema.json').read_text())


def limit(name: str) -> int:
    return int(schema()['x-resource-limits'][name])


def validate_bundle(value: dict) -> dict:
    """Validate one bundle against the wire authority, fail-closed.

    Raises ``ProofError('proof_contract', ...)`` naming the first offending
    field. Producers call this before publishing; the verifier maps it to a
    ``tampered`` verdict instead of raising.
    """
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator(schema())
    failure = next(validator.iter_errors(value), None)
    if failure:
        field = '.'.join(str(p) for p in failure.absolute_path) or 'bundle'
        raise ProofError(
            'proof_contract', f'Invalid proof bundle field: {field}')
    return value
