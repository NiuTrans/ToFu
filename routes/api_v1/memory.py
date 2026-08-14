"""Memory CRUD, structured My Context, and legacy profile compatibility.

MEMORY ONLY. Skill packages (a different noun — user-installed capability
packs) are served by ``routes/api_v1/skills.py`` under ``/api/v1/skills``.

Routes:
  GET    /api/v1/memory                       — list memories (no packages)
  POST   /api/v1/memory                       — create
  GET    /api/v1/memory/<id>                  — fetch one
  PUT    /api/v1/memory/<id>                  — update
  DELETE /api/v1/memory/<id>                  — delete
  POST   /api/v1/memory/merge                 — merge multiple
  POST   /api/v1/memory/<id>/toggle           — enable/disable
  GET    /api/v1/profile                      — personal-preference profile
  PUT    /api/v1/profile                      — hand-edit the profile
  POST   /api/v1/profile/pending/<id>         — confirm/dismiss a proposal

All routes require authentication; mutations don't need ``admin`` scope
because memories are user-owned and the cookie-auth UI uses them
intensively (settings panel + profile editor).
"""

from __future__ import annotations

import os

from quart import Blueprint, request

from lib.quart_sync import request_json

from lib.api_response import (
    api_bad_request, api_conflict, api_created, api_error, api_forbidden,
    api_not_found, api_ok,
)
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_memory_bp = Blueprint('api_v1_memory', __name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


def _project_path() -> str:
    """Resolve the project_path from the request, falling back to root.

    The query-string branch is decoded-until-stable via the shared
    ``decode_proxy_path_arg`` seam: a reverse proxy (VS Code ``/proxy/<port>/``)
    can double-encode an already-encoded path, so a GET ``?project_path=…``
    would otherwise arrive as a literal ``%2F…`` string and mis-scope the
    memory list. The JSON-body branch is unaffected (bodies aren't URL-encoded)
    so it takes precedence unchanged.
    """
    explicit = None
    if request.is_json:
        # Parse the body ONCE (each get_json is a cross-thread hop to the loop
        # under the sync shim); the JSON branch still takes precedence.
        explicit = (request_json(silent=True) or {}).get('project_path')
    if not explicit:
        from lib.request_parser import decode_proxy_path_arg
        explicit = decode_proxy_path_arg('project_path')
    return explicit or _PROJECT_ROOT


# ── Local Memory CRUD ────────────────────────────────────────────────

@api_v1_memory_bp.route('/api/v1/memory', methods=['GET'])
@require_auth
@api_meta(
    summary='List memories',
    description=(
        'Returns ``{memories: [...]}`` — flat memories ONLY (installed '
        'skill packages are a different noun, served by '
        '``/api/v1/skills``). Use ``?scope=all|project|global`` to filter.'
    ),
    tags=['memory'],
)
def list_memories_v1():
    from lib.memory import list_memories
    scope = request.args.get('scope', 'all')
    memories = [m for m in list_memories(project_path=_project_path(),
                                         scope=scope)
                if not m.get('is_package')]
    for m in memories:
        m.pop('filepath', None)
    return api_ok({'memories': memories})


@api_v1_memory_bp.route('/api/v1/memory/<memory_id>', methods=['GET'])
@require_auth
@api_meta(summary='Get one memory', tags=['memory'])
def get_memory_v1(memory_id):
    from lib.memory import get_memory
    mem = get_memory(memory_id, project_path=_project_path())
    if not mem:
        return api_not_found('Memory not found')
    mem.pop('filepath', None)
    return api_ok(mem)


@api_v1_memory_bp.route('/api/v1/memory', methods=['POST'])
@require_auth
@api_meta(
    summary='Create a memory',
    tags=['memory'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'required': ['name'], 'properties': {
            'name': {'type': 'string'},
            'description': {'type': 'string'},
            'body': {'type': 'string'},
            'tags': {'type': 'array', 'items': {'type': 'string'}},
            'scope': {'type': 'string', 'enum': ['global', 'project']}}}}}},
)
def create_memory_v1():
    from lib.memory import create_memory
    data = parse_body(force=True)
    name = data.get('name', 'Untitled')
    logger.info('[Memory.v1] creating %r (scope=%s)', name,
                data.get('scope', 'global'))
    mem = create_memory(
        name=name,
        description=data.get('description', ''),
        body=data.get('body', ''),
        tags=data.get('tags'),
        scope=data.get('scope', 'global'),
        project_path=_project_path(),
    )
    logger.info('[Memory.v1] created %s', mem.get('id', '?'))
    mem.pop('filepath', None)
    return api_created(mem)


@api_v1_memory_bp.route('/api/v1/memory/<memory_id>', methods=['PUT'])
@require_auth
@api_meta(summary='Update a memory', tags=['memory'])
def update_memory_v1(memory_id):
    from lib.memory import update_memory
    data = parse_body(force=True)
    try:
        mem = update_memory(memory_id, data, project_path=_project_path())
    except ValueError as e:
        # Package guard: skill packages are managed via /api/v1/skills.
        return api_bad_request(e)
    if not mem:
        return api_not_found('Memory not found')
    mem.pop('filepath', None)
    return api_ok(mem)


@api_v1_memory_bp.route('/api/v1/memory/<memory_id>', methods=['DELETE'])
@require_auth
@api_meta(summary='Delete a memory', tags=['memory'])
def delete_memory_v1(memory_id):
    from lib.memory import delete_memory
    logger.warning('[Memory.v1] deleting %s', memory_id)
    try:
        ok = delete_memory(memory_id, project_path=_project_path())
    except ValueError as e:
        # Package guard: uninstall skill packages via /api/v1/skills.
        return api_bad_request(e)
    if not ok:
        logger.warning('[Memory.v1] %s not found for deletion', memory_id)
        return api_not_found('Memory not found', deleted=False)
    return api_ok(deleted=True)


@api_v1_memory_bp.route('/api/v1/memory/merge', methods=['POST'])
@require_auth
@api_meta(
    summary='Merge multiple memories',
    description='Body: ``{memory_ids, name, description, body, tags?, scope?}``',
    tags=['memory'],
)
def merge_memories_v1():
    from lib.memory import merge_memories
    data = parse_body(force=True)
    logger.info('[Memory.v1] merging %s → %s',
                data.get('memory_ids', []), data.get('name', '?'))
    try:
        result = merge_memories(
            memory_ids=data.get('memory_ids', []),
            name=data.get('name', 'Merged Memory'),
            description=data.get('description', ''),
            body=data.get('body', ''),
            tags=data.get('tags'),
            scope=data.get('scope', 'project'),
            project_path=_project_path(),
        )
    except ValueError as e:
        logger.debug('[Memory.v1] merge validation error: %s', e)
        return api_bad_request(e)
    result['merged_memory'].pop('filepath', None)
    return api_created(result)


@api_v1_memory_bp.route('/api/v1/memory/<memory_id>/toggle',
                         methods=['POST'])
@require_auth
@api_meta(summary='Enable / disable a memory', tags=['memory'])
def toggle_memory_v1(memory_id):
    from lib.memory import toggle_memory
    data = parse_body()
    mem = toggle_memory(memory_id, enabled=data.get('enabled'),
                         project_path=_project_path())
    if not mem:
        return api_not_found('Memory not found')
    mem.pop('filepath', None)
    return api_ok(mem)


@api_v1_memory_bp.route('/api/v1/memory/actions/clear', methods=['GET'])
@require_auth
@api_meta(summary='Preview clearing visible experience memories', tags=['memory'])
def preview_clear_memories_v1():
    from lib.auth_mode import is_multi_user
    if is_multi_user():
        return api_forbidden('Bulk memory clearing is disabled in multi-user mode')
    from lib.memory import clear_memories
    return api_ok(clear_memories(project_path=_project_path(), dry_run=True))


@api_v1_memory_bp.route('/api/v1/memory/actions/clear', methods=['POST'])
@require_auth
@api_meta(summary='Clear visible experience memories', tags=['memory'])
def clear_memories_v1():
    from lib.auth_mode import is_multi_user
    if is_multi_user():
        return api_forbidden('Bulk memory clearing is disabled in multi-user mode')
    data = parse_body()
    if data.get('confirm') is not True:
        return api_bad_request('confirm=true is required')
    from lib.memory import clear_memories
    result = clear_memories(project_path=_project_path(), dry_run=False)
    logger.warning('[Memory.v1] cleared %d visible memories (%d failed)',
                   len(result['deleted_ids']), len(result['failed_ids']))
    return api_ok(result)


# ── Structured "My Context" CRUD ───────────────────────────────────

def _context_scope() -> str:
    from lib.memory import user_profile as up
    from .auth import current_auth
    return up.resolve_profile_scope(current_auth())


@api_v1_memory_bp.route('/api/v1/context', methods=['GET'])
@require_auth
@api_meta(summary='Get durable user context', tags=['memory'])
def get_user_context_v1():
    from lib.memory import user_profile as up
    return api_ok(up.context_status(_context_scope()))


@api_v1_memory_bp.route('/api/v1/context', methods=['PUT'])
@require_auth
@api_meta(summary='Replace durable user context', tags=['memory'])
def put_user_context_v1():
    from lib.memory import user_profile as up
    data = parse_body()
    try:
        result = up.save_context_items(data.get('items'), _context_scope())
    except up.ContextValidationError as exc:
        return api_bad_request(str(exc), cap=up.CONTEXT_CHAR_CAP)
    return api_ok(result)


@api_v1_memory_bp.route('/api/v1/context', methods=['POST'])
@require_auth
@api_meta(summary='Create one durable user-context item', tags=['memory'])
def create_user_context_v1():
    from lib.memory import user_profile as up
    data = parse_body()
    try:
        result = up.create_context_item(data, _context_scope(), source='manual')
    except up.ContextValidationError as exc:
        return api_bad_request(str(exc), cap=up.CONTEXT_CHAR_CAP)
    return api_created(result)


@api_v1_memory_bp.route('/api/v1/context/<item_id>', methods=['PUT'])
@require_auth
@api_meta(summary='Update one durable user-context item', tags=['memory'])
def update_user_context_v1(item_id):
    from lib.memory import user_profile as up
    try:
        result = up.update_context_item(item_id, parse_body(), _context_scope())
    except up.ContextValidationError as exc:
        return api_bad_request(str(exc), cap=up.CONTEXT_CHAR_CAP)
    if result is None:
        return api_not_found('Context item not found')
    return api_ok(result)


@api_v1_memory_bp.route('/api/v1/context/<item_id>', methods=['DELETE'])
@require_auth
@api_meta(summary='Delete one durable user-context item', tags=['memory'])
def delete_user_context_v1(item_id):
    from lib.memory import user_profile as up
    if not up.delete_context_item(item_id, _context_scope()):
        return api_not_found('Context item not found')
    return api_ok(deleted=True)


@api_v1_memory_bp.route('/api/v1/context/changes/<change_id>/undo',
                         methods=['POST'])
@require_auth
@api_meta(summary='Undo an assistant-learned context change', tags=['memory'])
def undo_user_context_change_v1(change_id):
    from lib.memory import user_profile as up
    try:
        result = up.undo_context_change(change_id, _context_scope())
    except up.ContextConflictError as exc:
        return api_conflict(str(exc))
    if result.get('not_found'):
        return api_not_found('Context change not found')
    return api_ok(result)


# ── Legacy profile compatibility ────────────────────────────────────

@api_v1_memory_bp.route('/api/v1/profile', methods=['GET'])
@require_auth
@api_meta(
    summary='Get the legacy profile projection',
    description=('Returns ``{body, items, chars, cap, over_cap, pending}`` — '
                 'a compatibility projection of structured My Context. '
                 '``items`` is the structured per-preference view '
                 '(``[{header, text}]``) the settings UI edits; ``body`` is '
                 'the raw markdown. ``pending`` is retained for back-compat '
                 '(now usually empty — new preferences auto-apply).'),
    tags=['memory'],
)
def get_user_profile_v1():
    from lib.memory import user_profile as up
    from .auth import current_auth
    scope = up.resolve_profile_scope(current_auth())
    body = up.load_profile(scope)
    return api_ok({
        'body': body,
        'items': up.parse_items(body),
        'chars': len(body),
        'cap': up.USER_PROFILE_CHAR_CAP,
        'over_cap': up.profile_over_cap(body, scope),
        'pending': up.load_pending(scope),
    })


@api_v1_memory_bp.route('/api/v1/profile', methods=['PUT'])
@require_auth
@api_meta(
    summary='Edit My Context through the legacy profile shape',
    description=('Body: ``{items: [{header, text}]}`` (structured, preferred) '
                 'OR ``{body}`` (raw markdown). An empty items list / body '
                 'clears the profile. Returns the save-result plus the '
                 're-parsed ``items``.'),
    tags=['memory'],
)
def put_user_profile_v1():
    from lib.memory import user_profile as up
    from .auth import current_auth
    scope = up.resolve_profile_scope(current_auth())
    data = parse_body()
    if isinstance(data.get('items'), list):
        res = up.save_items(data['items'], scope)
    else:
        res = up.save_profile(data.get('body', ''), scope)
    res['items'] = up.parse_items(scope=scope)
    return api_ok(res)


@api_v1_memory_bp.route('/api/v1/profile/pending/<pending_id>',
                         methods=['POST'])
@require_auth
@api_meta(
    summary='Confirm or dismiss a staged preference proposal',
    description=('Body: ``{accept: bool, text?: str}``. On accept the '
                 '(optionally edited) preference is written into the profile; '
                 'either way the proposal is removed from the pending list. '
                 'This is the propose-then-confirm gate — new preferences are '
                 'NEVER written silently.'),
    tags=['memory'],
)
def resolve_profile_pending_v1(pending_id):
    from lib.memory import user_profile as up
    from .auth import current_auth
    scope = up.resolve_profile_scope(current_auth())
    data = parse_body()
    res = up.resolve_pending(pending_id, accept=bool(data.get('accept')),
                             edited_text=data.get('text'), scope=scope)
    if res.get('busy'):
        return api_error('Pending proposal is already being resolved',
                         status=409)
    if res.get('error'):
        return api_error('Pending proposal could not be resolved', status=500)
    if not res.get('resolved'):
        return api_not_found('Pending proposal not found')
    return api_ok(res)


__all__ = ['api_v1_memory_bp']
