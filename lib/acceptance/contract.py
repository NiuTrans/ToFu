"""Owner review wire authority; lazy schema validation and OpenAPI projection.

Entry points: contract, validate. This module never opens storage or a task.
"""
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def contract() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] /
                       'contracts/artifact_review_v1.schema.json').read_text())


@lru_cache(maxsize=32)
def _validator(name):
    from jsonschema import Draft202012Validator
    schema = contract()
    return Draft202012Validator({**schema, '$ref': f'#/$defs/{name}'})


def validate(value, name='ArtifactReviewCommand'):
    error = next(_validator(name).iter_errors(value), None)
    if error:
        raise ValueError('Invalid owner review: ' + error.message[:300])
    return value
