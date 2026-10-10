"""Lossless JSON syntax for model-only native receipt projections.

Numbers keep their validated JSON token, including large integers, exponents
and negative zero. Duplicate keys/nonfinite constants are not safe to project.
This module does not parse tool output text inside a receipt or grant authority.
"""
from __future__ import annotations
import json


class JSONNumber(str):
    """A numeric JSON token, not an application string or rounded Python float."""


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON key')
        value[key] = item
    return value


def _nonfinite(_token):
    raise ValueError('non-JSON numeric constant')


def load_projection_json(text):
    return json.loads(text, object_pairs_hook=_unique_object,
                      parse_int=JSONNumber, parse_float=JSONNumber,
                      parse_constant=_nonfinite)


def dump_projection_json(value):
    if isinstance(value, JSONNumber):
        return str(value)
    if isinstance(value, dict):
        return '{' + ','.join(dump_projection_json(k) + ':' + dump_projection_json(v)
                              for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(dump_projection_json(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
