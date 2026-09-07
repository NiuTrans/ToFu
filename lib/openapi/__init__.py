"""lib/openapi/ — OpenAPI 3.1 spec generation (facade package).

Provides:
  - ``@api_meta(...)`` decorator that attaches OpenAPI metadata to a
    route handler (summary, description, tags, scopes, request/response
    schemas).
  - ``build_spec(app)`` that walks ``app.url_map`` and builds a full
    OpenAPI 3.1 document.
  - ``swagger_html(spec_url)`` / ``redoc_html(spec_url)`` for the
    interactive viewers (single-file CDN bundles, no build step — meets
    CLAUDE.md §3.2 "no frameworks, no build step").

The decorator stores metadata on the function object (``fn._api_meta``).
``build_spec`` reads it back during introspection — there is NO
module-level registry, so metadata lives per-handler. Routes without
``@api_meta`` are still listed (with a generic ``operationId`` and no
schemas) so the spec stays drift-free even if a developer forgets.

Package layout
--------------
This module is a **facade**: it re-exports every public symbol (and the
test-facing private helpers) so historic call sites keep working
byte-identically::

    from lib.openapi import api_meta, build_spec, swagger_html, redoc_html
    from lib.openapi import _flask_to_openapi_path, _path_parameters  # tests

Sub-modules:
  _meta    — ``api_meta`` decorator + its fn-attribute metadata storage
  _paths   — Flask↔OpenAPI path conversion, skip rules, auto-tags,
             default description
  _schema  — ``_default_responses`` / ``_tofu_config_schema`` /
             ``_components``
  _spec    — ``build_spec`` document assembly
  _docs    — ``swagger_html`` / ``redoc_html`` embed pages
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

from lib.openapi._meta import api_meta  # noqa: E402
from lib.openapi._paths import (  # noqa: E402
    _FLASK_VAR_RE,
    _SKIP_PATTERNS,
    _auto_tags,
    _default_description,
    _flask_to_openapi_path,
    _path_parameters,
    _skip_rule,
)
from lib.openapi._schema import (  # noqa: E402
    _components,
    _default_responses,
    _tofu_config_schema,
)
from lib.openapi._spec import build_spec  # noqa: E402
from lib.openapi._docs import redoc_html, swagger_html  # noqa: E402

__all__ = ['api_meta', 'build_spec', 'swagger_html', 'redoc_html']
