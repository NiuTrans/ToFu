"""Owner-scoped sidebar folder presets on the existing atomic JSON boundary.

Folders organize conversations and supply ordered defaults only to new drafts.
This repository owns validation/revisions/persistence, not workspace permission.
HTTP composition supplies the path and explicit legacy owner; unowned historical
records are visible only to that owner, never adopted by the first requester.
No background workers, permanent caches, directory scans or second binding store.
"""
from __future__ import annotations

from copy import deepcopy
import re
import secrets
import time

from lib.identity import require_user_id
from lib.json_store import read_json, update_json_atomic, JsonStoreReadError
from lib.request_parser import BadRequest

MAX_ROOTS = 32
MAX_PATH_LENGTH = 512


def is_valid_folder_color(value) -> bool:
    """Only automatic or hex3/hex6 palette colors are presentation-safe."""
    return isinstance(value, str) and (
        value == '' or re.fullmatch(r'#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?', value) is not None
    )


class FolderRevisionConflict(ValueError):
    """Caller edited an older confirmed preset; retry after explicit reload."""


def clean_project_paths(raw, *, field='projectPaths') -> list[str]:
    if not isinstance(raw, list):
        raise BadRequest(f'{field} must be an array', field=field)
    if len(raw) > MAX_ROOTS:
        raise BadRequest(f'{field} supports at most {MAX_ROOTS} directories', field=field)
    result = []
    for item in raw:
        if not isinstance(item, str):
            raise BadRequest(f'{field} entries must be strings', field=field)
        value = item.strip()
        if not value or len(value) > MAX_PATH_LENGTH or '\x00' in value:
            raise BadRequest(f'{field} entries must be nonempty paths of at most {MAX_PATH_LENGTH} characters', field=field)
        if value not in result:
            result.append(value)
    return result


def validate_folder_updates(body: dict, *, creating=False) -> dict:
    result = {}
    if creating or 'name' in body:
        name = body.get('name')
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise BadRequest('Folder name must contain 1–80 characters', field='name')
        result['name'] = name.strip()
    if 'color' in body:
        color = body['color']
        if not is_valid_folder_color(color):
            raise BadRequest('Folder color must be empty or a #RGB or #RRGGBB hex color', field='color')
        result['color'] = color
    for field in ('order', 'expectedRevision'):
        if field in body:
            value = body[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BadRequest(f'{field} must be a nonnegative integer', field=field)
            result[field] = value
    if 'collapsed' in body:
        if not isinstance(body['collapsed'], bool):
            raise BadRequest('collapsed must be a boolean', field='collapsed')
        result['collapsed'] = body['collapsed']
    if 'projectPaths' in body:
        result['projectPaths'] = clean_project_paths(body['projectPaths'])
        if 'projectPath' in body and body['projectPath'] != (result['projectPaths'][0] if result['projectPaths'] else ''):
            raise BadRequest('projectPath must match the primary projectPaths entry', field='projectPath')
    elif 'projectPath' in body:
        single = body['projectPath']
        if not isinstance(single, str) or len(single.strip()) > MAX_PATH_LENGTH or '\x00' in single:
            raise BadRequest('projectPath must be a bounded string', field='projectPath')
        result['projectPaths'] = [single.strip()] if single.strip() else []
    elif creating:
        result['projectPaths'] = []
    if 'readOnlyPaths' in body:
        result['readOnlyPaths'] = clean_project_paths(body['readOnlyPaths'], field='readOnlyPaths')
    elif creating:
        result['readOnlyPaths'] = []
    return result


class FolderRepository:
    def __init__(self, path: str, *, legacy_owner_user_id: int):
        self.path = path
        self.legacy_owner = require_user_id(legacy_owner_user_id, context='legacy folder owner')

    def _owner(self, row: dict) -> int:
        return row.get('ownerUserId', self.legacy_owner)

    @staticmethod
    def _rows(value):
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise JsonStoreReadError('Existing folder store is not a record array; refusing data loss')
        return value

    @staticmethod
    def _view(row):
        result = deepcopy(row)

        # Historical arbitrary CSS/markup stays in storage, never in a browser view.
        color = result.get('color', '')
        result['color'] = color if is_valid_folder_color(color) else ''
        paths = result.get('projectPaths')
        if not isinstance(paths, list):
            paths = [result['projectPath']] if result.get('projectPath') else []
        result['projectPaths'] = list(dict.fromkeys(paths))
        result['projectPath'] = result['projectPaths'][0] if result['projectPaths'] else ''
        result['readOnlyPaths'] = [path for path in result.get('readOnlyPaths', []) if path in result['projectPaths']]
        result['revision'] = result.get('revision', 0)
        result.pop('ownerUserId', None)
        return result

    def list(self, user_id: int) -> list[dict]:
        owner = require_user_id(user_id, context='folder list')
        rows = self._rows(read_json(self.path, default=[], strict=True))
        return [self._view(row) for row in rows if self._owner(row) == owner]

    def create(self, user_id: int, updates: dict) -> dict:
        owner = require_user_id(user_id, context='folder create')
        fields = validate_folder_updates(updates, creating=True)
        readonly = fields['readOnlyPaths']
        if any(path not in fields['projectPaths'] for path in readonly):
            raise BadRequest('readOnlyPaths must be a subset of projectPaths', field='readOnlyPaths')
        created = {}
        def mutate(value):
            rows = self._rows(value)
            created.update(id='f_' + secrets.token_hex(12), name=fields['name'],
                color=fields.get('color', ''), collapsed=False,
                order=sum(self._owner(row) == owner for row in rows),
                projectPaths=fields['projectPaths'], projectPath=fields['projectPaths'][0] if fields['projectPaths'] else '',
                readOnlyPaths=readonly, revision=1, ownerUserId=owner,
                createdAt=int(time.time() * 1000))
            rows.append(created)
            return rows
        update_json_atomic(self.path, mutate, default=[], strict=True, mode=0o600)
        return self._view(created)

    def update(self, user_id: int, folder_id: str, updates: dict) -> dict | None:
        owner = require_user_id(user_id, context='folder update')
        fields = validate_folder_updates(updates)
        found = []
        def mutate(value):
            rows = self._rows(value)
            for row in rows:
                if row.get('id') != folder_id or self._owner(row) != owner:
                    continue
                revision = row.get('revision', 0)
                expected = fields.get('expectedRevision')
                if expected is not None and expected != revision:
                    raise FolderRevisionConflict('Project settings changed elsewhere. Reload before saving again.')
                view = self._view(row)
                paths = fields.get('projectPaths', view['projectPaths'])
                readonly = fields.get('readOnlyPaths', [p for p in view['readOnlyPaths'] if p in paths])
                if any(path not in paths for path in readonly):
                    raise BadRequest('readOnlyPaths must be a subset of projectPaths', field='readOnlyPaths')
                row.update({key: val for key, val in fields.items() if key not in {'expectedRevision', 'projectPaths', 'readOnlyPaths'}})
                row.update(ownerUserId=owner, revision=revision + 1, projectPaths=paths,
                           projectPath=paths[0] if paths else '', readOnlyPaths=readonly)
                found.append(self._view(row))
                break
            return rows
        update_json_atomic(self.path, mutate, default=[], strict=True, mode=0o600)
        return found[0] if found else None

    def delete(self, user_id: int, folder_id: str) -> bool:
        owner = require_user_id(user_id, context='folder delete')
        deleted = []
        def mutate(value):
            rows = self._rows(value)
            kept = []
            for row in rows:
                if row.get('id') == folder_id and self._owner(row) == owner:
                    deleted.append(row)
                else:
                    kept.append(row)
            return kept
        update_json_atomic(self.path, mutate, default=[], strict=True, mode=0o600)
        return bool(deleted)

    def reorder(self, user_id: int, folder_ids: list[str]) -> None:
        owner = require_user_id(user_id, context='folder reorder')
        def mutate(value):
            rows = self._rows(value)
            owned = [row for row in rows if self._owner(row) == owner]
            ranks = {folder_id: index for index, folder_id in enumerate(dict.fromkeys(folder_ids))}
            owned.sort(key=lambda row: (ranks.get(row['id'], len(ranks)), row.get('order', 0)))
            for index, row in enumerate(owned):
                row.update(order=index, ownerUserId=owner)
            return rows
        update_json_atomic(self.path, mutate, default=[], strict=True, mode=0o600)
