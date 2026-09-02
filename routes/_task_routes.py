"""Generic /poll and /abort route factory for TaskRuntime-backed endpoints.

Eliminates duplicate poll/abort handler code across paper, translate,
trading_simulator, etc. Each module just calls register_task_routes()
on its blueprint and gets a uniform set of endpoints.

Example usage:

    from lib.agent_core.task_runtime import TaskRuntime
    from routes._task_routes import register_task_routes

    paper_report_runtime = TaskRuntime('paper-report', ttl=3600,
                                        push_channel='paper')

    paper_bp = Blueprint('paper', __name__)

    register_task_routes(
        paper_bp, paper_report_runtime,
        url_prefix='/api/paper/report',
        # Optional: 'start' is module-specific so you typically write that
        #           by hand — only /poll and /abort are auto-generated.
    )

    # ↓ Auto-registered:
    #   GET  /api/paper/report/poll/<task_id>?cursor=N
    #   POST /api/paper/report/abort/<task_id>
"""

from quart import request

from lib.api_response import api_not_found, api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.task_runtime_ports import TaskRouteRuntimePort
from routes.task_http import (
    task_replay_cursor,
    task_replay_parameters,
    task_replay_response,
)
from routes.api_v1.auth import request_user_id

logger = get_logger(__name__)


def _apply_decorators(handler, decorators):
    for decorator in reversed(tuple(decorators or ())):
        handler = decorator(handler)
    return handler


def register_task_routes(bp, runtime: TaskRouteRuntimePort, *, url_prefix: str,
                         enable_poll: bool = True,
                         enable_abort: bool = True,
                         poll_path: str = '/poll/<task_id>',
                         abort_path: str = '/abort/<task_id>',
                         abort_handler=None,
                         poll_responses=None,
                         abort_responses=None,
                         poll_extensions=None,
                         abort_extensions=None,
                         route_decorators=(),
                         tags=('tasks',)):
    """Attach standard /poll and /abort routes for a TaskRuntime.

    Args:
        bp: Flask/Quart Blueprint to attach routes to.
        runtime: Structural task replay/abort runtime backing the routes.
        url_prefix: URL prefix (e.g. '/api/paper/report'). Routes are
            registered under this prefix.
        enable_poll: If True, register GET <prefix>/poll/<task_id>.
        enable_abort: If True, register POST <prefix>/abort/<task_id>.
        poll_path: Override the poll route shape (default '/poll/<task_id>').
        abort_path: Override the abort route shape (default '/abort/<task_id>').
        abort_handler: Optional application-specific response adapter. It
            receives ``task_id`` plus the authenticated ``owner_user_id`` and
            replaces the default bool-to-HTTP mapping while retaining the
            shared route registration. The handler owns its atomic owner check.
        poll_responses: Optional OpenAPI response map for the poll adapter.
        abort_responses: Optional OpenAPI response map for the abort adapter.
        poll_extensions: Optional OpenAPI vendor extensions for polling.
        abort_extensions: Optional OpenAPI vendor extensions for aborting.
        route_decorators: Authentication/scope decorators applied uniformly to
            every enabled generated route.
        tags: OpenAPI tags applied to generated poll/abort operations.

    The generated routes use the runtime's `kind` as their endpoint name
    suffix to avoid conflicts when multiple runtimes share a blueprint.
    """
    kind = runtime.kind
    safe_kind = kind.replace('-', '_').replace(':', '_')

    if enable_poll:
        def _poll(task_id):
            user_id = int(request_user_id())
            if runtime.get_owned(task_id, user_id=user_id) is None:
                return api_not_found()
            cursor = task_replay_cursor(request.args)
            resp = runtime.poll(task_id, cursor=cursor)
            # runtime.poll() returns the versioned replay page. Preserve it
            # verbatim; the replay protocol owns its HTTP mapping too.
            return task_replay_response(resp)

        poll_view = api_meta(
            summary=f'Poll {kind} task events',
            description='Returns one tofu.task-replay/v1 cursor page.',
            tags=list(tags),
            parameters=task_replay_parameters(),
            responses=poll_responses,
            extensions=poll_extensions,
        )(_poll)
        poll_view = _apply_decorators(poll_view, route_decorators)
        bp.add_url_rule(
            f'{url_prefix}{poll_path}',
            endpoint=f'task_poll_{safe_kind}',
            view_func=poll_view,
            methods=['GET'],
        )

    if enable_abort:
        def _abort(task_id):
            user_id = int(request_user_id())
            if abort_handler is not None:
                return abort_handler(task_id, user_id)
            task = runtime.get_owned(task_id, user_id=user_id)
            if task is None:
                return api_not_found()
            ok = runtime.abort_owned(task_id, user_id=user_id)
            if not ok:
                return api_ok(status=task['status'], note='already finished')
            return api_ok(status='aborting')

        abort_view = api_meta(
            summary=f'Abort {kind} task',
            tags=list(tags),
            request_body=False,
            responses=abort_responses,
            extensions=abort_extensions,
        )(_abort)
        abort_view = _apply_decorators(abort_view, route_decorators)
        bp.add_url_rule(
            f'{url_prefix}{abort_path}',
            endpoint=f'task_abort_{safe_kind}',
            view_func=abort_view,
            methods=['POST'],
        )

    logger.debug('[TaskRoutes] registered for kind=%s prefix=%s '
                 '(poll=%s abort=%s)',
                 kind, url_prefix, enable_poll, enable_abort)
