"""Keep framework JSON provenance until the response boundary consumes it."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from quart import Response, current_app, jsonify
from quart.json.provider import DefaultJSONProvider
from quart.wrappers.response import DataBody


@dataclass(frozen=True, slots=True)
class JSONRepresentation:
    encoded: bytes
    payload: dict[str, Any] | None
    sort_keys: bool

    def payload_in_wire_order(self) -> dict[str, Any] | None:
        # A matching 304 consumes no semantic value. Defer normalization checks
        # and ordering until a body is needed.
        if self.payload is None:
            return None
        # loads joins an explicitly stored surrogate pair into one Unicode
        # scalar. Such strings (including keys) must keep the normalization
        # path. The standard encoder escapes surrogate code units with lowercase
        # ud; its existing bytes can prove their absence without examining every
        # source character. Literal escapes/non-BMP text can trigger the check
        # too, but only actual surrogate code units require the decode fallback.
        if _SURROGATE_ESCAPE.search(self.encoded) is not None:
            for key, value in self.payload.items():
                if (_SURROGATE_CODE_UNITS.search(key) is not None
                        or (type(value) is str and _SURROGATE_CODE_UNITS.search(value) is not None)):
                    return None
        # json.loads preserves the provider's key order.
        if self.sort_keys:
            return dict(sorted(self.payload.items()))
        return self.payload


_REPRESENTATION_ATTRIBUTE = '_tofu_json_representation'
_IMMUTABLE_JSON_SCALARS = frozenset({str, int, float, bool, type(None)})
_PROVIDER_RESPONSE = DefaultJSONProvider.response
_PROVIDER_DUMPS = DefaultJSONProvider.dumps
_SURROGATE_ESCAPE = re.compile(br'\\ud')
_SURROGATE_CODE_UNITS = re.compile('[\ud800-\udfff]')


def _immutable_payload(payload: dict) -> dict | None:
    if not all(type(key) is str and type(value) in _IMMUTABLE_JSON_SCALARS
               for key, value in payload.items()):
        return None
    return payload


def owned_json_response(payload: dict) -> Response:
    """Serialize a helper-owned dict with the unchanged framework provider.

    Standard-provider bytes certify valid JSON, including when the borrowed
    nested values later change. Only a flat dict of exact immutable JSON
    scalars can also retain its semantic value without copying a mutable graph
    or repeating the provider's date/UUID/key normalization. The caller owns
    the outer dict and must not mutate it after this call.
    """
    provider = current_app.json
    response = jsonify(payload)
    if (type(provider) is not DefaultJSONProvider
            or getattr(provider.response, '__func__', None) is not _PROVIDER_RESPONSE
            or getattr(provider.dumps, '__func__', None) is not _PROVIDER_DUMPS
            or current_app.response_class is not Response
            or type(response) is not Response
            or type(response.response) is not DataBody):
        return response

    body = response.response
    if body.begin != 0 or body.end != len(body.data):
        return response

    setattr(response, _REPRESENTATION_ATTRIBUTE,
            JSONRepresentation(body.data, _immutable_payload(payload), provider.sort_keys))
    return response


def take_json_representation(response: Any, encoded: bytes) -> JSONRepresentation | None:
    """Consume provenance only if intervening hooks left the exact bytes intact."""
    representation = getattr(response, _REPRESENTATION_ATTRIBUTE, None)
    if not isinstance(representation, JSONRepresentation):
        return None
    delattr(response, _REPRESENTATION_ATTRIBUTE)
    if representation.encoded is encoded or representation.encoded == encoded:
        return representation
    return None
