"""routes/api_v1/memory.py — Memory & Skill-package CRUD.

Routes:
  GET    /api/v1/memory                       — list
  POST   /api/v1/memory                       — create
  GET    /api/v1/memory/<id>                  — fetch one
  PUT    /api/v1/memory/<id>                  — update
  DELETE /api/v1/memory/<id>                  — delete
  POST   /api/v1/memory/merge                 — merge multiple
  POST   /api/v1/memory/<id>/toggle           — enable/disable
  POST   /api/v1/memory/install               — install zip (multipart or JSON)
  GET    /api/v1/memory/<id>/files            — list package files
  GET    /api/v1/memory/catalog               — curated catalog
  POST   /api/v1/memory/catalog/install       — install from catalog

All routes require authentication; mutations don't need ``admin`` scope
because memories are user-owned and the cookie-auth UI uses them
intensively (settings panel + Skills store + drag-drop install).
"""

from __future__ import annotations

import io
import os

import requests
from flask import Blueprint, jsonify, request

from lib.api_response import (
    api_bad_request, api_error, api_internal_error, api_not_found,
)
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_memory_bp = Blueprint('api_v1_memory', __name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

# Match the legacy installer caps (one-place to keep them in sync).
_INSTALL_MAX_BYTES = 25 * 1024 * 1024            # multipart install zip
_CATALOG_DL_CAP = 50 * 1024 * 1024               # curated catalog download
_CATALOG_DL_TIMEOUT = 60                         # seconds


def _project_path() -> str:
    """Resolve the project_path from the request, falling back to root."""
    explicit = None
    if request.is_json and request.get_json(silent=True):
        explicit = (request.get_json(silent=True) or {}).get('project_path')
    if not explicit:
        explicit = request.args.get('project_path')
    return explicit or _PROJECT_ROOT


# ── Local Memory CRUD ────────────────────────────────────────────────

@api_v1_memory_bp.route('/api/v1/memory', methods=['GET'])
@require_auth
@api_meta(
    summary='List memories',
    description=(
        'Returns ``{memories: [...], skills: [...]}`` (the ``skills`` '
        'alias is kept for backward compatibility with older Settings '
        'pages). Use ``?scope=all|project|global`` to filter.'
    ),
    tags=['memory'],
)
def list_memories_v1():
    from lib.memory import list_memories
    scope = request.args.get('scope', 'all')
    memories = list_memories(project_path=_project_path(), scope=scope)
    for m in memories:
        m.pop('filepath', None)
    return jsonify({'memories': memories, 'skills': memories})


@api_v1_memory_bp.route('/api/v1/memory/<memory_id>', methods=['GET'])
@require_auth
@api_meta(summary='Get one memory', tags=['memory'])
def get_memory_v1(memory_id):
    from lib.memory import get_memory
    mem = get_memory(memory_id, project_path=_project_path())
    if not mem:
        return api_not_found('Memory not found')
    mem.pop('filepath', None)
    return jsonify(mem)


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
    data = request.get_json(force=True)
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
    return jsonify(mem), 201


@api_v1_memory_bp.route('/api/v1/memory/<memory_id>', methods=['PUT'])
@require_auth
@api_meta(summary='Update a memory', tags=['memory'])
def update_memory_v1(memory_id):
    from lib.memory import update_memory
    data = request.get_json(force=True)
    mem = update_memory(memory_id, data, project_path=_project_path())
    if not mem:
        return api_not_found('Memory not found')
    mem.pop('filepath', None)
    return jsonify(mem)


@api_v1_memory_bp.route('/api/v1/memory/<memory_id>', methods=['DELETE'])
@require_auth
@api_meta(summary='Delete a memory', tags=['memory'])
def delete_memory_v1(memory_id):
    from lib.memory import delete_memory
    logger.warning('[Memory.v1] deleting %s', memory_id)
    ok = delete_memory(memory_id, project_path=_project_path())
    if not ok:
        logger.warning('[Memory.v1] %s not found for deletion', memory_id)
    return jsonify({'deleted': ok}), (200 if ok else 404)


@api_v1_memory_bp.route('/api/v1/memory/merge', methods=['POST'])
@require_auth
@api_meta(
    summary='Merge multiple memories',
    description='Body: ``{memory_ids, name, description, body, tags?, scope?}``',
    tags=['memory'],
)
def merge_memories_v1():
    from lib.memory import merge_memories
    data = request.get_json(force=True)
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
    return jsonify(result), 201


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
    return jsonify(mem)


# ── Skill-package install (drag-and-drop zip) ────────────────────────

@api_v1_memory_bp.route('/api/v1/memory/install', methods=['POST'])
@require_auth
@api_meta(
    summary='Install a skill package',
    description=(
        'Accepts ``multipart/form-data`` with a ``file`` field carrying '
        'the zip plus optional ``scope`` / ``overwrite`` / ``project_path`` '
        'form fields, OR JSON ``{path, scope?, overwrite?}`` when running '
        'locally with filesystem access to the source.'
    ),
    tags=['memory'],
)
def install_skill_package_v1():
    from lib.memory.installer import InstallerError, install_skill_package

    scope = 'project'
    overwrite = False
    source = None
    fname = None

    if request.content_type and request.content_type.startswith('multipart/'):
        if 'file' not in request.files:
            return api_bad_request('No file uploaded')
        f = request.files['file']
        fname = f.filename or 'upload.zip'
        scope = (request.form.get('scope') or 'project').strip().lower()
        overwrite = request.form.get('overwrite', '').lower() in ('1', 'true', 'yes')
        data = f.read(_INSTALL_MAX_BYTES + 1)
        if len(data) > _INSTALL_MAX_BYTES:
            return jsonify({
                'error': f'File exceeds {_INSTALL_MAX_BYTES // (1024 * 1024)} MB limit'
            }), 413
        source = bytes(data)
    else:
        body = parse_body()
        scope = (body.get('scope') or 'project').strip().lower()
        overwrite = bool(body.get('overwrite'))
        path = body.get('path') or ''
        if not path or not os.path.exists(path):
            return jsonify({'error': 'Provide a file upload or {"path": ...}'}), 400
        source = path
        fname = os.path.basename(path)

    if scope not in ('project', 'global'):
        return api_bad_request(f'Invalid scope: {scope}')

    project_path = _project_path()
    try:
        result = install_skill_package(
            source, scope=scope, project_path=project_path,
            overwrite=overwrite, original_filename=fname,
        )
    except InstallerError as e:
        logger.warning('[Memory.v1] Install rejected (%s): %s', fname, e)
        return api_bad_request(e)
    except Exception as e:
        logger.error('[Memory.v1] Install crashed (%s): %s', fname, e,
                     exc_info=True)
        return api_internal_error(f'Install failed: {e}')

    mem = result['memory']
    mem.pop('filepath', None)
    return jsonify({
        'memory': mem,
        'replaced': result['replaced'],
        'install_hints': result['install_hints'],
    }), 201


# ── Curated Catalog (App-Store style) ────────────────────────────────

@api_v1_memory_bp.route('/api/v1/memory/catalog', methods=['GET'])
@require_auth
@api_meta(
    summary='Curated skill catalog',
    description='Returns the curated catalog with ``installed`` flags per entry.',
    tags=['memory'],
)
def skill_catalog_v1():
    from lib.memory import list_all_memories
    from lib.memory.catalog import get_catalog
    project_path = _project_path()
    installed_ids = {
        m['id'] for m in list_all_memories(project_path=project_path)
        if m.get('is_package')
    }
    catalog = get_catalog()
    for entry in catalog:
        entry['installed'] = entry['id'] in installed_ids
    return jsonify({'catalog': catalog,
                    'installed_ids': sorted(installed_ids)})


@api_v1_memory_bp.route('/api/v1/memory/catalog/install', methods=['POST'])
@require_auth
@api_meta(
    summary='Install a skill package from the curated catalog',
    description='Body: ``{skill_id, scope?, overwrite?}``. Server downloads the zip.',
    tags=['memory'],
)
def skill_catalog_install_v1():
    from lib.memory.catalog import get_catalog_entry
    from lib.memory.installer import InstallerError, install_skill_package

    data = parse_body()
    skill_id = (data.get('skill_id') or '').strip()
    scope = (data.get('scope') or 'project').strip().lower()
    overwrite = bool(data.get('overwrite'))

    if not skill_id:
        return api_bad_request('skill_id is required')
    if scope not in ('project', 'global'):
        return api_bad_request(f'Invalid scope: {scope}')

    entry = get_catalog_entry(skill_id)
    if entry is None:
        return api_not_found(f'Unknown skill id: {skill_id}')

    url = entry.get('download_url', '')
    if not url.startswith('https://'):
        return api_bad_request('Catalog entry has no https download_url')

    logger.info('[Memory.v1] Catalog install: %s (scope=%s) from %s',
                skill_id, scope, url)
    try:
        resp = requests.get(url, timeout=_CATALOG_DL_TIMEOUT, stream=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning('[Memory.v1] Catalog download failed for %s: %s',
                       skill_id, e)
        return api_error(f'Download failed: {e}', status=502)

    buf = io.BytesIO()
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > _CATALOG_DL_CAP:
                logger.warning('[Memory.v1] Catalog zip %s exceeds cap',
                               skill_id)
                return jsonify({
                    'error': f'Archive exceeds {_CATALOG_DL_CAP // (1024 * 1024)} MB'
                }), 413
            buf.write(chunk)
    except requests.RequestException as e:
        logger.warning('[Memory.v1] Catalog stream error %s: %s', skill_id, e)
        return api_error(f'Download interrupted: {e}', status=502)

    project_path = _project_path()
    try:
        result = install_skill_package(
            bytes(buf.getvalue()), scope=scope, project_path=project_path,
            overwrite=overwrite, original_filename=f'{skill_id}.zip',
        )
    except InstallerError as e:
        logger.warning('[Memory.v1] Catalog install rejected (%s): %s',
                       skill_id, e)
        return api_bad_request(e)
    except Exception as e:
        logger.error('[Memory.v1] Catalog install crashed (%s): %s',
                     skill_id, e, exc_info=True)
        return api_internal_error(f'Install failed: {e}')

    mem = result['memory']
    mem.pop('filepath', None)
    return jsonify({
        'memory': mem,
        'replaced': result['replaced'],
        'install_hints': result['install_hints'],
        'catalog_id': skill_id,
    }), 201


@api_v1_memory_bp.route('/api/v1/memory/<memory_id>/files', methods=['GET'])
@require_auth
@api_meta(
    summary='List files inside an installed skill package',
    description='Returns ``{files: [{path, size, kind}], root: <package_dir>}``.',
    tags=['memory'],
)
def memory_files_v1(memory_id):
    from lib.memory import get_memory
    mem = get_memory(memory_id, project_path=_project_path())
    if not mem:
        return api_not_found('Memory not found')
    if not mem.get('is_package') or not mem.get('package_dir'):
        return api_bad_request('Not a package memory')

    root = mem['package_dir']
    if not os.path.isdir(root):
        return api_not_found('Package directory missing')

    files = []
    for dirpath, _dirs, fnames in os.walk(root):
        for fname in fnames:
            if fname.startswith('.'):
                continue
            full = os.path.join(dirpath, fname)
            try:
                sz = os.path.getsize(full)
            except OSError as e:
                logger.debug('[Memory.v1] files getsize failed: %s', e)
                sz = 0
            rel = os.path.relpath(full, root)
            low = fname.lower()
            if low == 'skill.md':
                kind = 'skill'
            elif low.endswith(('.md', '.txt', '.rst')):
                kind = 'doc'
            elif low.endswith(('.py', '.js', '.ts', '.sh', '.go', '.rb')):
                kind = 'script'
            elif low.endswith(('.json', '.yaml', '.yml', '.toml')):
                kind = 'config'
            else:
                kind = 'asset'
            files.append({'path': rel, 'size': sz, 'kind': kind})
    files.sort(key=lambda f: (f['kind'] != 'skill', f['path']))
    return jsonify({
        'memory_id': memory_id,
        'root': root,
        'files': files,
        'count': len(files),
    })


__all__ = ['api_v1_memory_bp']
