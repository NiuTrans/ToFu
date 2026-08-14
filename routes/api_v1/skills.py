"""routes/api_v1/skills.py — Skill-package API surface.

Skills are USER-installed capability packs — a different noun from
memories (``routes/api_v1/memory.py``). This blueprint owns every
skill-management endpoint:

Routes:
  GET    /api/v1/skills                       — list installed packages
  POST   /api/v1/skills/install               — install zip (multipart or JSON)
  GET    /api/v1/skills/catalog               — curated catalog
  POST   /api/v1/skills/catalog/install       — install from catalog
  GET    /api/v1/skills/<id>/files            — list package files
  POST   /api/v1/skills/<id>/toggle           — enable/disable
  DELETE /api/v1/skills/<id>                  — uninstall

All routes require authentication; mutations don't need ``admin`` scope
because skill packages are user-owned and the cookie-auth UI uses them
intensively (Settings → Skills tab: store, drag-drop install, enable
toggle, uninstall).
"""

from __future__ import annotations

import io
import os

import requests
from quart import Blueprint, request

from lib.quart_sync import request_files, request_form

from lib.api_response import (
    api_bad_request, api_created, api_error, api_internal_error,
    api_not_found, api_ok,
)
from lib.http_client import http_get
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import require_auth
from .memory import _project_path

logger = get_logger(__name__)

api_v1_skills_bp = Blueprint('api_v1_skills', __name__)

# Match the legacy installer caps (one-place to keep them in sync).
_INSTALL_MAX_BYTES = 25 * 1024 * 1024            # multipart install zip
_CATALOG_DL_CAP = 50 * 1024 * 1024               # curated catalog download
_CATALOG_DL_TIMEOUT = 60                         # seconds


# ── Installed packages ───────────────────────────────────────────────

@api_v1_skills_bp.route('/api/v1/skills', methods=['GET'])
@require_auth
@api_meta(
    summary='List installed skill packages',
    description=(
        'Returns ``{skills: [...]}`` — every installed skill package '
        '(project + global scope). Use ``?scope=all|project|global`` to '
        'filter. Memories are served by ``/api/v1/memory`` instead.'
    ),
    tags=['skills'],
)
def list_skills_v1():
    from lib.skills import list_skills
    scope = request.args.get('scope', 'all')
    skills = list_skills(project_path=_project_path())
    if scope != 'all':
        skills = [s for s in skills if s.get('scope') == scope]
    for s in skills:
        s.pop('filepath', None)
        s.pop('package_dir', None)
    return api_ok({'skills': skills})


@api_v1_skills_bp.route('/api/v1/skills/<skill_id>', methods=['DELETE'])
@require_auth
@api_meta(
    summary='Uninstall a skill package',
    description='Removes the package directory. User action; the model '
                'cannot uninstall skills (memory CRUD is package-guarded).',
    tags=['skills'],
)
def uninstall_skill_v1(skill_id):
    from lib.skills import uninstall_skill
    logger.warning('[Skills.v1] uninstalling %s', skill_id)
    ok = uninstall_skill(skill_id, project_path=_project_path())
    if not ok:
        logger.warning('[Skills.v1] %s not found for uninstall', skill_id)
        return api_not_found('Skill package not found', deleted=False)
    return api_ok(deleted=True)


@api_v1_skills_bp.route('/api/v1/skills/<skill_id>/toggle', methods=['POST'])
@require_auth
@api_meta(
    summary='Enable / disable a skill package',
    description='A disabled skill stays installed but leaves the '
                '``<available_skills>`` index and cannot be activated.',
    tags=['skills'],
)
def toggle_skill_v1(skill_id):
    from lib.memory import get_memory, toggle_memory
    data = parse_body()
    mem = get_memory(skill_id, project_path=_project_path())
    if not mem or not mem.get('is_package'):
        return api_not_found('Skill package not found')
    mem = toggle_memory(skill_id, enabled=data.get('enabled'),
                        project_path=_project_path())
    mem.pop('filepath', None)
    mem.pop('package_dir', None)
    return api_ok(mem)


@api_v1_skills_bp.route('/api/v1/skills/<skill_id>/files', methods=['GET'])
@require_auth
@api_meta(
    summary='List files inside an installed skill package',
    description='Returns ``{files: [{path, size, kind}], root: <package_dir>}``.',
    tags=['skills'],
)
def skill_files_v1(skill_id):
    from lib.skills import get_skill, list_skill_files
    skill = get_skill(skill_id, project_path=_project_path())
    if not skill:
        return api_not_found('Skill package not found')

    root = skill['package_dir']
    if not os.path.isdir(root):
        return api_not_found('Package directory missing')

    files = list_skill_files(root)
    files.sort(key=lambda f: (f['kind'] != 'skill', f['path']))
    return api_ok({
        'skill_id': skill_id,
        'root': root,
        'files': files,
        'count': len(files),
    })


@api_v1_skills_bp.route('/api/v1/skills/<skill_id>/env', methods=['GET'])
@require_auth
@api_meta(
    summary='Skill env bindings status (redacted)',
    description='Returns ``{env: [{name, declared, configured, hint}]}`` — '
                'values never cross the wire.',
    tags=['skills'],
)
def skill_env_status_v1(skill_id):
    from lib.skills import get_skill
    from lib.skills.env import skill_env_status
    skill = get_skill(skill_id, project_path=_project_path())
    if not skill:
        return api_not_found('Skill package not found')
    return api_ok({'skill_id': skill_id, 'env': skill_env_status(skill)})


@api_v1_skills_bp.route('/api/v1/skills/<skill_id>/env', methods=['PUT'])
@require_auth
@api_meta(
    summary='Set a skill env binding (vault-backed)',
    description='Body: ``{name, value}``. Stored Fernet-encrypted in the '
                'credential vault; subprocess execution picks it up '
                'automatically. The response echoes only redacted metadata.',
    tags=['skills'],
)
def skill_env_set_v1(skill_id):
    from lib.skills import get_skill
    from lib.skills.env import set_skill_env
    skill = get_skill(skill_id, project_path=_project_path())
    if not skill:
        return api_not_found('Skill package not found')
    data = parse_body()
    name = (data.get('name') or '').strip()
    value = (data.get('value') or '').strip()
    if not name:
        return api_bad_request('name is required', field='name')
    if not value:
        return api_bad_request('value is required', field='value')
    try:
        meta = set_skill_env(skill_id, name, value)
    except ValueError as e:
        return api_bad_request(str(e), field='name')
    return api_ok({'binding': meta})


@api_v1_skills_bp.route('/api/v1/skills/<skill_id>/env/<env_name>',
                        methods=['DELETE'])
@require_auth
@api_meta(summary='Delete a skill env binding', tags=['skills'])
def skill_env_delete_v1(skill_id, env_name):
    from lib.skills import get_skill
    from lib.skills.env import delete_skill_env
    skill = get_skill(skill_id, project_path=_project_path())
    if not skill:
        return api_not_found('Skill package not found')
    if not delete_skill_env(skill_id, env_name):
        return api_not_found(f'No binding for {env_name}')
    return api_ok({'name': env_name})


@api_v1_skills_bp.route('/api/v1/skills/<skill_id>/scope', methods=['POST'])
@require_auth
@api_meta(
    summary='Move a skill between project and global scope',
    description='Body: ``{scope: "project"|"global"}``. Global skills are '
                'visible in project-less chat mode too.',
    tags=['skills'],
)
def skill_scope_v1(skill_id):
    from lib.skills import set_skill_scope
    data = parse_body()
    scope = (data.get('scope') or '').strip().lower()
    if scope not in ('project', 'global'):
        return api_bad_request(f'Invalid scope: {scope}', field='scope')
    try:
        skill = set_skill_scope(skill_id, scope,
                                project_path=_project_path())
    except ValueError as e:
        return api_bad_request(str(e))
    if not skill:
        return api_not_found('Skill package not found')
    skill.pop('filepath', None)
    skill.pop('package_dir', None)
    return api_ok({'skill': skill})


# ── Skill-package install (drag-and-drop zip) ────────────────────────

@api_v1_skills_bp.route('/api/v1/skills/install', methods=['POST'])
@require_auth
@api_meta(
    summary='Install a skill package',
    description=(
        'Accepts ``multipart/form-data`` with a ``file`` field carrying '
        'the zip plus optional ``scope`` / ``overwrite`` / ``project_path`` '
        'form fields, OR JSON ``{path, scope?, overwrite?}`` when running '
        'locally with filesystem access to the source.'
    ),
    tags=['skills'],
)
def install_skill_package_v1():
    from lib.skills import InstallerError, install_skill_package

    scope = 'project'
    overwrite = False
    source = None
    fname = None

    if request.content_type and request.content_type.startswith('multipart/'):
        files = request_files()
        if 'file' not in files:
            return api_bad_request('No file uploaded')
        f = files['file']
        fname = f.filename or 'upload.zip'
        form = request_form()
        scope = (form.get('scope') or 'global').strip().lower()
        overwrite = form.get('overwrite', '').lower() in ('1', 'true', 'yes')
        data = f.read(_INSTALL_MAX_BYTES + 1)
        if len(data) > _INSTALL_MAX_BYTES:
            return api_error(
                f'File exceeds {_INSTALL_MAX_BYTES // (1024 * 1024)} MB limit',
                status=413)
        source = bytes(data)
    else:
        body = parse_body()
        scope = (body.get('scope') or 'global').strip().lower()
        overwrite = bool(body.get('overwrite'))
        path = body.get('path') or ''
        if not path or not os.path.exists(path):
            return api_error('Provide a file upload or {"path": ...}', status=400)
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
        logger.warning('[Skills.v1] Install rejected (%s): %s', fname, e)
        return api_bad_request(e)
    except Exception as e:
        logger.error('[Skills.v1] Install crashed (%s): %s', fname, e,
                     exc_info=True)
        return api_internal_error(f'Install failed: {e}')

    mem = result['memory']
    mem.pop('filepath', None)
    mem.pop('package_dir', None)
    return api_created({
        'memory': mem,
        'replaced': result['replaced'],
        'install_hints': result['install_hints'],
    })


# ── Curated Catalog (App-Store style) ────────────────────────────────

@api_v1_skills_bp.route('/api/v1/skills/catalog', methods=['GET'])
@require_auth
@api_meta(
    summary='Curated skill catalog',
    description='Returns the curated catalog with ``installed`` flags per entry.',
    tags=['skills'],
)
def skill_catalog_v1():
    from lib.skills import get_catalog, list_skills
    project_path = _project_path()
    packages = list_skills(project_path=project_path)
    installed_ids = {m['id'] for m in packages}
    # Catalog-installed packages carry a ``.catalog_id`` marker; match on
    # that first so e.g. catalog ``xlsx-skill`` (memory id ``xlsx``) shows
    # as installed.  Fall back to the raw id for drag-dropped packages
    # whose folder name happens to equal a catalog id.
    by_catalog_id = {m['catalog_id']: m['id'] for m in packages
                     if m.get('catalog_id')}
    catalog = get_catalog()
    for entry in catalog:
        cid = entry['id']
        mem_id = by_catalog_id.get(cid) or (cid if cid in installed_ids
                                            else None)
        entry['installed'] = mem_id is not None
        entry['installed_memory_id'] = mem_id or ''
    return api_ok({'catalog': catalog,
                   'installed_ids': sorted(installed_ids)})


@api_v1_skills_bp.route('/api/v1/skills/catalog/install', methods=['POST'])
@require_auth
@api_meta(
    summary='Install a skill package from the curated catalog',
    description='Body: ``{skill_id, scope?, overwrite?}``. Server downloads the zip.',
    tags=['skills'],
)
def skill_catalog_install_v1():
    from lib.skills import (
        InstallerError, get_catalog_entry, install_skill_package,
    )

    data = parse_body()
    skill_id = (data.get('skill_id') or '').strip()
    # Default GLOBAL (owner directive 2026-08-05): external capability packs
    # are cross-project by nature; project-scoped installs were invisible in
    # project-less chat mode (memory skills-chat-mode-project-scope-invisibility).
    scope = (data.get('scope') or 'global').strip().lower()
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

    logger.info('[Skills.v1] Catalog install: %s (scope=%s) from %s',
                skill_id, scope, url)
    try:
        resp = http_get(url, timeout=_CATALOG_DL_TIMEOUT, stream=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning('[Skills.v1] Catalog download failed for %s: %s',
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
                logger.warning('[Skills.v1] Catalog zip %s exceeds cap',
                               skill_id)
                return api_error(
                    f'Archive exceeds {_CATALOG_DL_CAP // (1024 * 1024)} MB',
                    status=413)
            buf.write(chunk)
    except requests.RequestException as e:
        logger.warning('[Skills.v1] Catalog stream error %s: %s', skill_id, e)
        return api_error(f'Download interrupted: {e}', status=502)

    project_path = _project_path()
    try:
        result = install_skill_package(
            bytes(buf.getvalue()), scope=scope, project_path=project_path,
            overwrite=overwrite, original_filename=f'{skill_id}.zip',
            catalog_id=skill_id, subdir=entry.get('subdir') or None,
        )
    except InstallerError as e:
        logger.warning('[Skills.v1] Catalog install rejected (%s): %s',
                       skill_id, e)
        return api_bad_request(e)
    except Exception as e:
        logger.error('[Skills.v1] Catalog install crashed (%s): %s',
                     skill_id, e, exc_info=True)
        return api_internal_error(f'Install failed: {e}')

    mem = result['memory']
    mem.pop('filepath', None)
    mem.pop('package_dir', None)
    return api_created({
        'memory': mem,
        'replaced': result['replaced'],
        'install_hints': result['install_hints'],
        'catalog_id': skill_id,
    })


__all__ = ['api_v1_skills_bp']
