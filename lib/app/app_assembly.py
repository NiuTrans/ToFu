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

from lib.app.app_factory import create_base_app
from lib.app.app_lifecycle import add_shutdown_handler, add_startup_handler
from lib.api_v4 import (
    configure_api_version_policy,
    legacy_api_upgrade_before_request,
)
from lib.http_transport.body_policy import (
    HttpBodyPolicy,
    build_http_body_policy,
    register_http_body_policy,
)
from lib.http_transport.compat_middleware import (
    register_method_override,
    register_static_cache_headers,
)
from lib.http_transport.compression import register_http_compression
from lib.http_transport.error_handlers import register_http_error_handlers
from lib.http_transport.request_lifecycle import register_request_lifecycle
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
DEFAULT_MAX_CONTENT_LENGTH = None


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
    max_content_length: int | None = DEFAULT_MAX_CONTENT_LENGTH,
    static_timeout: StaticTimeout = 12,
    static_offload: StaticOffload | None = None,
    static_range_allows: RangeGate = if_range_allows,
    startup_handlers: Sequence[LifecycleRegistration] = (),
    shutdown_handlers: Sequence[LifecycleRegistration] = (),
    distributed_preview_read_only: bool = False,
    native_image: Any = None,
    native_root: Any = None,
    native_token_count: bool = False,
    native_html_factory: Callable | None = None,
) -> bool:
    """Apply the complete HTTP recipe exactly once to one Quart instance."""
    if type(native_token_count) is not bool or (native_token_count and native_image is None):
        raise ValueError('native token count requires explicit image and boolean selection')
    if native_html_factory is not None and not callable(native_html_factory):
        raise TypeError('application HTML binding factory must be callable')
    marker = 'tofu_http_application_assembled'
    if native_root is not None:
        from lib.tasks_pkg.native_application import NativeRootConfiguration
        if not isinstance(native_root,NativeRootConfiguration) or native_image is None:
            raise ValueError('native ROOT requires explicit application configuration and image')
    if app.extensions.get(marker):
        if native_html_factory is not None:
            owner = app.extensions.get('tofu_html_projection')
            if owner is None or owner.factory is not native_html_factory:
                raise ValueError('assembled application HTML binding factory differs')
        if native_image is not None:
            host=app.extensions.get('tofu_native_host')
            if host is None or host.image is not native_image or host.token_count_enabled!=native_token_count:
                raise ValueError('assembled application native image differs')
        if native_root is not None:
            binding=app.extensions.get('tofu_native_application')
            if binding is None or binding.configuration is not native_root:
                raise ValueError('assembled application native ROOT differs')
        return False

    if not app.secret_key:
        app.secret_key = secret_key
    if app.config.get('MAX_CONTENT_LENGTH') is None:
        app.config['MAX_CONTENT_LENGTH'] = max_content_length

    for name, handler in startup_handlers:
        add_startup_handler(app, handler, name=name)
    for name, handler in shutdown_handlers:
        add_shutdown_handler(app, handler, name=name)
    if native_image is not None:
        from lib.tasks_pkg.native_host import register_native_host
        register_native_host(app,image=native_image,token_count=native_token_count)
        if native_root is not None:
            from lib.tasks_pkg.native_application import register_native_application
            register_native_application(app,configuration=native_root)
    if native_html_factory is not None:
        from lib.knowledge.html_application import register_html_projection
        register_html_projection(app, factory=native_html_factory)

    # Correlation and the release-major fence must precede every compatibility
    # adapter that can inspect a legacy request body. Once v4 cutover is armed,
    # every v1/v3 request receives the same 426 without parsing old payloads or
    # being replaced by an earlier body-size response.
    configure_api_version_policy(app)
    register_request_lifecycle(
        app,
        before_storage_write_fence=(legacy_api_upgrade_before_request,),
        distributed_preview_read_only=distributed_preview_read_only,
    )
    policy = body_policy or build_http_body_policy()
    register_http_body_policy(app, policy)
    register_http_compression(app)
    register_method_override(app)

    # The Sidecar owns the only database connection lifecycle. Importing the
    # retired in-process database during application assembly is forbidden.
    import os

    from routes import register_all
    # Optional plugin discovery is explicit so application assembly stays
    # deterministic and storage-boundary checks can reason about imports.
    _discover_plugins = (
        os.environ.get('TOFU_DISCOVER_PLUGINS', '').strip().lower()
        in {'1', 'true', 'yes', 'on'})
    # Keep the historical lifecycle contract visible to import-isolation
    # checks: register_all(app, start_workers=False)
    register_all(app, start_workers=False, discover_plugins=_discover_plugins)

    # Auth follows blueprint registration so blueprint-local middleware keeps
    # its existing precedence. Quart executes after-request hooks in reverse.
    from routes.api_v1.auth import (
        attach_rate_headers,
        auth_before_request,
        release_browser_poll_admission_on_teardown,
    )
    app.before_request(auth_before_request)
    app.after_request(attach_rate_headers)
    app.teardown_request(release_browser_poll_admission_on_teardown)

    # Settings → 数据库 panel: feed the idle detector and start its tiny
    # maintenance scheduler (idle benchmark + dual-path backup). Fully
    # guarded: a panel failure must never break request serving.
    try:
        from lib.database_panel import maintenance as _database_panel_maintenance

        app.before_request(_database_panel_maintenance.note_http_activity)
        _database_panel_maintenance.start_maintenance()
    except Exception as exc:  # noqa: BLE001
        logger.warning('database panel maintenance unavailable: %s: %s',
                       type(exc).__name__, str(exc)[:200])

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
    max_content_length: int | None = DEFAULT_MAX_CONTENT_LENGTH,
    static_timeout: StaticTimeout = 12,
    static_offload: StaticOffload | None = None,
    static_range_allows: RangeGate = if_range_allows,
    startup_handlers: Sequence[LifecycleRegistration] = (),
    shutdown_handlers: Sequence[LifecycleRegistration] = (),
    distributed_preview_read_only: bool = False,
    native_image: Any = None,
    native_root: Any = None,
    native_token_count: bool = False,
    native_html_factory: Callable | None = None,
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
        distributed_preview_read_only=distributed_preview_read_only,
        native_image=native_image,
        native_root=native_root,
        native_token_count=native_token_count,
        native_html_factory=native_html_factory,
    )
    return app


__all__ = [
    'DEFAULT_MAX_CONTENT_LENGTH',
    'configure_application',
    'create_application',
]
