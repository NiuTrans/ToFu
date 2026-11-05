"""Promote project skill packages into the owner-global store.

Owner decision 2026-10-31: every skill is owner-global. Project roots
(``lib.skills.paths.PROJECT_SKILL_ROOTS``) remain readable sources, but the
runtime authority for an id is the owner-global package, which shadows any
same-id project copy (``lib.skills.paths.skill_scan_dirs``). This module moves
project packages to that authority through the normal validated, atomic
installer. It never deletes or edits the project source, never executes
bundled scripts and never replaces a different global package unless asked.

Entry points:
    python -m lib.skills.promote --owner 1 --scan PROJECT [PROJECT ...]
    python -m lib.skills.promote --owner 1 --scan PROJECT --apply
    python -m lib.skills.promote --owner 1 [--check] [--replace] SKILL_DIR [...]

Per package status: ``missing`` (no global package with this id),
``identical`` (same canonical content digest), ``differs`` (a global package
with this id has other content), ``global-edited`` (the global package was
installed from content identical to this source and edited afterwards, so
this source is the older copy). ``--check`` and plain ``--scan`` only
report. Otherwise ``missing`` is installed, ``identical`` is skipped and
``differs`` is refused unless ``--replace`` is given: replacing the global
copy is a deliberate owner decision, never a side effect of promotion.
``global-edited`` is refused even with ``--replace``, which would discard
those edits.

A package Tofu installed into a project keeps its provenance. Its own origin
markers are not copied; while their content seal still matches the source
bytes (``source_origin: sealed``) the catalog id, revision, registry and URL
they record are carried into the global install (``provenance``). An edited
source (``source_origin: edited``) carries none, because the record no longer
describes those bytes. Every install is pinned to the inspected digest.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from lib.memory.storage import _memory_from_file
from lib.skills.installer import (
    InstallerError,
    _make_memory_id,
    _validate_skill_md,
    canonical_skill_content_sha256,
    install_skill_package,
)
from lib.skills.paths import PROJECT_SKILL_ROOTS, owner_global_skills_dir

#: Origin fields that say where package content came from. The installer
#: writes the per-install fields (owner, time, content seal) itself.
_PROVENANCE_FIELDS = ('catalog_id', 'source_revision', 'source_registry', 'source_url')

_REFUSALS = {
    'differs': ('a different global package already owns this id; '
                'compare them, then pass --replace to overwrite it'),
    'global-edited': ('the global package was edited after it was installed from '
                      'content identical to this source, so this source is the older '
                      'copy and --replace would discard those edits; copy them into '
                      'the source or remove the global package first'),
}


def recorded_origin(package_dir: str) -> dict[str, str]:
    """Return the origin Tofu recorded when it installed ``package_dir``.

    Reads the markers the registry exposes; every field is ``''`` for a
    package Tofu did not install.
    """
    record = _memory_from_file(
        os.path.join(package_dir, 'SKILL.md'), package_dir=package_dir,
        include_body=False) or {}
    return {field: str(record.get(field) or '')
            for field in (*_PROVENANCE_FIELDS, 'content_sha256')}


def project_packages(project_path: str) -> list[str]:
    """Return package directories under every declared project skill root."""
    packages: list[str] = []
    for subdir, _visibility, _exported in PROJECT_SKILL_ROOTS:
        root = os.path.join(project_path, subdir)
        if not os.path.isdir(root) or os.path.islink(root):
            continue
        for entry in sorted(os.listdir(root)):
            package = os.path.join(root, entry)
            if (not entry.startswith('.') and os.path.isdir(package)
                    and not os.path.islink(package)
                    and os.path.isfile(os.path.join(package, 'SKILL.md'))):
                packages.append(package)
    return packages


def inspect_package(source: str, *, owner_user_id: int) -> dict[str, Any]:
    """Classify one source package against the owner-global store."""
    row: dict[str, Any] = {'source': source}
    try:
        if os.path.islink(source) or not os.path.isfile(os.path.join(source, 'SKILL.md')):
            raise InstallerError('not a skill package directory (SKILL.md missing or symlinked root)')
        metadata = _validate_skill_md(os.path.join(source, 'SKILL.md'))
        skill_id = _make_memory_id(str(metadata['name']).strip())
        if not skill_id:
            raise InstallerError('SKILL.md name does not produce a valid id')
        row['id'] = skill_id
        row['digest'] = canonical_skill_content_sha256(source)
    except (InstallerError, OSError, ValueError) as exc:
        row.update(status='invalid', error=str(exc))
        return row
    origin = recorded_origin(source)
    if origin['content_sha256']:
        sealed = origin['content_sha256'] == row['digest']
        row['source_origin'] = 'sealed' if sealed else 'edited'
        provenance = {field: origin[field] for field in _PROVENANCE_FIELDS if origin[field]}
        if sealed and provenance:
            row['provenance'] = provenance
    target = os.path.join(owner_global_skills_dir(owner_user_id), skill_id)
    row['global_path'] = target
    if not os.path.isfile(os.path.join(target, 'SKILL.md')):
        row['status'] = 'missing'
    elif canonical_skill_content_sha256(target) == row['digest']:
        row['status'] = 'identical'
    elif recorded_origin(target)['content_sha256'] == row['digest']:
        row['status'] = 'global-edited'
    else:
        row['status'] = 'differs'
    return row


def promote(sources: list[str], *, owner_user_id: int, apply: bool,
            replace: bool) -> list[dict[str, Any]]:
    """Inspect, then install ``missing`` (and ``differs`` with ``replace``)."""
    rows = []
    for source in sources:
        row = inspect_package(os.path.abspath(source), owner_user_id=owner_user_id)
        status = row['status']
        if apply and (status == 'missing' or (status == 'differs' and replace)):
            try:
                result = install_skill_package(
                    row['source'], scope='global', owner_user_id=owner_user_id,
                    overwrite=status == 'differs',
                    exclude_source_origin_markers=True,
                    expected_content_sha256=row['digest'],
                    **row.get('provenance', {}))
                row['action'] = 'replaced' if result['replaced'] else 'installed'
                row['installed_id'] = (result.get('memory') or {}).get('id')
            except InstallerError as exc:
                row.update(action='failed', error=str(exc))
        elif apply and status in _REFUSALS:
            row['action'] = 'refused'
            row['error'] = _REFUSALS[status]
        else:
            row['action'] = 'none'
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m lib.skills.promote',
        description='Promote project skill packages into the owner-global store.')
    parser.add_argument('--owner', type=int, required=True, help='owner user id (personal deployments: 1)')
    parser.add_argument('--scan', action='store_true',
                        help='treat positional paths as projects and inspect every declared skill root')
    parser.add_argument('--apply', action='store_true', help='with --scan: install the reported packages')
    parser.add_argument('--check', action='store_true', help='with package paths: report only')
    parser.add_argument('--replace', action='store_true',
                        help='overwrite a different same-id global package (explicit owner '
                             'decision; never one edited after it was installed from this source)')
    parser.add_argument('paths', nargs='+')
    args = parser.parse_args(argv)
    if args.scan:
        sources = [package for project in args.paths for package in project_packages(project)]
        apply = args.apply
    else:
        sources = list(args.paths)
        apply = not args.check
    rows = promote(sources, owner_user_id=args.owner, apply=apply, replace=args.replace)
    for row in rows:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    failed = sum(1 for row in rows if row.get('action') in ('failed', 'refused')
                 or row['status'] == 'invalid')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
