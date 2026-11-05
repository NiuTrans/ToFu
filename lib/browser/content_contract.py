"""Canonical target and receipt contract for authenticated page content.

Schemas feed model tools and executable boundary tests. This module owns only
bounded values, never browser transport, access decisions, or persistence.
Collectors in ``content`` consume ReadTarget and return CONTENT_RECEIPT_SCHEMA.
JsonReadSource/JSON_READ_SOURCE_SCHEMA own the observed addresses of dedicated
JSON data reads; live policy remains in BrowserPage and acceptance in read_pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from urllib.parse import urlsplit

READ_TARGET_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'kind': {'type': 'string', 'enum': ['auto', 'list', 'article', 'record']},
        'query': {'type': 'string', 'maxLength': 512,
                  'description': 'Topic/entity hint, not authority or a page query.'},
        'fields': {'type': 'array', 'maxItems': 12, 'uniqueItems': True,
                   'items': {'type': 'string', 'minLength': 1, 'maxLength': 64},
                   'description': 'Desired fields; absence is reported.'},
    },
}
CONTENT_RECEIPT_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['status', 'title', 'url', 'content', 'records', 'coverage', 'evidence'],
    'properties': {
        'status': {'enum': ['ok', 'partial', 'empty', 'blocked', 'unavailable']},
        'title': {'type': 'string', 'maxLength': 512},
        'url': {'type': 'string', 'maxLength': 8192},
        'content': {'type': 'string', 'maxLength': 80000},
        'records': {'type': 'array', 'maxItems': 100, 'items': {
            'type': 'object', 'required': ['title', 'url', 'snippet'],
            'properties': {'title': {'type': 'string', 'maxLength': 512},
                           'url': {'type': 'string', 'maxLength': 8192},
                           'snippet': {'type': 'string', 'maxLength': 2000},
                           'fields': {'type': 'object', 'maxProperties': 12}},
            'additionalProperties': False,
        }},
        'coverage': {'type': 'object', 'additionalProperties': False,
                     'required': ['scope', 'truncated', 'stop_reason', 'missing_fields', 'media_unread'],
                     'properties': {
                         'scope': {'enum': ['observed_page', 'observed_records', 'none']},
                         'truncated': {'type': 'boolean'},
                         'stop_reason': {'type': 'string', 'maxLength': 128},
                         'missing_fields': {'type': 'array', 'maxItems': 12, 'items': {'type': 'string'}},
                         'media_unread': {'type': 'boolean'},
                     }},
        'evidence': {'type': 'array', 'maxItems': 12, 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['strategy', 'url'],
            'properties': {
                'strategy': {'enum': ['rendered_dom', 'captured_api', 'hydrated_state']},
                'url': {'type': 'string', 'maxLength': 8192},
            },
        }},
    },
}


@dataclass(frozen=True)
class ReadTarget:
    """A request-scoped selection hint, deliberately not an authority token."""

    kind: str = 'auto'
    query: str = ''
    fields: tuple[str, ...] = ()

    @classmethod
    def from_value(cls, value=None) -> 'ReadTarget':
        if isinstance(value, cls):
            value = {'kind': value.kind, 'query': value.query, 'fields': list(value.fields)}
        if value is None:
            return cls()
        if not isinstance(value, dict) or set(value) - {'kind', 'query', 'fields'}:
            raise ValueError('target must contain only kind, query, and fields')
        kind = value.get('kind', 'auto')
        query = value.get('query', '')
        fields = value.get('fields', [])
        if kind not in ('auto', 'list', 'article', 'record'):
            raise ValueError('target.kind must be auto, list, article, or record')
        if not isinstance(query, str) or len(query) > 512:
            raise ValueError('target.query must be a string of at most 512 characters')
        if not isinstance(fields, (list, tuple)) or len(fields) > 12 or any(
                not isinstance(field, str) or not field.strip() or len(field) > 64
                for field in fields):
            raise ValueError('target.fields must contain at most 12 nonempty field names')
        if len(set(fields)) != len(fields):
            raise ValueError('target.fields must be unique')
        return cls(kind=kind, query=query.strip(), fields=tuple(fields))

    def as_dict(self) -> dict:
        return {'kind': self.kind, 'query': self.query, 'fields': list(self.fields)}


def content_budget(value, default=30000) -> int:
    """One non-growing output ceiling; invalid direct callers fail explicitly."""
    if value is None:
        value = default
    if isinstance(value, bool):
        raise ValueError('max_chars must be an integer')
    try:
        return max(1000, min(80000, int(value)))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('max_chars must be an integer') from exc


def _http_origin(url) -> tuple[str, str, int]:
    if not isinstance(url, str):
        raise ValueError('JSON source URLs must be absolute HTTP(S) URLs')
    parts = urlsplit(url)
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username is not None or parts.password is not None):
        raise ValueError('JSON source URLs must be absolute HTTP(S) URLs without credentials')
    port = parts.port
    default_port = 443 if parts.scheme == 'https' else 80
    return parts.scheme, parts.hostname, default_port if port is None else port


@dataclass(frozen=True)
class JsonReadSource:
    """Observed addresses of a same-origin data read, never a permission token."""

    document_url: str
    request_url: str
    response_url: str

    @classmethod
    def from_value(cls, value, *, expected_document_url: str) -> 'JsonReadSource':
        if not isinstance(value, dict) or set(value) != set(JSON_READ_SOURCE_SCHEMA['required']):
            raise ValueError('JSON source requires document_url, request_url and response_url')
        source = cls(**value)
        origin = _http_origin(expected_document_url)
        if any(_http_origin(url) != origin for url in source.urls):
            raise ValueError('JSON source must remain on the captured document origin')
        return source

    @property
    def urls(self) -> tuple[str, str, str]:
        return self.document_url, self.request_url, self.response_url


JSON_READ_SOURCE_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': [field.name for field in fields(JsonReadSource)],
    'properties': {field.name: {'type': 'string', 'format': 'uri', 'minLength': 1}
                   for field in fields(JsonReadSource)},
    'description': 'Observed document, request and final response URLs; all must share the captured document origin and pass live read policy.',
}


__all__ = ['READ_TARGET_SCHEMA', 'CONTENT_RECEIPT_SCHEMA', 'ReadTarget', 'content_budget',
           'JSON_READ_SOURCE_SCHEMA', 'JsonReadSource']
