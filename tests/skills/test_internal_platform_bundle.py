"""Reviewed internal Skill source is discoverable, intact and never public-exported."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / 'bundles/yourprovider/skills'


def test_internal_source_manifest_is_complete_and_non_executable_by_default():
    manifest = json.loads((BUNDLE / 'manifest.json').read_text())
    assert manifest['schema'] == 'tofu-internal-platform-skills/1'
    assert manifest['export_modes'] == ['personal', 'internal']
    assert manifest['live_verified'] is False
    assert manifest['upstream_signature_verified'] is False
    expected = set(manifest['files']) | {'manifest.json'}
    actual = {str(path.relative_to(BUNDLE)) for path in BUNDLE.rglob('*') if path.is_file()}
    assert actual == expected
    for relative, record in manifest['files'].items():
        path = BUNDLE / relative
        assert not path.is_symlink()
        assert set(path.parts).isdisjoint({'node_modules', '.git', 'sessions', '__pycache__'})
        assert path.suffix not in {'.zip', '.sig', '.pyc'}
        data = path.read_bytes()
        assert len(data) == record['bytes']
        assert hashlib.sha256(data).hexdigest() == record['sha256']
        assert not re.search(rb'Bearer\s+[A-Za-z0-9+/._-]{24,}', data)
    for package in manifest['packages']:
        assert (BUNDLE / package / 'SKILL.md').is_file()


def test_internal_source_is_scanned_but_never_exported_publicly():
    from lib.skills.paths import skill_scan_dirs
    from export_pkg._excludes import _should_exclude
    scanned = {path for path, _scope in skill_scan_dirs(str(ROOT), None, owner_user_id=7)}
    assert str(BUNDLE) in scanned
    for relative in json.loads((BUNDLE / 'manifest.json').read_text())['files']:
        name = Path(relative).name
        path = 'bundles/yourprovider/skills/' + relative
        assert _should_exclude(path, name, 'opensource') is not None
        assert _should_exclude(path, name, 'internal') is None
        assert _should_exclude(path, name, 'personal') is None


def test_internal_packages_are_discovered_and_read_without_host_installs(monkeypatch):
    from lib.skills import registry
    from lib.skills.load import load_skill
    # Only committed source inputs are visible; no installed owner/global tree.
    monkeypatch.setattr(registry, '_skill_scan_dirs',
                        lambda *args, **kwargs: [(str(BUNDLE), 'project')])
    registry._skills_cache.clear()
    manifest = json.loads((BUNDLE / 'manifest.json').read_text())
    packages = registry.list_skills(str(ROOT), owner_user_id=7)
    assert {package['id'] for package in packages} == set(manifest['packages'])
    for package in packages:
        assert Path(package['package_dir']).parent == BUNDLE
        assert package.get('enabled', True)
        text = load_skill(package['id'], str(ROOT), owner_user_id=7)
        assert 'Skill loaded:' in text and '<skill_instructions>' in text
        assert 'content_hash:' in text
    registry._skills_cache.clear()


def test_portable_authority_links_resolve_and_do_not_advertise_omitted_upstream():
    for name in ('env-platform-skill', 'tb-harbor-image-pipeline'):
        package = BUNDLE / name
        assert (package / '../../../../docs/modules/skills.md').resolve().is_file()
        text = (package / 'SKILL.md').read_text()
        assert 'Portable internal source boundary' in text
        assert not (package / 'references/upstream-skill.md').exists()
        assert 'docs/sandbox-platform-skills.md' not in text
        assert not (package / 'lib/load-cookie.js').exists()


def test_sandbox_references_share_the_reviewed_owner_and_unwired_auth():
    for layer in ('gateway', 'gym', 'sidecar'):
        pointer = BUNDLE / 'env-platform-skill/references' / (layer + '-log-readme.md')
        target = pointer.parent / ('../../tb-harbor-image-pipeline/references/' + layer + '-log-readme.md')
        assert target.is_file()
        assert len(pointer.read_text()) < len(target.read_text())
    for name in ('env-platform-skill', 'tb-harbor-image-pipeline'):
        receipt = json.loads((BUNDLE / name / 'LOCAL-INSTALL.json').read_text())
        assert receipt['activation_receipt'] is None
        assert receipt['runtime_operations_executed'] is False
        for file in ('api-client.js', 'read-contract.js', 'sandbox-api.js'):
            assert (BUNDLE / name / 'lib' / file).is_file()
    for file in ('api-client.js', 'read-contract.js', 'sandbox-api.js'):
        assert (BUNDLE / 'env-platform-skill/lib' / file).read_bytes() == (BUNDLE / 'tb-harbor-image-pipeline/lib' / file).read_bytes()
    assert not (BUNDLE / 'tb-harbor-image-pipeline/scripts/campaign.js').exists()
    assert not (BUNDLE / 'tb-harbor-image-pipeline/scripts/image_pool.js').exists()
