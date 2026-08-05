"""routes/api_v1/paper_folders.py — Reading-mode paper-library folder CRUD.

Folders are lightweight per-user organizational groups for papers on the
Reading-mode bookshelf (``paper_library.folder_id`` is the link). Stored as a
flat JSON array at ``data/config/paper_folders.json`` via :mod:`lib.json_store`
(atomic + locked) — deliberately mirroring the conversation-folder store
(:mod:`routes.api_v1.folders`) rather than sharing it, so paper and
conversation membership never collide under one delete/notify path.

Routes:
  GET    /api/v1/paper-folders                 — list all folders
  POST   /api/v1/paper-folders                 — create
  PUT    /api/v1/paper-folders/{folder_id}     — update fields
  DELETE /api/v1/paper-folders/{folder_id}     — remove
  POST   /api/v1/paper-folders/reorder         — bulk reorder by id list
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

api_v1_paper_folders_bp = Blueprint('api_v1_paper_folders', __name__)


_PAPER_FOLDERS_PATH = _config_path('paper_folders.json')


def _read_folders() -> list[dict]:
    data = read_json(_PAPER_FOLDERS_PATH, default=[])
    return data if isinstance(data, list) else []


def _new_folder_id() -> str:
    return 'pf_' + hex(int(time.time() * 1000))[2:] + secrets.token_hex(2)


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


@api_v1_paper_folders_bp.route('/api/v1/paper-folders', methods=['GET'])
@require_auth
@api_meta(
    summary='List paper-library folders',
    description='Returns the full ordered paper-folder list for the caller.',
    tags=['paper'],
    responses={
        '200': {'description': 'OK', 'content': {'application/json': {
            'schema': {'type': 'object', 'properties': {
                'ok': {'type': 'boolean'},
                'items': {'type': 'array', 'items': _FOLDER_SCHEMA}}}}}},
    },
)
def list_paper_folders():
    # Same coordinated bare-array migration as api_v1/folders.py.
    return api_ok({'items': _read_folders()})


@api_v1_paper_folders_bp.route('/api/v1/paper-folders', methods=['POST'])
@require_auth
@api_meta(
    summary='Create a paper folder',
    tags=['paper'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'required': ['name'], 'properties': {
            'name': {'type': 'string', 'maxLength': 80},
            'color': {'type': 'string', 'maxLength': 32}}}}}},
)
def create_paper_folder():
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

    update_json_atomic(_PAPER_FOLDERS_PATH, _mutate, default=[])
    logger.info('[PaperFolders] created id=%s name=%r', new_folder['id'], name)
    return api_created(new_folder)


@api_v1_paper_folders_bp.route('/api/v1/paper-folders/<folder_id>', methods=['PUT'])
@require_auth
@api_meta(
    summary='Update a paper folder',
    description='Patch any subset of: name / color / collapsed / order.',
    tags=['paper'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'properties': {
            'name': {'type': 'string', 'maxLength': 80},
            'color': {'type': 'string', 'maxLength': 32},
            'collapsed': {'type': 'boolean'},
            'order': {'type': 'integer'}}}}}},
)
def update_paper_folder(folder_id):
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
                        logger.debug('[PaperFolders] bad order=%r: %s',
                                     body['order'], e)
                found.append(f)
                break
        return folders

    update_json_atomic(_PAPER_FOLDERS_PATH, _mutate, default=[])
    if not found:
        return api_not_found('Folder not found')
    logger.info('[PaperFolders] updated id=%s name=%r', folder_id,
                found[0].get('name'))
    return api_ok(found[0])


@api_v1_paper_folders_bp.route('/api/v1/paper-folders/<folder_id>', methods=['DELETE'])
@require_auth
@api_meta(
    summary='Delete a paper folder',
    description=(
        'Removes the folder. Papers referencing it via '
        '``folder_id`` are not modified server-side; the frontend '
        'unassigns them after the response.'
    ),
    tags=['paper'],
)
def delete_paper_folder(folder_id):
    deleted = {'flag': False}

    def _mutate(folders):
        if not isinstance(folders, list):
            return []
        kept = [f for f in folders if f.get('id') != folder_id]
        deleted['flag'] = len(kept) != len(folders)
        return kept

    update_json_atomic(_PAPER_FOLDERS_PATH, _mutate, default=[])
    if not deleted['flag']:
        return api_not_found('Folder not found')
    logger.info('[PaperFolders] deleted id=%s', folder_id)
    return api_ok()


@api_v1_paper_folders_bp.route('/api/v1/paper-folders/reorder', methods=['POST'])
@require_auth
@api_meta(
    summary='Reorder paper folders',
    description='Pass the desired folder-id sequence; missing ids keep '
                'their current relative order (placed after the listed ones).',
    tags=['paper'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'required': ['order'],
                    'properties': {
                        'order': {'type': 'array',
                                   'items': {'type': 'string'}}}}}}},
)
def reorder_paper_folders():
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

    update_json_atomic(_PAPER_FOLDERS_PATH, _mutate, default=[])
    return api_ok()


__all__ = ['api_v1_paper_folders_bp']
