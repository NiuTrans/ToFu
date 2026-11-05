"""Install an in-tree companion skill into one owner's global skill store.

Entry point: ``ensure_companion_skill(source_dir, owner_user_id=...)``. The
MCP catalog install route calls it for a curated connector that declares
``companion_skill`` (``lib.mcp.registry.companion_skill_dir``), so the
workflow guide exists exactly where its connector does. It reuses the
promotion inspector and the validated, digest-pinned, atomic installer and
never executes package scripts.

Outcomes: a missing guide is installed; an identical one is left alone; a
copy Tofu installed from an earlier companion revision and nobody edited is
updated; every other same-id package (the owner's own or an edited copy)
is kept. A failure is reported, never raised: the connector install it
accompanies does not depend on it.
"""
from __future__ import annotations

from lib.log import get_logger
from lib.skills.installer import (
    InstallerError,
    canonical_skill_content_sha256,
    install_skill_package,
)
from lib.skills.promote import inspect_package, recorded_origin

logger = get_logger(__name__)

#: ``source_registry`` recorded on companion installs; it is how a later
#: revision recognizes the copy as its own.
COMPANION_SOURCE_REGISTRY = 'tofu-companion'


def _unedited_companion_copy(package_dir: str) -> bool:
    origin = recorded_origin(package_dir)
    return bool(
        origin['source_registry'] == COMPANION_SOURCE_REGISTRY
        and origin['content_sha256']
        and origin['content_sha256'] == canonical_skill_content_sha256(package_dir))


def ensure_companion_skill(source_dir: str, *, owner_user_id: int) -> dict:
    """Install or refresh one companion package; return ``{id, action}``.

    ``action`` is ``installed``, ``updated``, ``unchanged``, ``kept`` (a
    different same-id package wins) or ``failed``.
    """
    row = inspect_package(source_dir, owner_user_id=owner_user_id)
    skill_id = str(row.get('id') or '')
    status = row['status']
    if status == 'identical':
        return {'id': skill_id, 'action': 'unchanged'}
    if status == 'invalid':
        logger.error('[Skills] companion package %s is invalid: %s',
                     source_dir, row.get('error'))
        return {'id': skill_id, 'action': 'failed'}
    try:
        replace = (status == 'differs'
                   and _unedited_companion_copy(row['global_path']))
        if status != 'missing' and not replace:
            return {'id': skill_id, 'action': 'kept'}
        install_skill_package(
            row['source'], scope='global', owner_user_id=owner_user_id,
            overwrite=replace, expected_content_sha256=row['digest'],
            source_registry=COMPANION_SOURCE_REGISTRY)
    except (InstallerError, OSError) as exc:
        logger.warning('[Skills] companion skill %s install failed: %s',
                       skill_id, exc)
        return {'id': skill_id, 'action': 'failed'}
    return {'id': skill_id, 'action': 'updated' if replace else 'installed'}


__all__ = ['COMPANION_SOURCE_REGISTRY', 'ensure_companion_skill']
