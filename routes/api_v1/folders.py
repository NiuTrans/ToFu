"""routes/api_v1/folders.py — Conversation folder CRUD.

Folders are lightweight per-user organizational groups for conversations
(``conv.settings.folderId`` is the link). Stored as a flat JSON array at
``data/config/folders.json`` via :mod:`lib.json_store` (atomic + locked).

Routes:
  GET    /api/v1/folders                     — list all folders
  POST   /api/v1/folders                     — create
  PUT    /api/v1/folders/{folder_id}         — update fields
  DELETE /api/v1/folders/{folder_id}         — remove
  POST   /api/v1/folders/reorder             — bulk reorder by id list
"""

from __future__ import annotations

import secrets
import time

from flask import Blueprint

from lib.api_response import (
    api_bad_request, api_created, api_not_found, api_ok,
)
from lib.config_dir import config_path as _config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import optional_list, optional_str, parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_folders_bp = Blueprint('api_v1_folders', __name__)


_FOLDERS_PATH = _config_path('folders.json')


def _read_folders() -> list[dict]:
    data = read_json(_FOLDERS_PATH, default=[])
    return data if isinstance(data, list) else []


def _new_folder_id() -> str:
    return 'f_' + hex(int(time.time() * 1000))[2:] + secrets.token_hex(2)


# Single-user default — mirrors lib/conversations/meta_cache.py::DEFAULT_USER_ID.
# folders.json is a global per-install store today; the userId on the frame is
# forward-safety for when auth lands (the client drops a frame whose userId is
# not its own — see cross_tab_sync.js::_onFoldersChangedPush).
_FOLDERS_USER_ID = 1


def _notify_folders_changed(*, deleted_folder_id: str | None = None,
                            user_id: int = _FOLDERS_USER_ID) -> None:
    """Push a real-time ``folders_changed`` frame to connected clients.

    Folders are NOT a ``conversations`` row, so this does NOT reuse
    ``notify_conv_changed`` (which would fake a ``conv_changed`` frame). Instead
    it emits a dedicated folder-scoped frame on the SAME ``notify`` channel the
    conversation-sync subscription already listens on fleet-wide:

        { type:'folders_changed', deletedFolderId?, userId }

    The client refreshes the folder tree in place (mirroring the debounced
    non-destructive sidebar pattern) and, when ``deletedFolderId`` is present,
    unassigns local conversations off the removed folder on EVERY device — not
    just the one that clicked delete.

    Best-effort: a push failure never breaks the folder mutation (the periodic
    poll + next refocus still reconcile).
    """
    try:
        from lib.agent_core.push import push_event
        payload = {'type': 'folders_changed', 'userId': user_id}
        if deleted_folder_id is not None:
            payload['deletedFolderId'] = deleted_folder_id
        # taskId is a routing key only; folders aren't task-scoped, so a stable
        # sentinel is fine — the client subscribes notify:* (channel-wide).
        push_event('notify', '__folders__', payload)
    except Exception as e:
        logger.debug('[Folders] folders_changed push skipped: %s', e)


_FOLDER_SCHEMA = {
    'type': 'object',
    'properties': {
        'id': {'type': 'string'},
        'name': {'type': 'string'},
        'color': {'type': 'string'},
        'collapsed': {'type': 'boolean'},
        'order': {'type': 'integer'},
        'createdAt': {'type': 'integer'},
    },
    'required': ['id', 'name'],
}


@api_v1_folders_bp.route('/api/v1/folders', methods=['GET'])
@require_auth
@api_meta(
    summary='List conversation folders',
    description='Returns the full ordered folder list for the caller.',
    tags=['conversations'],
    responses={
        '200': {'description': 'OK', 'content': {'application/json': {
            'schema': {'type': 'object', 'properties': {
                'ok': {'type': 'boolean'},
                'items': {'type': 'array', 'items': _FOLDER_SCHEMA}}}}}},
    },
)
def list_folders():
    # Coordinated bare-array migration (batch 19): array under ``items``;
    # Api.folders.list unwraps with an Array.isArray fallback.
    return api_ok({'items': _read_folders()})


@api_v1_folders_bp.route('/api/v1/folders', methods=['POST'])
@require_auth
@api_meta(
    summary='Create a folder',
    tags=['conversations'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'required': ['name'], 'properties': {
            'name': {'type': 'string', 'maxLength': 80},
            'color': {'type': 'string', 'maxLength': 32}}}}}},
)
def create_folder():
    body = parse_body()
    name = optional_str(body, 'name', default='', max_len=80).strip()
    if not name:
        return api_bad_request('Folder name is required', field='name')
    color = optional_str(body, 'color', default='', max_len=32)

    new_folder: dict = {}

    def _mutate(folders):
        if not isinstance(folders, list):
            folders = []
        new_folder.update({
            'id': _new_folder_id(),
            'name': name,
            'color': color,
            'collapsed': False,
            'order': len(folders),
            'createdAt': int(time.time() * 1000),
        })
        folders.append(new_folder)
        return folders

    update_json_atomic(_FOLDERS_PATH, _mutate, default=[])
    logger.info('[Folders] created id=%s name=%r', new_folder['id'], name)
    _notify_folders_changed()
    return api_created(new_folder)


@api_v1_folders_bp.route('/api/v1/folders/<folder_id>', methods=['PUT'])
@require_auth
@api_meta(
    summary='Update a folder',
    description='Patch any subset of: name / color / collapsed / order.',
    tags=['conversations'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'properties': {
            'name': {'type': 'string', 'maxLength': 80},
            'color': {'type': 'string', 'maxLength': 32},
            'collapsed': {'type': 'boolean'},
            'order': {'type': 'integer'}}}}}},
)
def update_folder(folder_id):
    body = parse_body()
    found: list[dict] = []

    def _mutate(folders):
        if not isinstance(folders, list):
            folders = []
        for f in folders:
            if f.get('id') == folder_id:
                if 'name' in body:
                    new_name = optional_str(body, 'name', default='',
                                            max_len=80).strip()
                    if new_name:
                        f['name'] = new_name
                if 'color' in body:
                    f['color'] = optional_str(body, 'color', default='',
                                              max_len=32)
                if 'collapsed' in body:
                    f['collapsed'] = bool(body['collapsed'])
                if 'order' in body:
                    try:
                        f['order'] = int(body['order'])
                    except (TypeError, ValueError) as e:
                        logger.debug('[Folders] bad order=%r: %s',
                                     body['order'], e)
                found.append(f)
                break
        return folders

    update_json_atomic(_FOLDERS_PATH, _mutate, default=[])
    if not found:
        return api_not_found('Folder not found')
    logger.info('[Folders] updated id=%s name=%r', folder_id,
                found[0].get('name'))
    _notify_folders_changed()
    return api_ok(found[0])


@api_v1_folders_bp.route('/api/v1/folders/<folder_id>', methods=['DELETE'])
@require_auth
@api_meta(
    summary='Delete a folder',
    description=(
        'Removes the folder. Conversations referencing it via '
        '``settings.folderId`` are not modified server-side; the '
        'frontend unassigns them after the response.'
    ),
    tags=['conversations'],
)
def delete_folder(folder_id):
    deleted = {'flag': False}

    def _mutate(folders):
        if not isinstance(folders, list):
            return []
        kept = [f for f in folders if f.get('id') != folder_id]
        deleted['flag'] = len(kept) != len(folders)
        return kept

    update_json_atomic(_FOLDERS_PATH, _mutate, default=[])
    if not deleted['flag']:
        return api_not_found('Folder not found')
    logger.info('[Folders] deleted id=%s', folder_id)
    _notify_folders_changed(deleted_folder_id=folder_id)
    return api_ok()


@api_v1_folders_bp.route('/api/v1/folders/reorder', methods=['POST'])
@require_auth
@api_meta(
    summary='Reorder folders',
    description='Pass the desired folder-id sequence; missing ids keep '
                'their current relative order (placed after the listed ones).',
    tags=['conversations'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'required': ['order'],
                    'properties': {
                        'order': {'type': 'array',
                                   'items': {'type': 'string'}}}}}}},
)
def reorder_folders():
    body = parse_body()
    order = optional_list(body, 'order', item_type=str, default=[]) or []

    def _mutate(folders):
        if not isinstance(folders, list):
            return []
        idx_map = {fid: i for i, fid in enumerate(order)}
        for f in folders:
            fid = f.get('id')
            if fid in idx_map:
                f['order'] = idx_map[fid]
        return folders

    update_json_atomic(_FOLDERS_PATH, _mutate, default=[])
    _notify_folders_changed()
    return api_ok()


__all__ = ['api_v1_folders_bp']
