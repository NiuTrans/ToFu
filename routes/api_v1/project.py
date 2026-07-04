"""routes/api_v1/project.py — Project Co-Pilot REST surface.

Routes (legacy snake_case path → new hyphen-case path):
  POST   /api/v1/project/set                — set primary project root
  PUT    /api/v1/project/paths              — atomically set primary + extras
  GET    /api/v1/project/status             — current project state
  DELETE /api/v1/project                    — clear active project
  POST   /api/v1/project/browse             — list a directory
  POST   /api/v1/project/mkdir              — create a new folder
  POST   /api/v1/project/rmdir              — delete a folder (→ trash bin)
  GET    /api/v1/project/recent             — list recent project paths
  POST   /api/v1/project/recent             — save a recent path
  DELETE /api/v1/project/recent             — clear recent list
  POST   /api/v1/project/write-approval     — resolve a pending write
  POST   /api/v1/project/undo               — per-round / per-conv undo
  POST   /api/v1/project/undo-all           — undo all pending mods in one project
  GET    /api/v1/project/gitignore/suggestions — pending suggestions
  POST   /api/v1/project/gitignore/accept   — append dirs to .gitignore
  POST   /api/v1/project/gitignore/dismiss  — drop suggestions
  POST   /api/v1/project/rescan             — refresh file index
  POST   /api/v1/project/redo               — re-apply previously-undone round
  POST   /api/v1/project/write              — direct file write (Apply Code)
  GET    /api/v1/project/feed               — Project Brain: activity feed (read)
  GET    /api/v1/project/charter            — Project Brain: charter (read)
  POST   /api/v1/project/charter/commit     — Project Brain: human-gated charter commit
  GET    /api/v1/project/charter/pending    — Project Brain: unresolved proposals
  POST   /api/v1/project/charter/dismiss    — Project Brain: reject a proposal
  GET    /api/v1/project/board              — Project Brain: coordination board (read)
  POST   /api/v1/project/board/post         — Project Brain: human posts an epic
  POST   /api/v1/project/board/complete     — Project Brain: human marks epic done
  POST   /api/v1/project/board/block        — Project Brain: human flags epic blocked
  POST   /api/v1/project/board/reopen       — Project Brain: human reopens an epic
  GET    /api/v1/project/brain/summary      — Project Brain: collab-bar summary
  GET    /api/v1/project/brain/peers        — Project Brain: LIVE peer/team roster
  GET    /api/v1/project/brain/influence    — Project Brain: per-conversation influence
  POST   /api/v1/project/brain/peer-message — Project Brain: human nudges a sibling conversation

All require ``@require_auth``. Mutations that change ``data/config/`` or
walk the filesystem keep the legacy ``rate_limit(10/60)`` to throttle
runaway client loops.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from lib.api_response import (
    api_bad_request, api_internal_error, api_not_found, api_ok,
)
from lib.log import get_logger
from lib.openapi import api_meta
from lib.rate_limiter import rate_limit
from lib.request_parser import parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_project_bp = Blueprint('api_v1_project', __name__)


def _active_project_path(explicit: str = '') -> str:
    """Resolve the project path: explicit body field overrides server state."""
    if explicit:
        return explicit
    from lib.project_mod.config import _state
    return _state.get('path', '') or ''


def _decoded_path_arg(name: str = 'path') -> str:
    """Read a project path query arg, defensively undoing proxy double-encoding.

    Thin wrapper over the shared ``lib.request_parser.decode_proxy_path_arg``
    seam — the single source of truth for the VS Code-proxy re-encode fix,
    used by every route that reads a filesystem path from the query string.
    """
    from lib.request_parser import decode_proxy_path_arg
    return decode_proxy_path_arg(name)


# ── State / lifecycle ────────────────────────────────────────────────

@api_v1_project_bp.route('/api/v1/project/set', methods=['POST'])
@require_auth
@rate_limit(limit=10, per=60)
@api_meta(
    summary='Set the primary project root',
    description='Replaces any existing primary path; clears extras.',
    tags=['project'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'required': ['path'],
                    'properties': {'path': {'type': 'string'}}}}}},
)
def project_set():
    data = parse_body()
    path = data.get('path', '').strip()
    if not path:
        return api_bad_request('No path provided', field='path')
    try:
        from lib.project_mod import set_project
        return api_ok({**set_project(path)})
    except Exception as e:
        logger.error('[Project.v1] set failed for path %s: %s', path, e,
                     exc_info=True)
        return api_bad_request(e)


@api_v1_project_bp.route('/api/v1/project/paths', methods=['PUT'])
@require_auth
@rate_limit(limit=10, per=60)
@api_meta(
    summary='Atomically set primary + extra project paths',
    description='Body: ``{paths: [primary, extra1, extra2, ...]}``.',
    tags=['project'],
)
def project_paths():
    data = parse_body()
    paths = data.get('paths', [])
    if not paths or not isinstance(paths, list):
        return jsonify({'error': 'Provide a "paths" array with at least '
                                  'one directory'}), 400
    readonly = data.get('readOnlyPaths') or []
    if not isinstance(readonly, list):
        readonly = []
    try:
        from lib.project_mod import set_project_paths
        return api_ok({**set_project_paths(paths, readonly_paths=readonly)})
    except Exception as e:
        logger.error('[Project.v1] paths failed for %s: %s', paths, e,
                     exc_info=True)
        return api_bad_request(e)


@api_v1_project_bp.route('/api/v1/project/status', methods=['GET'])
@require_auth
@api_meta(summary='Active project state', tags=['project'])
def project_status():
    from lib.project_mod import get_state
    return jsonify(get_state())


@api_v1_project_bp.route('/api/v1/project', methods=['DELETE'])
@require_auth
@api_meta(summary='Clear the active project', tags=['project'])
def project_clear():
    from lib.project_mod import clear_project
    clear_project()
    return api_ok()


@api_v1_project_bp.route('/api/v1/project/browse', methods=['POST'])
@require_auth
@api_meta(
    summary='List a directory',
    description='Body: ``{path?, showHidden?}``. Used by the file picker.',
    tags=['project'],
)
def project_browse():
    data = parse_body()
    path = data.get('path', '').strip() or None
    show_hidden = data.get('showHidden', False)
    from lib.project_mod import browse_directory
    result = browse_directory(path, show_hidden=show_hidden)
    if result.get('error'):
        return jsonify(result), 400
    return jsonify(result)


@api_v1_project_bp.route('/api/v1/project/mkdir', methods=['POST'])
@require_auth
@rate_limit(limit=20, per=60)
@api_meta(
    summary='Create a new folder',
    description=('Body: ``{parent, name}``. Creates ``name`` (a single '
                 'non-navigating segment) under the existing ``parent`` '
                 'directory. Used by the file picker\'s "New folder" action.'),
    tags=['project'],
)
def project_mkdir():
    data = parse_body()
    parent = (data.get('parent') or '').strip()
    name = (data.get('name') or '').strip()
    if not parent:
        return api_bad_request('parent is required', field='parent')
    if not name:
        return api_bad_request('name is required', field='name')
    from lib.project_mod import create_directory
    result = create_directory(parent, name)
    if result.get('error'):
        return jsonify(result), 400
    return jsonify(result)


@api_v1_project_bp.route('/api/v1/project/rmdir', methods=['POST'])
@require_auth
@rate_limit(limit=20, per=60)
@api_meta(
    summary='Delete a folder (moved to a recoverable trash bin)',
    description=('Body: ``{path}``. Moves the directory into a ``.tofu_trash`` '
                 'bin rather than an irreversible delete. Refuses system '
                 'paths and active workspace roots. Used by the file picker\'s '
                 '"Delete folder" action.'),
    tags=['project'],
)
def project_rmdir():
    data = parse_body()
    path = (data.get('path') or '').strip()
    if not path:
        return api_bad_request('path is required', field='path')
    from lib.project_mod import delete_directory
    result = delete_directory(path)
    if result.get('error'):
        return jsonify(result), 400
    return jsonify(result)


# ── Recent projects ──────────────────────────────────────────────────

@api_v1_project_bp.route('/api/v1/project/recent',
                          methods=['GET', 'POST', 'DELETE'])
@require_auth
@api_meta(
    summary='Recent project paths CRUD',
    description='GET → list, POST {path} → add, DELETE → wipe.',
    tags=['project'],
)
def project_recent():
    from lib.project_mod import (
        clear_recent_projects, get_recent_projects, save_recent_project,
    )
    if request.method == 'POST':
        data = parse_body()
        path = data.get('path', '').strip()
        if path:
            save_recent_project(path)
        return api_ok()
    if request.method == 'DELETE':
        clear_recent_projects()
        return api_ok()
    return jsonify({'projects': get_recent_projects()})


# ── Approval / undo / redo / rescan ─────────────────────────────────

@api_v1_project_bp.route('/api/v1/project/write-approval', methods=['POST'])
@require_auth
@api_meta(
    summary='Resolve a pending write-approval prompt',
    description='Body: ``{approvalId, approved}``.',
    tags=['project'],
)
def project_write_approval():
    data = parse_body()
    approval_id = data.get('approvalId', '')
    approved = data.get('approved', False)
    if not approval_id:
        return api_bad_request('No approvalId', field='approvalId')
    from lib.tasks_pkg import resolve_write_approval
    if not resolve_write_approval(approval_id, approved):
        return api_not_found('Approval not found or expired')
    return api_ok({'approved': approved})


@api_v1_project_bp.route('/api/v1/project/undo', methods=['POST'])
@require_auth
@api_meta(
    summary='Undo file modifications',
    description='Body: ``{taskId|convId, projectPath?}``. ``taskId`` undoes only that round; '
                '``convId`` undoes the entire conversation\'s changes.',
    tags=['project'],
)
def project_undo():
    data = parse_body()
    task_id = data.get('taskId', '').strip()
    conv_id = data.get('convId', '').strip()
    explicit_path = data.get('projectPath', '').strip()
    # ★ Concurrency-safe resolution: an explicit projectPath (sent by the
    #   frontend per-conversation) wins. Otherwise recover the project that
    #   actually recorded this task/conv — NEVER fall back to the globally-
    #   active project (_state['path']), which may point at a different
    #   project when conversations edit projects concurrently, causing undo
    #   to silently no-op (undone=0).
    if not explicit_path:
        from lib.project_mod import resolve_base_path
        explicit_path = resolve_base_path(task_id=task_id or None,
                                          conv_id=conv_id or None) or ''
    project_path = _active_project_path(explicit_path)
    if not project_path:
        return api_bad_request('No active project')

    try:
        if task_id:
            from lib.project_mod import undo_task_modifications
            result = undo_task_modifications(project_path, task_id)
            logger.info('[Project.v1] undo task=%s: undone=%s failed=%s',
                        task_id[:8], result.get('undone', 0),
                        result.get('failed', 0))
        elif conv_id:
            from lib.project_mod import undo_conv_modifications
            result = undo_conv_modifications(project_path, conv_id)
            logger.info('[Project.v1] undo conv=%s: undone=%s failed=%s',
                        conv_id[:8], result.get('undone', 0),
                        result.get('failed', 0))
        else:
            return api_bad_request('Provide taskId or convId')
        return jsonify(result)
    except Exception as e:
        logger.error('[Project.v1] undo failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.undo')


@api_v1_project_bp.route('/api/v1/project/undo-all', methods=['POST'])
@require_auth
@api_meta(
    summary='Undo every pending file modification in ONE project',
    description=(
        'Body: ``{projectPath?}``. Reverts all pending modifications recorded '
        'for a SINGLE project (across every conversation that edited it) — '
        'NOT a global wipe across all projects. The target project is the '
        'explicit ``projectPath`` (pinned per-conversation by the frontend) '
        'and falls back to the UI-active project only when omitted.'),
    tags=['project'])
def project_undo_all():
    data = parse_body()
    project_path = _active_project_path(data.get('projectPath', '').strip())
    if not project_path:
        return api_bad_request('No active project')
    try:
        from lib.project_mod import undo_all_modifications
        result = undo_all_modifications(project_path)
        logger.info('[Project.v1] undo_all: undone=%s failed=%s',
                    result.get('undone', 0), result.get('failed', 0))
        return jsonify(result)
    except Exception as e:
        logger.error('[Project.v1] undo_all failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.undo_all')


@api_v1_project_bp.route('/api/v1/project/redo', methods=['POST'])
@require_auth
@api_meta(
    summary='Re-apply a previously-undone round',
    description='Body: ``{taskId, projectPath?}``.',
    tags=['project'],
)
def project_redo():
    data = parse_body()
    task_id = (data.get('taskId') or '').strip()
    if not task_id:
        return api_bad_request('taskId is required', field='taskId')
    # ★ Concurrency-safe resolution, mirroring project_undo: an explicit
    #   projectPath (pinned per-conversation by the frontend) wins; otherwise
    #   recover the project that actually recorded this task from its snapshot
    #   — NEVER fall back to the globally-active project (_state['path']),
    #   which may point at a different project when conversations edit
    #   projects concurrently, sending redo to the wrong project.
    explicit_path = (data.get('projectPath') or '').strip()
    if not explicit_path:
        from lib.project_mod import resolve_base_path
        explicit_path = resolve_base_path(task_id=task_id) or ''
    project_path = _active_project_path(explicit_path)
    if not project_path:
        return api_bad_request('No active project')
    try:
        from lib.project_mod import redo_task_modifications
        result = redo_task_modifications(project_path, task_id)
        logger.info('[Project.v1] redo task=%s: redone=%s ok=%s',
                    task_id[:8], result.get('redone', 0), result.get('ok'))
        return jsonify(result)
    except Exception as e:
        logger.error('[Project.v1] redo failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.redo')


@api_v1_project_bp.route('/api/v1/project/rescan', methods=['POST'])
@require_auth
@api_meta(summary='Re-scan the active project (refresh file index + stats)',
          tags=['project'])
def project_rescan():
    try:
        from lib.project_mod import rescan
        return jsonify({'ok': True, **(rescan() or {})})
    except Exception as e:
        logger.error('[Project.v1] rescan failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.rescan')


# ── .gitignore suggestions ──────────────────────────────────────────

@api_v1_project_bp.route('/api/v1/project/gitignore/suggestions',
                          methods=['GET'])
@require_auth
@api_meta(
    summary='List pending .gitignore suggestions',
    description='Detected from grep timeouts on dirs with no source files.',
    tags=['project'],
)
def project_gitignore_suggestions():
    # GET route → the projectPath query arg can be proxy-double-encoded, same
    # as the project-brain reads. Decode-until-stable before resolving.
    project_path = _active_project_path(_decoded_path_arg('projectPath'))
    if not project_path:
        return api_bad_request('No active project')
    try:
        from lib.project_mod.gitignore_suggest import get_suggestions
        return api_ok({'projectPath': project_path,
                       'suggestions': get_suggestions(project_path)})
    except Exception as e:
        logger.error('[Project.v1] gitignore/suggestions failed: %s', e,
                     exc_info=True)
        return api_internal_error(
            e, source='api_v1.project.gitignore_suggestions')


@api_v1_project_bp.route('/api/v1/project/gitignore/accept', methods=['POST'])
@require_auth
@rate_limit(limit=10, per=60)
@api_meta(
    summary='Append directories to the project .gitignore',
    description=(
        'Only dirs currently in the suggestion registry are accepted '
        '(defense against arbitrary writes). Existing entries are skipped.'
    ),
    tags=['project'],
)
def project_gitignore_accept():
    data = parse_body()
    project_path = _active_project_path((data.get('projectPath') or '').strip())
    dirs = data.get('dirs') or []
    if not project_path:
        return api_bad_request('No active project')
    if not isinstance(dirs, list) or not dirs:
        return api_bad_request('dirs must be a non-empty list', field='dirs')
    try:
        from lib.project_mod.gitignore_suggest import accept_suggestions
        result = accept_suggestions(project_path, dirs)
        if 'error' in result:
            return jsonify(result), 400
        logger.info('[Project.v1] gitignore/accept %s: added=%s skipped=%s '
                    'unknown=%s', project_path, result.get('added'),
                    result.get('skipped_existing'), result.get('unknown'))
        return api_ok({**result})
    except Exception as e:
        logger.error('[Project.v1] gitignore/accept failed: %s', e,
                     exc_info=True)
        return api_internal_error(
            e, source='api_v1.project.gitignore_accept')


@api_v1_project_bp.route('/api/v1/project/gitignore/dismiss',
                          methods=['POST'])
@require_auth
@api_meta(
    summary='Drop dirs from the suggestion registry',
    description='No .gitignore write \u2014 just removes from the in-memory list.',
    tags=['project'],
)
def project_gitignore_dismiss():
    data = parse_body()
    project_path = _active_project_path((data.get('projectPath') or '').strip())
    dirs = data.get('dirs') or []
    if not project_path:
        return api_bad_request('No active project')
    if not isinstance(dirs, list) or not dirs:
        return api_bad_request('dirs must be a non-empty list', field='dirs')
    try:
        from lib.project_mod.gitignore_suggest import dismiss_suggestions
        return api_ok({'removed': dismiss_suggestions(project_path, dirs)})
    except Exception as e:
        logger.error('[Project.v1] gitignore/dismiss failed: %s', e,
                     exc_info=True)
        return api_internal_error(
            e, source='api_v1.project.gitignore_dismiss')



# ── Project Brain: Activity Feed (read-only) ─────────────────────────

@api_v1_project_bp.route('/api/v1/project/feed', methods=['GET'])
@require_auth
@api_meta(
    summary='Read the cross-conversation project activity feed',
    description=(
        'Read-only "project brain" pulse. Query: ``path`` (REQUIRED — the '
        'project root the caller already holds; this route NEVER consults the '
        'process-global active-project singleton, so concurrent conversations '
        'on different projects can never thrash each other), ``since`` '
        '(optional seq; returns events with seq > since for incremental '
        'fetch). Returns ``{events: [...newest-first...], maxSeq}``.'
    ),
    tags=['project'],
)
def project_feed():
    # CRITICAL: key STRICTLY on the explicit ``path`` query param. Do NOT fall
    # back to _active_project_path()/_state — that global is mutated by UI
    # actions and reading it here would reintroduce the read/write-badge
    # flip-flop thrash (see project-global-state-thrash-flipflop). The feed is
    # per-project data addressed by the path the frontend already has in hand.
    project_path = _decoded_path_arg()
    if not project_path:
        return api_bad_request('path is required', field='path')
    try:
        since = int(request.args.get('since') or 0)
    except (ValueError, TypeError):
        since = 0
    try:
        limit = int(request.args.get('limit') or 100)
    except (ValueError, TypeError):
        limit = 100
    try:
        from lib.conversations.project_feed import read_project_feed
        return api_ok(read_project_feed(project_path, since_seq=since,
                                        limit=limit))
    except Exception as e:
        logger.error('[Project.v1] feed read failed for %s: %s',
                     project_path, e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.feed')


# ── Project Brain: Charter + Board (read + human-commit) ─────────────

@api_v1_project_bp.route('/api/v1/project/charter', methods=['GET'])
@require_auth
@api_meta(
    summary='Read the project charter (north star + committed decisions)',
    description=(
        'Read-only. Query: ``path`` (REQUIRED — keyed strictly on the explicit '
        'path, never the global singleton). Returns ``{content, decisions, '
        'version, updated_by_conv, updated_at, exists}``.'),
    tags=['project'],
)
def project_charter():
    project_path = _decoded_path_arg()
    if not project_path:
        return api_bad_request('path is required', field='path')
    try:
        from lib.conversations.project_charter import read_charter
        return api_ok(read_charter(project_path))
    except Exception as e:
        logger.error('[Project.v1] charter read failed for %s: %s',
                     project_path, e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.charter')


@api_v1_project_bp.route('/api/v1/project/charter/commit', methods=['POST'])
@require_auth
@rate_limit(limit=20, per=60)
@api_meta(
    summary='HUMAN-GATED commit of a charter change',
    description=(
        'The human gate for the charter north star — an agent can only PROPOSE '
        '(project_charter_propose); only this route COMMITS. Body: ``{path, '
        'content?, add_decision?, expected_version?, updated_by_conv?}``. '
        'Optimistic-locked: a stale ``expected_version`` is rejected with '
        '``version_conflict``. On success emits a ``decided`` activity event.'),
    tags=['project'],
)
def project_charter_commit():
    data = parse_body()
    project_path = (data.get('path') or '').strip()
    if not project_path:
        return api_bad_request('path is required', field='path')
    content = data.get('content')
    add_decision = data.get('add_decision')
    if content is None and not add_decision:
        return api_bad_request('provide content and/or add_decision')
    expected_version = data.get('expected_version')
    if expected_version is not None:
        try:
            expected_version = int(expected_version)
        except (ValueError, TypeError):
            return api_bad_request('expected_version must be an int',
                                   field='expected_version')
    try:
        from lib.conversations.project_charter import commit_charter
        result = commit_charter(
            project_path, content=content, add_decision=add_decision,
            expected_version=expected_version,
            updated_by_conv=(data.get('updated_by_conv') or '').strip(),
            resolves_proposal=(data.get('resolves_proposal') or '').strip())
        if not result.get('ok'):
            # version_conflict is a real, recoverable client outcome → 409.
            if result.get('error') == 'version_conflict':
                return jsonify(result), 409
            return jsonify(result), 400
        return api_ok(result)
    except Exception as e:
        logger.error('[Project.v1] charter commit failed for %s: %s',
                     project_path, e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.charter_commit')


@api_v1_project_bp.route('/api/v1/project/board', methods=['GET'])
@require_auth
@api_meta(
    summary='Read the project coordination board',
    description=(
        'Read-only. Query: ``path`` (REQUIRED, keyed strictly on the explicit '
        'path). Returns ``{tasks: [...], open, claimed, done}`` with each '
        "task's EFFECTIVE status (an expired claim reads as open)."),
    tags=['project'],
)
def project_board():
    project_path = _decoded_path_arg()
    if not project_path:
        return api_bad_request('path is required', field='path')
    try:
        from lib.conversations.project_board import read_board
        return api_ok(read_board(project_path))
    except Exception as e:
        logger.error('[Project.v1] board read failed for %s: %s',
                     project_path, e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.board')



def _board_conv_id(data: dict) -> str:
    """Resolve the acting conversation for a human board mutation.

    A human is NOT a conversation, but the board is keyed on conversations
    (``created_by_conv`` is the dispatch target; feed events carry a conv_id).
    The frontend passes the DISPLAYED conversation's id explicitly as the
    human's proxy. We take it verbatim and NEVER invent one or fall back to a
    global — a missing conv is refused by the caller.
    """
    return (data.get('convId') or data.get('createdByConv') or '').strip()


@api_v1_project_bp.route('/api/v1/project/board/post', methods=['POST'])
@require_auth
@rate_limit(limit=20, per=60)
@api_meta(
    summary='HUMAN posts a new epic to the coordination board',
    description=(
        'Body: ``{path, title, convId, depends_on?}``. ``convId`` is the '
        'displayed conversation acting as the human\'s proxy and becomes the '
        'epic\'s ``created_by_conv`` (the dispatch target), so a human-posted '
        'epic is dispatchable exactly like an agent-posted one. Refused (400) '
        'when no conversation context is supplied — never invents one or falls '
        'back to the active-project global. Audit-logged.'),
    tags=['project'],
)
def project_board_post():
    data = parse_body()
    project_path = (data.get('path') or '').strip()
    if not project_path:
        return api_bad_request('path is required', field='path')
    title = (data.get('title') or '').strip()
    if not title:
        return api_bad_request('title is required', field='title')
    conv_id = _board_conv_id(data)
    if not conv_id:
        return api_bad_request('convId is required (the acting conversation)',
                               field='convId')
    depends_on = data.get('depends_on') or []
    if not isinstance(depends_on, list):
        depends_on = []
    try:
        from lib.conversations.project_board import post_task
        result = post_task(project_path, conv_id, title, depends_on=depends_on)
        if not result.get('ok'):
            return jsonify(result), 400
        logger.info('[Project.v1] board/post proj=%.40r conv=%s id=%s',
                    project_path, conv_id[:8], result.get('id'))
        return api_ok(result)
    except Exception as e:
        logger.error('[Project.v1] board/post failed for %s: %s',
                     project_path, e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.board_post')


@api_v1_project_bp.route('/api/v1/project/board/complete', methods=['POST'])
@require_auth
@rate_limit(limit=20, per=60)
@api_meta(
    summary='HUMAN marks a board epic done',
    description=(
        'Body: ``{path, taskId, convId}``. Reuses the same engine path as the '
        'agent tool (so completing may trigger dispatch of unblocked '
        'dependents). Audit-logged.'),
    tags=['project'],
)
def project_board_complete():
    data = parse_body()
    project_path = (data.get('path') or '').strip()
    if not project_path:
        return api_bad_request('path is required', field='path')
    task_id = (data.get('taskId') or '').strip()
    if not task_id:
        return api_bad_request('taskId is required', field='taskId')
    conv_id = _board_conv_id(data)
    try:
        from lib.conversations.project_board import complete_task
        result = complete_task(project_path, conv_id, task_id)
        if not result.get('ok'):
            return jsonify(result), 400
        logger.info('[Project.v1] board/complete proj=%.40r task=%s',
                    project_path, task_id)
        return api_ok(result)
    except Exception as e:
        logger.error('[Project.v1] board/complete failed for %s: %s',
                     project_path, e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.board_complete')


@api_v1_project_bp.route('/api/v1/project/board/block', methods=['POST'])
@require_auth
@rate_limit(limit=20, per=60)
@api_meta(
    summary='HUMAN flags a board epic as blocked',
    description=(
        'Body: ``{path, taskId, convId, reason?}``. Emits a ``blocked`` feed '
        'event (a signal, not a status change). Audit-logged.'),
    tags=['project'],
)
def project_board_block():
    data = parse_body()
    project_path = (data.get('path') or '').strip()
    if not project_path:
        return api_bad_request('path is required', field='path')
    task_id = (data.get('taskId') or '').strip()
    if not task_id:
        return api_bad_request('taskId is required', field='taskId')
    conv_id = _board_conv_id(data)
    reason = (data.get('reason') or '').strip()
    try:
        from lib.conversations.project_board import block_task
        result = block_task(project_path, conv_id, task_id, reason)
        if not result.get('ok'):
            return jsonify(result), 400
        logger.info('[Project.v1] board/block proj=%.40r task=%s',
                    project_path, task_id)
        return api_ok(result)
    except Exception as e:
        logger.error('[Project.v1] board/block failed for %s: %s',
                     project_path, e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.board_block')


@api_v1_project_bp.route('/api/v1/project/board/reopen', methods=['POST'])
@require_auth
@rate_limit(limit=20, per=60)
@api_meta(
    summary='HUMAN reopens a board epic (done|claimed → open)',
    description=(
        'Body: ``{path, taskId, convId}``. A direct status write that clears '
        'the owner + lease — NOT a lease mutation and NO background reaper. '
        'Permitted from ``done`` (revive) and ``claimed`` (break a stuck live '
        'claim). Emits a ``note`` feed event so the transition is observable; '
        'the previous owner sees the epic flip from "(you)" to open on its '
        'NEXT prompt assembly (not interrupted mid-turn). Audit-logged.'),
    tags=['project'],
)
def project_board_reopen():
    data = parse_body()
    project_path = (data.get('path') or '').strip()
    if not project_path:
        return api_bad_request('path is required', field='path')
    task_id = (data.get('taskId') or '').strip()
    if not task_id:
        return api_bad_request('taskId is required', field='taskId')
    conv_id = _board_conv_id(data)
    try:
        from lib.conversations.project_board import reopen_task
        result = reopen_task(project_path, conv_id, task_id)
        if not result.get('ok'):
            return jsonify(result), 400
        logger.info('[Project.v1] board/reopen proj=%.40r task=%s from=%s',
                    project_path, task_id, result.get('from'))
        return api_ok(result)
    except Exception as e:
        logger.error('[Project.v1] board/reopen failed for %s: %s',
                     project_path, e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.board_reopen')


@api_v1_project_bp.route('/api/v1/project/charter/pending', methods=['GET'])
@require_auth
@api_meta(
    summary='List UNRESOLVED charter proposals (awaiting the human)',
    description=(
        'Read-only. Query: ``path`` (REQUIRED). Returns ``{pending: [...]}`` — '
        'proposed_decision events NOT yet resolved by a matching commit or '
        'dismiss (by proposalId). This is the single source both the collab '
        'bar count and the Charter panel read, so the count decrements the '
        'moment a human commits/rejects.'),
    tags=['project'],
)
def project_charter_pending():
    project_path = _decoded_path_arg()
    if not project_path:
        return api_bad_request('path is required', field='path')
    try:
        from lib.conversations.project_charter import pending_proposals
        return api_ok({'pending': pending_proposals(project_path)})
    except Exception as e:
        logger.error('[Project.v1] charter pending failed for %s: %s',
                     project_path, e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.charter_pending')


@api_v1_project_bp.route('/api/v1/project/charter/dismiss', methods=['POST'])
@require_auth
@rate_limit(limit=20, per=60)
@api_meta(
    summary='Durably DISMISS (reject) a pending charter proposal',
    description=(
        'The human-reject gate. Body: ``{path, proposalId, summary?}``. Emits '
        'a ``dismissed`` activity event carrying the resolved ``proposalId`` '
        'so the proposal drops out of the pending set permanently (not a local '
        'DOM dismiss that evaporates on reload).'),
    tags=['project'],
)
def project_charter_dismiss():
    data = parse_body()
    project_path = (data.get('path') or '').strip()
    if not project_path:
        return api_bad_request('path is required', field='path')
    proposal_id = (data.get('proposalId') or '').strip()
    if not proposal_id:
        return api_bad_request('proposalId is required', field='proposalId')
    try:
        from lib.conversations.project_charter import dismiss_proposal
        result = dismiss_proposal(
            project_path, (data.get('updated_by_conv') or '').strip(),
            proposal_id, summary=(data.get('summary') or '').strip())
        if not result.get('ok'):
            return jsonify(result), 400
        return api_ok(result)
    except Exception as e:
        logger.error('[Project.v1] charter dismiss failed for %s: %s',
                     project_path, e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.charter_dismiss')


@api_v1_project_bp.route('/api/v1/project/brain/summary', methods=['GET'])
@require_auth
@api_meta(
    summary='One-shot Project Brain summary for the collaboration bar',
    description=(
        'Read-only, cheap aggregation across Board + Activity Feed + presence, '
        'keyed strictly on the explicit ``path``. Returns ``{epicsOpen, '
        'epicsClaimed, epicsDone, pendingDecisions, activePeers, peerEpics, '
        'charterExists}``, where ``peerEpics`` maps an active peer conv_id → '
        'the title of the epic it is currently advancing (a live claim).'),
    tags=['project'],
)
def project_brain_summary():
    project_path = _decoded_path_arg()
    if not project_path:
        return api_bad_request('path is required', field='path')
    try:
        from lib.conversations.project_brain_summary import build_brain_summary
        return api_ok(build_brain_summary(project_path))
    except Exception as e:
        logger.error('[Project.v1] brain summary failed for %s: %s',
                     project_path, e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.brain_summary')


@api_v1_project_bp.route('/api/v1/project/brain/peers', methods=['GET'])
@require_auth
@api_meta(
    summary='LIVE peer status — the conversations-as-a-team roster',
    description=(
        'Read-only, LIVE cross-conversation roster: joins presence (who is '
        'active + phase/file) with the live task registry (current round / '
        'status) and the board claim map (which epic each peer is advancing). '
        'Reuses the SAME ``claims_by_conv`` join the collab bar uses, so the '
        'Team column can never drift from the summary. Query: ``path`` '
        '(REQUIRED) + ``convId`` (optional — excluded from the roster so a '
        'conversation never lists itself as a peer). Returns '
        '``{peers: [...], count}``; each peer carries ``{convId, agentId, '
        'title, phase, statusLabel, currentFile, round, taskStatus, '
        'claimedEpic}``. This is LIVE state, NOT conversation history.'),
    tags=['project'],
)
def project_brain_peers():
    project_path = _decoded_path_arg()
    if not project_path:
        return api_bad_request('path is required', field='path')
    # convId is OPTIONAL here (unlike influence): a project-wide roster is
    # meaningful with no active conv. When present it's excluded from the list.
    conv_id = _decoded_path_arg('convId')
    try:
        from lib.conversations.project_peer import build_peer_status
        return api_ok(build_peer_status(project_path, conv_id or ''))
    except Exception as e:
        logger.error('[Project.v1] brain peers failed for %s: %s',
                     project_path, e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.brain_peers')


@api_v1_project_bp.route('/api/v1/project/brain/influence', methods=['GET'])
@require_auth
@api_meta(
    summary='How the project brain influences ONE conversation',
    description=(
        'Read-only, per-conversation view of the brain\'s effect on a single '
        'chat: the charter it is bound by, the board epics it OWNS (a live '
        'claim), the epics it must AVOID (a sibling holds an unexpired lease), '
        'the open epics it could pick up, and the decisions awaiting a human. '
        'Query: ``path`` (REQUIRED) + ``convId`` (REQUIRED). The two '
        '``injected`` flags mirror the actual system-context injection gate '
        'exactly (computed from the SAME render_charter_block / '
        'render_board_block the prompt uses), so the panel can never drift '
        'from what the model really sees.'),
    tags=['project'],
)
def project_brain_influence():
    project_path = _decoded_path_arg()
    if not project_path:
        return api_bad_request('path is required', field='path')
    conv_id = _decoded_path_arg('convId')
    if not conv_id:
        return api_bad_request('convId is required', field='convId')
    try:
        from lib.conversations.project_brain_influence import (
            build_conv_influence,
        )
        return api_ok(build_conv_influence(project_path, conv_id))
    except Exception as e:
        logger.error('[Project.v1] brain influence failed for %s conv=%s: %s',
                     project_path, (conv_id or '')[:8], e, exc_info=True)
@api_v1_project_bp.route('/api/v1/project/brain/peer-message', methods=['POST'])
@require_auth
@rate_limit(limit=20, per=60)
@api_meta(
    summary='HUMAN sends an advisory nudge to a sibling conversation',
    description=(
        'Body: ``{path, convId, toConvId, text}``. The operator, acting via '
        'the DISPLAYED conversation ``convId`` (their proxy — the board/feed '
        'are conversation-keyed), sends the advisory note ``text`` to sibling '
        'conversation ``toConvId``. It is enqueued as a ``KIND_PEER_MSG`` turn '
        '— seen on the target\'s NEXT turn, NEVER interrupting a live turn — '
        'framed as operator guidance and stamped ``_peerHuman`` so the target '
        'attributes it to the operator. Reuses ``send_peer_message`` (the '
        'single seam): the SAME per-(sender,target) rate limit + self-send '
        'refusal apply — the human path is not a storm bypass. Returns '
        '``{ok, queueId}`` or a 400 with ``error`` (``rate_limited`` carries '
        '``retryAfter``). Refused when no ``convId`` (never invents one).'),
    tags=['project'],
)
def project_brain_peer_message():
    data = parse_body()
    project_path = (data.get('path') or '').strip()
    if not project_path:
        return api_bad_request('path is required', field='path')
    from_conv = _board_conv_id(data)
    if not from_conv:
        return api_bad_request('convId is required (the acting conversation)',
                               field='convId')
    to_conv = (data.get('toConvId') or data.get('to_conv_id') or '').strip()
    if not to_conv:
        return api_bad_request('toConvId is required', field='toConvId')
    text = (data.get('text') or '').strip()
    if not text:
        return api_bad_request('text is required', field='text')
    try:
        from lib.conversations.project_peer import send_peer_message
        res = send_peer_message(project_path, from_conv, to_conv, text,
                                human=True)
        if not res.get('ok'):
            logger.info('[Project.v1] peer-message refused %s→%s: %s',
                        from_conv[:8], to_conv[:8], res.get('error'))
            return jsonify(res), 400
        logger.info('[Project.v1] operator peer-message %s→%s (%d chars)',
                    from_conv[:8], to_conv[:8], len(text))
        return api_ok(res)
    except Exception as e:
        logger.error('[Project.v1] peer-message failed for %s: %s',
                     project_path, e, exc_info=True)
        return api_internal_error(e, source='api_v1.project.brain_peer_message')


        return api_internal_error(e, source='api_v1.project.brain_influence')


# ── Direct file write (Apply Code button) ────────────────────────────

@api_v1_project_bp.route('/api/v1/project/write', methods=['POST'])
@require_auth
@rate_limit(limit=20, per=60)
@api_meta(
    summary='Write content to a project file',
    description=(
        'Writes ``content`` to the project-relative ``path``, creating '
        'parent directories as needed. Used by the "Apply Code" button '
        'in the code-block hover overlay. Returns '
        '``{ok, path, created, lines}``.'
    ),
    tags=['project'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'required': ['path', 'content'],
                    'properties': {
                        'path': {'type': 'string'},
                        'content': {'type': 'string'}}}}}},
)
def project_write():
    data = parse_body()
    path = (data.get('path') or '').strip()
    content = data.get('content', '')
    if not path:
        return api_bad_request('path is required', field='path')

    project_path = _active_project_path()
    if not project_path:
        return api_bad_request('No active project')

    from lib.project_mod.write_tools import tool_write_file
    result = tool_write_file(project_path, path, content)
    if not result.get('ok'):
        logger.warning('[Project.v1] write failed for %s: %s',
                       path, result.get('error'))
        return jsonify(result), 400
    return jsonify(result)

__all__ = ['api_v1_project_bp']
