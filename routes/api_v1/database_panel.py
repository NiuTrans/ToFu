"""Settings → 数据库 panel API (personal deployments only).

One read surface plus bounded actions: benchmark start/poll, backend
switch (persisted to .env, applied by the existing restart machinery),
storage-path discovery + accelerated-write-front configuration, dual-path
backup, and preferences.  Every heavy action is a guarded background job;
this blueprint never touches the live authority beyond the certified
``system.backup`` snapshot path.
"""

from __future__ import annotations

import os

from quart import Blueprint, request

from lib.api_response import (
    api_bad_request, api_conflict, api_error, api_internal_error, api_ok,
)
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import require_scope


api_v1_database_panel_bp = Blueprint("api_v1_database_panel", __name__)

_TAG = ["system"]
_SCOPE = "admin"


def _personal_only_error():
    from lib.database_panel.probe import deployment_mode

    if deployment_mode() == 'personal':
        return None
    return api_conflict(
        'database panel is available in personal deployments only',
        errorKind='personal_only')


@api_v1_database_panel_bp.route("/api/v1/database/status", methods=["GET"])
@require_scope(_SCOPE)
@api_meta(
    summary="Database engine status for the settings panel",
    description=(
        "Current backend, readiness, data location + filesystem class, "
        "runtime-aware availability, preferences and backup witnesses. "
        "A lightweight read: no launch verification or disk write probes."
    ),
    tags=_TAG, scope=_SCOPE,
)
def database_status():
    guard = _personal_only_error()
    if guard is not None:
        return guard
    try:
        from lib.database_panel import config_store, probe, switch

        runtime = probe.runtime_status()
        engine = probe.engine_status(runtime)
        preferences = config_store.load_preferences()
        return api_ok({
            'engine': engine,
            'fastpath': probe.fastpath_state(engine['backend'], runtime),
            'searchProjection': probe.search_projection_state(engine['backend'], runtime),
            'backends': switch.describe(engine),
            'preferences': preferences,
            'migration': switch.migration_status(),
        })
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.database.status")


@api_v1_database_panel_bp.route("/api/v1/database/paths", methods=["GET"])
@require_scope(_SCOPE)
@api_meta(
    summary="Probe candidate storage paths",
    description=(
        "Runs the bounded filesystem capability probe on candidate "
        "locations (cached 10 min; pass ?refresh=1 to re-probe) and ranks "
        "a recommended faster persistent path."
    ),
    tags=_TAG, scope=_SCOPE,
)
def database_paths():
    guard = _personal_only_error()
    if guard is not None:
        return guard
    try:
        from lib.database_panel import probe

        refresh = request.args.get('refresh') == '1'
        return api_ok(probe.discover_paths(refresh=refresh))
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.database.paths")


@api_v1_database_panel_bp.route("/api/v1/database/paths", methods=["POST"])
@require_scope(_SCOPE)
@api_meta(
    summary="Configure native search projection or backend accelerated write front",
    description=(
        "TofuDB: {searchProjection: {directory}} saves a base directory for "
        "the required reconstructible search index; empty selects automatic. "
        "Custom bases are namespaced by authority. No live data is moved. "
        "Native fastPath requires an existing local persistent base and Strict WAL; "
        "activation or a retained FASTPATH marker locks selection until offline export. "
        "SQLite: {fastPath: {enabled, directory?}} persists TOFU_STORAGE_FASTPATH* "
        "to the project .env. Changes take effect only at the next launch."
    ),
    request_body={
        'required': True,
        'content': {'application/json': {'schema': {
            'oneOf': [
                {'type': 'object', 'required': ['searchProjection'],
                 'additionalProperties': False, 'properties': {
                     'searchProjection': {
                         'type': 'object', 'required': ['directory'],
                         'additionalProperties': False, 'properties': {
                             'directory': {'type': 'string', 'maxLength': 4096}}}}},
                {'type': 'object', 'required': ['fastPath'],
                 'additionalProperties': False, 'properties': {
                     'fastPath': {
                         'type': 'object', 'required': ['enabled'],
                         'additionalProperties': False, 'properties': {
                             'enabled': {'type': 'boolean'},
                             'directory': {'type': 'string', 'maxLength': 4096}}}}},
            ],
        }}},
    },
    tags=_TAG, scope=_SCOPE,
)
def database_paths_apply():
    guard = _personal_only_error()
    if guard is not None:
        return guard
    body = parse_body(strict=True, exact=('fastPath', 'searchProjection'))
    if len(body) != 1:
        return api_bad_request('Provide exactly one of fastPath or searchProjection')
    try:
        from lib.database_panel import probe

        engine = probe.engine_status()
        if 'searchProjection' in body:
            projection = body['searchProjection']
            if (not isinstance(projection, dict)
                    or set(projection) != {'directory'}
                    or not isinstance(projection['directory'], str)):
                return api_bad_request('searchProjection requires a string directory')
            if engine['backend'] != 'tofudb':
                return api_conflict(
                    'search projection configuration requires TofuDB',
                    errorKind='search_projection_unsupported')
            try:
                state = probe.configure_search_projection_directory(projection['directory'])
            except probe.SearchProjectionEnvironmentOverrideError as exc:
                return api_conflict(str(exc), errorKind='search_projection_env_override')
            except ValueError as exc:
                return api_bad_request(str(exc))
            return api_ok({
                'restartRequired': state['restartRequired'],
                'searchProjection': state,
            })

        fast_path = body['fastPath']
        if (not isinstance(fast_path, dict)
                or 'enabled' not in fast_path
                or not isinstance(fast_path['enabled'], bool)
                or set(fast_path) - {'enabled', 'directory'}):
            return api_bad_request('fastPath requires a boolean enabled')
        enabled = fast_path['enabled']
        directory = fast_path.get('directory', '')
        if (not isinstance(directory, str)
                or len(directory) > probe.MAX_DIRECTORY_LENGTH or '\x00' in directory):
            return api_bad_request('fastPath.directory must be a valid string path')
        if directory:
            from pathlib import Path

            if not Path(directory).is_absolute():
                return api_bad_request('fastPath.directory must be absolute')
        state = probe.fastpath_state(engine['backend'])
        if not state['supported']:
            return api_conflict(
                'accelerated path is managed by the active backend',
                errorKind='fastpath_unsupported')

        if engine['backend'] == 'tofudb':
            try:
                state = probe.configure_native_fastpath(enabled, directory)
            except probe.NativeFastpathSettingsError as exc:
                return api_conflict(str(exc), errorKind=exc.code)
            return api_ok({
                'restartRequired': state['restartRequired'], 'fastpath': state,
                'searchProjection': probe.search_projection_state('tofudb'),
            })

        from tofu_dotenv import update_dotenv_file
        from lib.database_panel.switch import env_file_path as _env_path

        if enabled and directory:
            from pathlib import Path

            target = Path(directory)
            report = probe.discover_paths(refresh=True)
            known = {entry['path']: entry for entry in report['candidates']}
            evidence = known.get(str(target))
            if evidence is None:
                capability = probe.probe_single_path(
                    target, create_directory=True)
                writable = bool(capability.get('writable'))
                persistent = capability.get('persistence') == 'persistent'
                free = capability.get('free_bytes') or 0
            else:
                writable = bool(evidence['writable'])
                persistent = evidence['persistence'] == 'persistent'
                free = evidence['freeBytes'] or 0
            if not writable or not persistent:
                return api_bad_request(
                    'fastPath.directory must be writable and persistent')
            if free < 2 * 1024 ** 3:
                return api_bad_request(
                    'fastPath.directory needs at least 2 GiB free')
            update_dotenv_file(_env_path(), {
                'TOFU_STORAGE_FASTPATH': 'auto',
                'TOFU_STORAGE_FASTPATH_DIR': str(target),
            })
            # See switch._write_backend_env: the re-exec inherits this
            # process environment, so keep it consistent with the file.
            os.environ['TOFU_STORAGE_FASTPATH'] = 'auto'
            os.environ['TOFU_STORAGE_FASTPATH_DIR'] = str(target)
        else:
            mode = 'auto' if enabled else 'off'
            update_dotenv_file(
                _env_path(), {'TOFU_STORAGE_FASTPATH': mode},
                remove=('TOFU_STORAGE_FASTPATH_DIR',))
            os.environ['TOFU_STORAGE_FASTPATH'] = mode
            os.environ.pop('TOFU_STORAGE_FASTPATH_DIR', None)
        return api_ok({
            'restartRequired': True,
            'fastpath': probe.fastpath_state(engine['backend']),
            'searchProjection': None,
        })
    except ValueError as exc:
        return api_bad_request(str(exc))
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.database.paths_apply")


@api_v1_database_panel_bp.route("/api/v1/database/preferences", methods=["POST"])
@require_scope(_SCOPE)
@api_meta(
    summary="Save panel preferences",
    description=(
        "Body: {backup?: {enabled, directory}}. The maintenance thread "
        "honors backup (daily verified mirror to the stable directory)."
    ),
    tags=_TAG, scope=_SCOPE,
)
def database_preferences():
    guard = _personal_only_error()
    if guard is not None:
        return guard
    body = parse_body(exact=('backup',))
    try:
        backup = body.get('backup') or {}
        directory = backup.get('directory')
        if directory is not None:
            from pathlib import Path

            directory = str(directory).strip()
            if directory and not Path(directory).expanduser().is_absolute():
                return api_bad_request('backup.directory must be absolute')
        from lib.database_panel import config_store

        preferences = config_store.save_preferences(
            backup_enabled=(bool(backup['enabled'])
                            if 'enabled' in backup else None),
            backup_directory=directory,
        )
        return api_ok({'preferences': preferences})
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.database.preferences")


@api_v1_database_panel_bp.route("/api/v1/database/backend", methods=["POST"])
@require_scope(_SCOPE)
@api_meta(
    summary="Switch the storage backend",
    description=(
        "Body: {backend: 'tofudb'|'sqlite', migrate?: bool}. Persists "
        "TOFU_PERSONAL_STORAGE_BACKEND to the project .env and answers "
        "restartRequired; the client then runs the approval-gated "
        "/api/v1/update/restart flow. sqlite→tofudb with migrate=true "
        "first runs the certified offline import as a background job "
        "(poll /api/v1/database/migration). Existing data is never "
        "deleted by a switch."
    ),
    tags=_TAG, scope=_SCOPE,
)
def database_backend_switch():
    guard = _personal_only_error()
    if guard is not None:
        return guard
    body = parse_body(exact=('backend', 'migrate'))
    try:
        from lib.database_panel import switch

        result = switch.request_switch(
            str(body.get('backend') or ''),
            migrate=bool(body.get('migrate')))
        return api_ok(result)
    except switch.SwitchError as exc:
        status = 409 if exc.code in (
            'already_active', 'migration_running') else 400
        return api_error(str(exc), status=status, kind=exc.code)
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.database.backend")


@api_v1_database_panel_bp.route("/api/v1/database/migration", methods=["GET"])
@require_scope(_SCOPE)
@api_meta(
    summary="Poll the SQLite→TofuDB migration job",
    tags=_TAG, scope=_SCOPE,
)
def database_migration():
    guard = _personal_only_error()
    if guard is not None:
        return guard
    try:
        from lib.database_panel import switch

        return api_ok(switch.migration_status())
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.database.migration")


@api_v1_database_panel_bp.route("/api/v1/database/backup", methods=["POST"])
@require_scope(_SCOPE)
@api_meta(
    summary="Mirror one verified backup to the stable directory now",
    description=(
        "Body: {directory?} — defaults to the configured backup "
        "directory. The selected authority owns the consistent snapshot; "
        "this copies the verified artifact + manifest durably."
    ),
    tags=_TAG, scope=_SCOPE,
)
def database_backup_now():
    guard = _personal_only_error()
    if guard is not None:
        return guard
    body = parse_body(exact=('directory',))
    try:
        from lib.database_panel import backup

        result = backup.run_backup(str(body.get('directory') or ''))
        return api_ok({'backup': result})
    except backup.BackupError as exc:
        status = 409 if exc.code == 'backup_busy' else 400
        return api_error(str(exc), status=status, kind=exc.code)
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.database.backup")
