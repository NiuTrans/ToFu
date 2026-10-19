"""Sidebar folder preset HTTP adapters, delivered through the v4 route map.

The repository lib/conversations/folders.py owns atomic records, owner checks,
validation and revision fencing. Routes resolve identity, parse declared fields,
and publish the existing folders_changed hint only after committed mutation.
Legacy unowned personal records belong to the composition's bootstrap owner.
"""
from __future__ import annotations

from quart import Blueprint
from lib.api_response import api_conflict, api_created, api_not_found, api_ok
from lib.config_dir import config_path as _config_path
from lib.conversations.folders import FolderRepository, FolderRevisionConflict
from lib.identity import PERSONAL_USER_ID
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import optional_list, parse_body
from .auth import request_user_id, require_auth

logger = get_logger(__name__)
api_v1_folders_bp = Blueprint('api_v1_folders', __name__)
_FOLDERS_PATH = _config_path('folders.json')

# Composition-level legacy migration, never an inferred request/record owner.
def _repository() -> FolderRepository:
    return FolderRepository(_FOLDERS_PATH, legacy_owner_user_id=PERSONAL_USER_ID)


def _notify_folders_changed(*, deleted_folder_id: str | None = None, user_id: int) -> None:
    try:
        from lib.agent_core.events import build_push_frame
        from lib.agent_core.push import push_event
        fields = {'userId': user_id}
        if deleted_folder_id is not None:
            fields['deletedFolderId'] = deleted_folder_id
        push_event('notify', '__folders__', build_push_frame('folders_changed', **fields), user_id=user_id)
    except Exception as error:
        logger.debug('[Folders] committed folder push skipped: %s', error)


_ROOT_LIST_SCHEMA = {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 512}, 'maxItems': 32}
_DEFAULT_PROPERTIES = {
    'projectPath': {'type': 'string', 'maxLength': 512},
    'projectPaths': _ROOT_LIST_SCHEMA,
    'readOnlyPaths': _ROOT_LIST_SCHEMA,
}
_FOLDER_SCHEMA = {
    'type': 'object', 'properties': {
        'id': {'type': 'string'}, 'name': {'type': 'string'},
        'color': {'type': 'string'}, 'collapsed': {'type': 'boolean'},
        'order': {'type': 'integer'}, **_DEFAULT_PROPERTIES,
        'revision': {'type': 'integer', 'minimum': 0}, 'createdAt': {'type': 'integer'},
    }, 'required': ['id', 'name'],
}
_WRITE_PROPERTIES = {
    'name': {'type': 'string', 'minLength': 1, 'maxLength': 80},
    'color': {'type': 'string', 'maxLength': 7, 'pattern': '^(?:#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6})?$'}, **_DEFAULT_PROPERTIES,
}


@api_v1_folders_bp.route('/api/v1/folders', methods=['GET'])
@require_auth
@api_meta(summary='List conversation folders', tags=['conversations'], responses={
    '200': {'description': 'Owner-visible confirmed folder presets', 'content': {'application/json': {
        'schema': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}, 'items': {'type': 'array', 'items': _FOLDER_SCHEMA}}}}}},
})
def list_folders():
    return api_ok({'items': _repository().list(request_user_id())})


@api_v1_folders_bp.route('/api/v1/folders', methods=['POST'])
@require_auth
@api_meta(summary='Create a folder', tags=['conversations'], request_body={
    'required': True, 'content': {'application/json': {'schema': {
        'type': 'object', 'required': ['name'], 'properties': _WRITE_PROPERTIES}}}},
    responses={'201': {'description': 'Created confirmed preset', 'content': {'application/json': {'schema': _FOLDER_SCHEMA}}}},
)
def create_folder():
    user_id = request_user_id()
    body = parse_body(strict=True, exact=tuple(_WRITE_PROPERTIES))
    created = _repository().create(user_id, body)
    _notify_folders_changed(user_id=user_id)
    return api_created(created)


@api_v1_folders_bp.route('/api/v1/folders/<folder_id>', methods=['PUT'])
@require_auth
@api_meta(summary='Update project folder settings', tags=['conversations'], request_body={
    'required': True, 'content': {'application/json': {'schema': {
        'type': 'object', 'properties': {**_WRITE_PROPERTIES,
            'collapsed': {'type': 'boolean'}, 'order': {'type': 'integer', 'minimum': 0},
            'expectedRevision': {'type': 'integer', 'minimum': 0}}}}}},
    responses={'200': {'description': 'Updated confirmed preset', 'content': {'application/json': {'schema': _FOLDER_SCHEMA}}},
               '409': {'description': 'Settings revision changed; reload before applying draft'}},
)
def update_folder(folder_id):
    user_id = request_user_id()
    body = parse_body(strict=True, exact=(*_WRITE_PROPERTIES, 'collapsed', 'order', 'expectedRevision'))
    try:
        updated = _repository().update(user_id, folder_id, body)
    except FolderRevisionConflict as error:
        return api_conflict(str(error))
    if updated is None:
        return api_not_found('Folder not found')
    _notify_folders_changed(user_id=user_id)
    return api_ok(updated)


@api_v1_folders_bp.route('/api/v1/folders/<folder_id>', methods=['DELETE'])
@require_auth
@api_meta(summary='Delete a folder', tags=['conversations'],
          description='Removes only this owner\'s folder grouping; conversation workspaces are unchanged.')
def delete_folder(folder_id):
    user_id = request_user_id()
    if not _repository().delete(user_id, folder_id):
        return api_not_found('Folder not found')
    _notify_folders_changed(user_id=user_id, deleted_folder_id=folder_id)
    return api_ok()


@api_v1_folders_bp.route('/api/v1/folders/reorder', methods=['POST'])
@require_auth
@api_meta(summary='Reorder folders', tags=['conversations'], request_body={
    'required': True, 'content': {'application/json': {'schema': {
        'type': 'object', 'required': ['order'], 'properties': {'order': {'type': 'array', 'items': {'type': 'string'}}}}}}})
def reorder_folders():
    user_id = request_user_id()
    body = parse_body(strict=True, exact=('order',))
    _repository().reorder(user_id, optional_list(body, 'order', item_type=str, default=[]) or [])
    _notify_folders_changed(user_id=user_id)
    return api_ok()


__all__ = ['api_v1_folders_bp']
