"""Generic /poll and /abort route factory for TaskRuntime-backed endpoints.

Eliminates duplicate poll/abort handler code across paper, translate,
trading_simulator, etc. Each module just calls register_task_routes()
on its blueprint and gets a uniform set of endpoints.

Example usage:

    from lib.task_runtime import TaskRuntime
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

from flask import jsonify, request

from lib.api_response import api_not_found, api_ok
from lib.log import get_logger

logger = get_logger(__name__)


def register_task_routes(bp, runtime, *, url_prefix: str,
                         enable_poll: bool = True,
                         enable_abort: bool = True,
                         poll_path: str = '/poll/<task_id>',
                         abort_path: str = '/abort/<task_id>'):
    """Attach standard /poll and /abort routes for a TaskRuntime.

    Args:
        bp: Flask/Quart Blueprint to attach routes to.
        runtime: TaskRuntime instance to back the routes.
        url_prefix: URL prefix (e.g. '/api/paper/report'). Routes are
            registered under this prefix.
        enable_poll: If True, register GET <prefix>/poll/<task_id>.
        enable_abort: If True, register POST <prefix>/abort/<task_id>.
        poll_path: Override the poll route shape (default '/poll/<task_id>').
        abort_path: Override the abort route shape (default '/abort/<task_id>').

    The generated routes use the runtime's `kind` as their endpoint name
    suffix to avoid conflicts when multiple runtimes share a blueprint.
    """
    kind = runtime.kind
    safe_kind = kind.replace('-', '_').replace(':', '_')

    if enable_poll:
        @bp.route(f'{url_prefix}{poll_path}', methods=['GET'],
                  endpoint=f'task_poll_{safe_kind}')
        def _poll(task_id):
            try:
                cursor = int(request.args.get('cursor', '0'))
                if cursor < 0:
                    cursor = 0
            except (TypeError, ValueError) as _e_audit:
                logger.debug('[_task_routes] _poll caught %s: %s', type(_e_audit).__name__, _e_audit)
                cursor = 0
            resp = runtime.poll(task_id, cursor=cursor)
            # runtime.poll() already returns the canonical response shape
            # (ok / events / next_cursor / status / done / error). Preserve
            # it verbatim — only the HTTP status varies.
            status_code = 404 if resp.get('error') == 'not_found' else 200
            return jsonify(resp), status_code

    if enable_abort:
        @bp.route(f'{url_prefix}{abort_path}', methods=['POST'],
                  endpoint=f'task_abort_{safe_kind}')
        def _abort(task_id):
            ok = runtime.abort(task_id)
            if not ok:
                task = runtime.get(task_id)
                if task is None:
                    return api_not_found()
                return api_ok(status=task['status'], note='already finished')
            return api_ok(status='aborting')

    logger.debug('[TaskRoutes] registered for kind=%s prefix=%s '
                 '(poll=%s abort=%s)',
                 kind, url_prefix, enable_poll, enable_abort)
