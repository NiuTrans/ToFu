"""Quart base-application construction, independent of route assembly."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quart import Quart

from lib.app_lifecycle import register_app_lifecycle


class _TofuQuart(Quart):
    """Quart shell with the Flask-3.1 option present before construction."""

    default_config = {
        **Quart.default_config,
        'PROVIDE_AUTOMATIC_OPTIONS': True,
    }


def create_base_app(
    import_name: str,
    config: Mapping[str, Any] | None = None,
) -> Quart:
    """Create the native ASGI shell used by the server assembly module.

    Static serving stays disabled because Tofu's explicit route moves FUSE
    filesystem work off the event loop. Domain blueprints and middleware are
    intentionally registered by the assembly layer after this function
    returns.
    """
    # Quart 0.19 paired with Flask 3.1 reads PROVIDE_AUTOMATIC_OPTIONS while
    # constructing its first static route, before instance config can be
    # updated. The project dependency floor is Quart 0.20, but keeping the
    # default on this private subclass makes stale self-hosted environments
    # fail safe without mutating Quart's process-global class.
    app = _TofuQuart(import_name, static_folder=None)
    if config:
        app.config.update(dict(config))
    register_app_lifecycle(app)

    @app.errorhandler(PermissionError)
    async def _permission_error(exc: PermissionError):
        # routes/common.py:_request_user_id fails CLOSED with PermissionError
        # when multi-user mode meets a user_id-less credential.  Without this
        # mapping the request surfaced as Quart's bare 500, drifting from the
        # 'permission' closed-kind envelope every other authz rejection uses.
        from quart import request

        from lib.api_v4 import is_api_v4_path, problem_response
        if is_api_v4_path(request.path):
            return problem_response(
                status=403,
                code='permission_denied',
                title='Permission denied',
                detail=str(exc) or 'This principal cannot access the resource.',
                instance=request.path,
            )
        from lib.api_response import api_typed_error
        return api_typed_error('permission', status=403, message=str(exc))

    return app


__all__ = ['create_base_app']
