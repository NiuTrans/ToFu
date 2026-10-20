"""External native source inputs retain identity and owned snapshot boundaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from serverctl_pkg import native_engine as engines

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('legacy_workspace', [False, True])
def test_retained_engine_manifest_keeps_snapshot_workspace_local(tmp_path, legacy_workspace):
    """Cargo must not discover a workspace outside the staged engine crate."""
    cargo = shutil.which('cargo') or shutil.which(
        'cargo', path=str(Path(os.environ.get('CARGO_HOME', Path.home() / '.cargo')) / 'bin'))
    if cargo is None:
        pytest.skip('Cargo is required to verify actual workspace discovery')
    crate = tmp_path / 'target/update-source-root/packages/tofu-db'
    (crate / 'src').mkdir(parents=True)
    (crate / 'src/lib.rs').write_text('')
    source_manifest = Path(__file__).resolve().parents[2] / 'packages/tofu-db/Cargo.toml'
    shutil.copyfile(source_manifest, crate / 'Cargo.toml')
    (crate.parent / 'Cargo.toml').write_text('[workspace]\nexclude = ["tofu-db"]\n')
    ancestor = tmp_path / 'target/Cargo.toml'
    if legacy_workspace:
        ancestor.write_text('[workspace]\nexclude = ["update-source"]\n')
    before = {path: path.read_bytes() for path in tmp_path.rglob('Cargo.toml')}

    result = subprocess.run(
        [cargo, 'locate-project', '--workspace', '--manifest-path', str(crate / 'Cargo.toml')],
        capture_output=True, text=True, timeout=15)

    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)['root']) == crate / 'Cargo.toml'
    assert all(path.read_bytes() == content for path, content in before.items())


def project(tmp_path,inputs):
    crate=tmp_path/'packages/tofu-db'
    (crate/'src').mkdir(parents=True)
    (crate/'src/lib.rs').write_text('pub fn fixture() {}')
    (crate/'Cargo.toml').write_text('[package]\nname="tofu-db"\nversion="0.0.0"\n'
        '[package.metadata.tofu]\nexternal-inputs='+json.dumps(inputs)+'\n')
    (tmp_path/'contracts').mkdir()
    (tmp_path/'contracts/fixture.json').write_text('{}')
    return crate


@pytest.mark.parametrize('inputs',[['../foreign'],['contracts/fixture.json','contracts/fixture.json'],['contracts/missing.json']])
def test_invalid_external_inputs_refuse_identity(tmp_path,inputs):
    project(tmp_path,inputs)
    with pytest.raises(engines.EnginePreparationError):engines.source_fingerprint(tmp_path)


def test_symlinked_external_input_refuses_identity(tmp_path):
    project(tmp_path,['contracts/fixture.json'])
    path=tmp_path/'contracts/fixture.json';path.unlink()
    target=tmp_path/'foreign';target.write_text('{}');path.symlink_to(target)
    with pytest.raises(engines.EnginePreparationError,match='symlink'):
        engines.source_fingerprint(tmp_path)


def test_external_snapshot_cannot_overwrite_unowned_file(tmp_path):
    crate=project(tmp_path,['contracts/fixture.json'])
    destination=crate/'contracts/fixture.json';destination.parent.mkdir();destination.write_text('foreign')
    with pytest.raises(engines.EnginePreparationError,match='not owned'):
        engines._sync_build_snapshot(tmp_path,crate,crate/'target/update-source')
    assert destination.read_text()=='foreign'


def test_build_keeps_external_inputs_inside_owned_target_tree(tmp_path, monkeypatch):
    """Existing crate files must neither block nor be overwritten by preparation."""
    import runtime_guards
    from types import SimpleNamespace

    crate = project(tmp_path, ['contracts/fixture.json'])
    existing = crate / 'contracts/fixture.json'
    existing.parent.mkdir()
    existing.write_text('existing crate input')
    monkeypatch.setattr(engines.shutil, 'which', lambda *args, **kwargs: '/toolchain/cargo')
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda _: SimpleNamespace(
        effective_cpu_count=2, effective_memory_available_mb=2048))
    monkeypatch.setattr(engines.shutil, 'which', lambda *args, **kwargs: '/toolchain/cargo')
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda _: SimpleNamespace(
        effective_cpu_count=2, effective_memory_available_mb=2048))
    compiled = []

    def compile_snapshot(command, *, cwd, **kwargs):
        compiled.append(cwd)
        external_root = cwd.parent.parent
        assert external_root.is_relative_to(crate / 'target')
        assert (external_root / 'contracts/fixture.json').read_text() == '{}'
        assert (cwd / 'src/lib.rs').read_bytes() == (crate / 'src/lib.rs').read_bytes()
        assert existing.read_text() == 'existing crate input'
        return ''

    monkeypatch.setattr(engines, '_run', compile_snapshot)
    engines._build(tmp_path, tmp_path / 'staging', {})
    assert len(compiled) == 1
    assert existing.read_text() == 'existing crate input'


@pytest.mark.parametrize('relative', ['update-source-root', 'update-source-root/packages'])
def test_build_refuses_redirected_snapshot_ancestors(tmp_path, monkeypatch, relative):
    crate = project(tmp_path, ['contracts/fixture.json'])
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    marker = foreign / 'keep.txt'
    marker.write_text('foreign bytes')
    redirected = crate / 'target' / relative
    redirected.parent.mkdir(parents=True)
    redirected.symlink_to(foreign, target_is_directory=True)
    monkeypatch.setattr(engines.shutil, 'which', lambda *args, **kwargs: '/toolchain/cargo')
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    monkeypatch.setattr(engines, '_run', lambda *args, **kwargs: pytest.fail('unsafe build started'))
    monkeypatch.setattr(engines.shutil, 'which', lambda *args, **kwargs: '/toolchain/cargo')
    monkeypatch.setattr(engines, '_run', lambda *args, **kwargs: pytest.fail('unsafe build started'))

    with pytest.raises(engines.EnginePreparationError, match='symlink'):
        engines._build(tmp_path, tmp_path / 'staging', {})
    assert list(foreign.iterdir()) == [marker]
    assert marker.read_text() == 'foreign bytes'
