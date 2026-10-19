"""Working-fact schema and limits, loaded only at this capability's boundary."""
from functools import lru_cache
import json
from pathlib import Path


class FactInputError(ValueError):
    """The proposal must be corrected before another write is attempted."""


@lru_cache(maxsize=1)
def contract():
    return json.loads((Path(__file__).resolve().parents[3] /
                       'contracts/working_facts_v1.schema.json').read_text())


@lru_cache(maxsize=8)
def _validator(name):
    from jsonschema import Draft202012Validator
    return Draft202012Validator({**contract(), '$ref': '#/$defs/' + name})


def validate(value, name):
    error = next(_validator(name).iter_errors(value), None)
    if error:
        raise FactInputError('Invalid working facts request: ' + error.message[:300])
    return value


def limit(name):
    return contract()['x-limits'][name]
