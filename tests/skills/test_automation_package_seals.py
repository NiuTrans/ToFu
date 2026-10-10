"""Complete immutable package seals preserve the existing release digest format."""
import io
from pathlib import Path
import subprocess
import zipfile
import pytest

from lib.skills.installer import (
    InstallerError, canonical_skill_content_sha256, validated_skill_package_seal,
    stage_skill_snapshot_package,
)

pytestmark=pytest.mark.unit


@pytest.fixture
def package(tmp_path):
    (tmp_path/'SKILL.md').write_bytes(b'---\nname: sealed-fixture\ndescription: Complete frozen content\n---\nUse the reference.\n')
    (tmp_path/'references').mkdir()
    (tmp_path/'references/all.bin').write_bytes(bytes(range(256)))
    return tmp_path


def test_complete_resources_match_preexisting_catalog_release_digest(package):
    seal=validated_skill_package_seal(str(package))
    # Golden digest was checked against the unmodified shared installer.
    assert seal['content_sha256']=='44584ebabb07fc5d565607164643cdc9e4fccc1c1f2999c27ab23b465dd3bcc7'
    assert seal['content_sha256']==canonical_skill_content_sha256(str(package))
    assert seal['file_count']==2 and seal['total_bytes']==341
    assert seal['files']=={
        'SKILL.md':{'size':85,'sha256':'951b64cfe157ed9f91fa8d4158dbd84b395070622a14cbdcf30a18c512f63e9f'},
        'references/all.bin':{'size':256,'sha256':'40aff2e9d2d8922e47afd4648e6967497158785fbd1da870e7110266bf944880'}}


def test_provenance_markers_do_not_rewrite_content_identity(package):
    before=validated_skill_package_seal(str(package))
    (package/'.skill-origin.json').write_text('{"server":"owned"}')
    (package/'.catalog_id').write_text('mtskills-fixture')
    assert validated_skill_package_seal(str(package))==before


def test_full_resource_changes_are_visible_even_when_guide_is_identical(package):
    before=validated_skill_package_seal(str(package))
    (package/'references/all.bin').write_bytes(b'changed frozen resource')
    after=validated_skill_package_seal(str(package))
    assert after['files']['SKILL.md']==before['files']['SKILL.md']
    assert after['content_sha256']!=before['content_sha256']
    assert after['files']['references/all.bin']!=before['files']['references/all.bin']


def test_seal_includes_unicode_resources_without_truncation(package):
    resource=package/'references/说明.txt'
    resource.write_text('完整说明\n'*2000,encoding='utf-8')
    seal=validated_skill_package_seal(str(package))
    assert seal['files']['references/说明.txt']['size']==resource.stat().st_size
    assert seal['file_count']==3 and seal['total_bytes']==341+resource.stat().st_size


def test_resource_symlinks_are_rejected_before_a_seal_is_published(package,tmp_path):
    secret=tmp_path.parent/'outside-secret';secret.write_text('never read outside package')
    (package/'references/all.bin').unlink()
    (package/'references/all.bin').symlink_to(secret)
    with pytest.raises(InstallerError):validated_skill_package_seal(str(package))


def archive(files):
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as output:
        for name,value in files.items():output.writestr(name,value)
    return buffer.getvalue()


def test_private_staging_does_not_install_invalidate_or_execute(tmp_path,monkeypatch):
    import lib.skills.registry as registry
    def forbidden(*args,**kwargs):
        raise AssertionError('Staging crossed installation/script execution boundary')
    monkeypatch.setattr(registry,'_invalidate_skills_cache',forbidden)
    monkeypatch.setattr(subprocess,'Popen',forbidden)
    marker=tmp_path/'must-not-exist'
    blob=archive({'package/SKILL.md':'---\nname: staged\ndescription: Sealed source\n---\nInstructions\n',
        'package/references/all.bin':bytes(range(256)),
        'package/scripts/setup.sh':f'#!/bin/sh\necho unsafe > {marker}\n'})
    with stage_skill_snapshot_package(blob,parent_directory=str(tmp_path),owner_user_id=7) as staged:
        root=Path(staged['root'])
        assert staged['owner_user_id']==7 and staged['scripts_executed'] is False
        assert staged['seal']['file_count']==3
        assert set(staged['seal']['files'])=={'SKILL.md','references/all.bin','scripts/setup.sh'}
        assert root.is_relative_to(tmp_path) and root.exists()
        assert not marker.exists() and not (tmp_path/'.tofu/skills').exists()
    assert not root.exists() and not marker.exists()


@pytest.mark.parametrize('files',[
    {'../escape':'unsafe'},
    {'one/SKILL.md':'---\nname: one\ndescription: first\n---\n',
     'two/SKILL.md':'---\nname: two\ndescription: second\n---\n'},
    {'package/SKILL.md':'missing frontmatter'},
])
def test_invalid_snapshot_stage_cleans_up_without_publication(tmp_path,files):
    with pytest.raises(InstallerError):
        with stage_skill_snapshot_package(archive(files),parent_directory=str(tmp_path),owner_user_id=7):
            raise AssertionError('Invalid package became an accepted stage')
    assert list(tmp_path.iterdir())==[]


def test_downstream_failure_disposes_unaccepted_stage(tmp_path):
    blob=archive({'SKILL.md':'---\nname: staged\ndescription: valid\n---\n'})
    with pytest.raises(RuntimeError,match='Freeze rejected'):
        with stage_skill_snapshot_package(blob,parent_directory=str(tmp_path),owner_user_id=7) as staged:
            root=Path(staged['root'])
            raise RuntimeError('Freeze rejected')
    assert not root.exists() and list(tmp_path.iterdir())==[]
