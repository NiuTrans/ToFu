"""routes/api_v1/site_packs.py — user-tier site pack management.

Site packs (docs/SITE_PACKS.md) internalize one site each: auth projection,
browser adapter operations, and model-facing routing hints. Core packs ship
with the package and release-tiered bundles ship per export mode; these
routes manage the user tier under ``<config_path>/site_packs/`` so a user
without a source checkout can internalize their own sites (including
intranet systems reachable only from their browser).

Routes:
  GET    /api/v1/site-packs             — list registered packs (with source layer)
  POST   /api/v1/site-packs             — validate a manifest and install it user-tier
  DELETE /api/v1/site-packs/<pack_id>   — remove a user-tier pack file

Validation is the same two-layer gate the loader applies (JSON Schema plus
the semantic rules in ``lib/site_packs/_manifest.py``), so a pack accepted
here can never fail hot reload later. Only the user tier is writable through
HTTP: core and bundle layers are deployment content.
"""

from __future__ import annotations

import os
from pathlib import Path

from quart import Blueprint

from lib.api_response import api_bad_request, api_error, api_ok
from lib.config_dir import config_path
from lib.json_store import write_json_atomic
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_site_packs_bp = Blueprint('api_v1_site_packs', __name__)


def _user_dir() -> Path:
    return Path(config_path('site_packs'))


def _layer_of(source_path: str) -> str:
    if not source_path:
        return 'unknown'
    path = Path(source_path)
    try:
        path.relative_to(_user_dir())
        return 'user'
    except ValueError:
        pass
    parts = path.parts
    if 'bundles' in parts:
        return 'bundle'
    return 'core'


def _pack_row(pack) -> dict:
    return {
        'id': pack.id,
        'name': pack.name,
        'version': pack.version,
        'domains': list(pack.domains),
        'aliases': list(pack.aliases),
        'routing_hint': pack.routing_hint,
        'layer': _layer_of(pack.source_path),
        'writable': _layer_of(pack.source_path) == 'user',
        'auth_projected': bool(pack.auth and pack.auth.project),
        'operations': [
            {'name': op.name, 'description': op.description,
             'access': op.access, 'timeout': op.timeout}
            for op in pack.operations
        ],
    }


@api_v1_site_packs_bp.route('/api/v1/site-packs', methods=['GET'])
@require_auth
@api_meta(
    summary='List registered site packs',
    description=(
        'Returns ``{packs: [...]}`` from the merged three-layer registry '
        '(core < bundle < user). Each row names its source ``layer`` and '
        'whether it is user-tier writable.'
    ),
    tags=['capabilities'],
)
def list_site_packs():
    from lib.site_packs import load_packs
    packs = [_pack_row(pack) for pack in load_packs().values()]
    return api_ok({'packs': packs, 'count': len(packs)})


@api_v1_site_packs_bp.route('/api/v1/site-packs', methods=['POST'])
@require_auth
@api_meta(
    summary='Install a user-tier site pack',
    description=(
        'Body: one ``tofu.site-pack/v1`` manifest object. Validated against '
        'the manifest contract (schema + semantic rules); an invalid pack is '
        'rejected with 400 and never written. When the id matches a '
        'core/bundle pack, the user-tier file overrides it (layering rule). '
        'Hot reload applies on the next registry read — no restart.'
    ),
    tags=['capabilities'],
)
def install_site_pack():
    from lib.site_packs._manifest import ManifestError, parse_manifest

    data = parse_body()
    if not isinstance(data, dict) or not data:
        return api_bad_request('body must be a site pack manifest object')
    try:
        manifest = parse_manifest(data, source='http-upload')
    except ManifestError as exc:
        return api_bad_request(str(exc))
    from lib.site_packs import load_packs
    before = load_packs().get(manifest.id)
    overridden = before is not None and _layer_of(before.source_path) != 'user'
    target = _user_dir() / f'{manifest.id}.json'
    try:
        os.makedirs(target.parent, exist_ok=True)
        write_json_atomic(str(target), data)
    except OSError as exc:
        logger.warning('[SitePacks] user pack write failed: %s', exc)
        return api_bad_request(f'could not persist pack: {exc}')
    installed = load_packs().get(manifest.id)
    logger.info('[SitePacks] user pack installed: %s (%d operations)',
                manifest.id, len(manifest.operations))
    return api_ok({
        'pack': _pack_row(installed) if installed else _pack_row(manifest),
        'overridden_core_or_bundle': overridden,
    })


@api_v1_site_packs_bp.route('/api/v1/site-packs/<pack_id>', methods=['DELETE'])
@require_auth
@api_meta(
    summary='Remove a user-tier site pack',
    description=(
        'Deletes the user-tier file for ``pack_id``. Core/bundle packs are '
        'deployment content and cannot be removed through HTTP — deleting an '
        'id that only exists in those layers returns 404. If a user-tier '
        'override is removed, the core/bundle pack of the same id becomes '
        'visible again on the next read.'
    ),
    tags=['capabilities'],
)
def delete_site_pack(pack_id: str):
    from lib.site_packs import load_packs

    target = _user_dir() / f'{pack_id}.json'
    if not pack_id or not target.is_file():
        return api_error(f'no user-tier site pack: {pack_id}', status=404)
    try:
        target.unlink()
    except OSError as exc:
        logger.warning('[SitePacks] user pack delete failed: %s', exc)
        return api_bad_request(f'could not delete pack: {exc}')
    restored = load_packs().get(pack_id)
    return api_ok({
        'deleted': pack_id,
        'core_or_bundle_restored': _pack_row(restored) if restored else None,
    })
