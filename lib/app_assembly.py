"""Native Quart HTTP application assembly.

The base factory owns only the ASGI shell and lifespan dispatcher. This module
owns the repeatable HTTP recipe: middleware order, blueprint registration,
auth, static serving, database teardown and error mapping. Process-wide
production workers remain separate lifecycle handlers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from logging import Logger
from typing import Any

from lib.app_factory import create_base_app
from lib.app_lifecycle import add_shutdown_handler, add_startup_handler
from lib.http_body_policy import (
    HttpBodyPolicy,
    build_http_body_policy,
    register_http_body_policy,
)
from lib.http_compat_middleware import (
    register_method_override,
    register_static_cache_headers,
)
from lib.http_compression import register_http_compression
from lib.http_error_handlers import register_http_error_handlers
from lib.http_request_lifecycle import register_request_lifecycle
from lib.static_serving import (
    RangeGate,
    StaticOffload,
    TimeoutProvider,
    if_range_allows,
    load_static_bytes,
    register_static_route,
)


LifecycleRegistration = tuple[str, Callable[[], Any]]
StaticTimeout = float | TimeoutProvider
DEFAULT_MAX_CONTENT_LENGTH = 520 * 1024 * 1024


def _default_static_offload(static_dir: str) -> StaticOffload:
    async def offload(
        loop: asyncio.AbstractEventLoop,
        filename: str,
    ):
        return await loop.run_in_executor(
            None, load_static_bytes, static_dir, filename)

    return offload


def configure_application(
    app: Any,
    *,
    static_dir: str,
    logger: Logger,
    secret_key: str,
    body_policy: HttpBodyPolicy | None = None,
    max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH,
    static_timeout: StaticTimeout = 12,
    static_offload: StaticOffload | None = None,
    static_range_allows: RangeGate = if_range_allows,
    startup_handlers: Sequence[LifecycleRegistration] = (),
    shutdown_handlers: Sequence[LifecycleRegistration] = (),
) -> bool:
    """Apply the complete HTTP recipe exactly once to one Quart instance."""
    marker = 'tofu_http_application_assembled'
    if app.extensions.get(marker):
        return False

    if not app.secret_key:
        app.secret_key = secret_key
    if app.config.get('MAX_CONTENT_LENGTH') is None:
        app.config['MAX_CONTENT_LENGTH'] = max_content_length

    for name, handler in startup_handlers:
        add_startup_handler(app, handler, name=name)
    for name, handler in shutdown_handlers:
        add_shutdown_handler(app, handler, name=name)

    policy = body_policy or build_http_body_policy()
    register_http_body_policy(app, policy)
    register_http_compression(app)
    register_method_override(app)
    register_request_lifecycle(app)

    from lib.database import close_db
    app.teardown_appcontext(close_db)

    from routes import register_all
    register_all(app, start_workers=False)

    # Auth follows blueprint registration so blueprint-local middleware keeps
    # its existing precedence. Quart executes after-request hooks in reverse.
    from routes.api_v1.auth import attach_rate_headers, auth_before_request
    app.before_request(auth_before_request)
    app.after_request(attach_rate_headers)

    register_static_cache_headers(app)
    register_static_route(
        app,
        offload=static_offload or _default_static_offload(static_dir),
        timeout=static_timeout,
        logger=logger,
        range_allows=static_range_allows,
    )
    register_http_error_handlers(app)

    app.extensions[marker] = True
    app.extensions['tofu_app_assembly'] = {
        'static_dir': static_dir,
        'body_policy': policy,
        'blueprints': tuple(sorted(app.blueprints)),
    }
    return True


def create_application(
    import_name: str,
    *,
    static_dir: str,
    logger: Logger,
    secret_key: str,
    config: Mapping[str, Any] | None = None,
    body_policy: HttpBodyPolicy | None = None,
    max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH,
    static_timeout: StaticTimeout = 12,
    static_offload: StaticOffload | None = None,
    static_range_allows: RangeGate = if_range_allows,
    startup_handlers: Sequence[LifecycleRegistration] = (),
    shutdown_handlers: Sequence[LifecycleRegistration] = (),
):
    """Create and fully assemble an independent native Quart instance."""
    app = create_base_app(import_name, config)
    configure_application(
        app,
        static_dir=static_dir,
        logger=logger,
        secret_key=secret_key,
        body_policy=body_policy,
        max_content_length=max_content_length,
        static_timeout=static_timeout,
        static_offload=static_offload,
        static_range_allows=static_range_allows,
        startup_handlers=startup_handlers,
        shutdown_handlers=shutdown_handlers,
    )
    return app


__all__ = [
    'DEFAULT_MAX_CONTENT_LENGTH',
    'configure_application',
    'create_application',
]
