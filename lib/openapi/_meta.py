"""lib/openapi/_meta.py — the ``@api_meta`` decorator.

Storage mechanism
-----------------
``api_meta`` attaches its metadata dict directly to the *function object*
(``fn._api_meta`` — and, when it wraps the handler, ``wrapper._api_meta``
too). There is **no module-global registry**: ``build_spec`` reads the
metadata back per-view via ``getattr(view, '_api_meta', None)`` while it
walks ``app.url_map``. Because the state lives on each handler, this
submodule is self-contained and shares nothing by reference with the
spec builder.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Optional

from lib.log import get_logger

logger = get_logger(__name__)


def api_meta(*, summary: str = '', description: str = '',
             tags: Optional[list[str]] = None,
             scope: str = '',
             request_body: Optional[dict] = None,
             responses: Optional[dict] = None,
             parameters: Optional[list[dict]] = None,
             deprecated: bool = False,
             public: bool = False) -> Any:
    """Attach OpenAPI metadata to a route handler.

    Parameters
    ----------
    summary : str
        Short title for the operation (shown in Swagger UI's nav).
    description : str
        Long-form description (Markdown allowed).
    tags : list[str]
        OpenAPI tags — used for grouping in the UI.
    scope : str
        Required API-key scope (e.g. ``'chat'``, ``'admin'``). Becomes
        the security requirement on the operation.
    request_body : dict | None
        OpenAPI ``requestBody`` object. Caller supplies an inline schema
        or ``$ref``. If None, no body is documented.
    responses : dict | None
        Mapping of status code → response object. Defaults to a generic
        200 ``{ok:true}`` response when unspecified.
    parameters : list[dict] | None
        OpenAPI ``parameters`` list (path/query/header).
    deprecated : bool
        Mark the operation deprecated.
    public : bool
        If True, document the operation as not requiring auth (e.g.
        capabilities/health endpoints).
    """
    meta = {
        'summary': summary,
        'description': description,
        'tags': list(tags or []),
        'scope': scope,
        'request_body': request_body,
        'responses': responses,
        'parameters': list(parameters or []),
        'deprecated': bool(deprecated),
        'public': bool(public),
    }

    def decorator(fn):
        fn._api_meta = meta

        # Dual-mode: an async handler MUST stay a coroutine function or
        # Quart will run the sync wrapper in its thread-pool and try to
        # serialize the returned coroutine object as the response body.
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                return await fn(*args, **kwargs)
        else:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                return fn(*args, **kwargs)

        wrapper._api_meta = meta
        return wrapper

    return decorator
