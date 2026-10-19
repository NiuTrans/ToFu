"""Compiled source identity must survive staging and track transitive inputs."""
from pathlib import Path
import os
import shutil
import subprocess

import pytest

from serverctl_pkg import native_engine as engines

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_manifest_identity_parse_failure_is_not_an_empty_closure(tmp_path):
    crate = tmp_path / 'packages/tofu-db'
    crate.mkdir(parents=True)
    (crate / 'Cargo.toml').write_text('invalid TOML')
    with pytest.raises(engines.EnginePreparationError, match='source manifest'):
        engines.source_fingerprint(tmp_path)


@pytest.mark.parametrize("explicit_include", [False, True])
@pytest.mark.parametrize("external_schema", [False, True])
def test_compiled_identity_matches_python_after_staging_and_transitive_edit(tmp_path, explicit_include, external_schema):
    cargo = shutil.which('cargo')
    if cargo is None:
        pytest.skip('cross-language identity requires Cargo')
    root = tmp_path / 'application'
    crate = root / 'packages/tofu-db'
    runtime = root / 'packages/tofu-runtime'
    vendor = runtime / 'vendor/leaf'
    for location in (crate, runtime, vendor):
        (location / 'src').mkdir(parents=True)
    (crate / 'Cargo.toml').write_text('''[package]
name = "tofu-db"
version = "0.0.0"
edition = "2021"
[dependencies]
tofu-runtime = { path = "../tofu-runtime", optional = true, default-features = false }
[build-dependencies]
sha2 = "0.10"
toml = "1.1"
''')
    if external_schema:
        (root / 'contracts').mkdir()
        (root / 'contracts/probe.json').write_text('{"version":1}')
    shutil.copyfile(ROOT / 'packages/tofu-db/build.rs', crate / 'build.rs')
    (crate / 'src/main.rs').write_text('fn main() { println!("{}", env!("TOFUDB_SOURCE_SHA256")); }\n')
    (runtime / 'Cargo.toml').write_text('''[package]
name = "tofu-runtime"
version = "0.0.0"
edition.workspace = true
[dependencies]
leaf = { path = "vendor/leaf" }
''')
    (runtime / 'src/lib.rs').write_text('pub fn runtime() {}\n')
    (vendor / 'Cargo.toml').write_text('[package]\nname = "leaf"\nversion = "0.0.0"\n')
    leaf_source = vendor / 'src/lib.rs'
    leaf_source.write_text('pub fn leaf() {}\n')
    (root / 'packages/Cargo.toml').write_text('''[workspace]
members = ["tofu-runtime", "tofu-runtime/vendor/leaf"]
exclude = ["tofu-db"]
[workspace.package]
edition = "2021"
''')
    (crate / '.cargo').mkdir()
    (crate / '.cargo/config.toml').write_text('[build]\nincremental = false\n')
    if explicit_include:
        # Multiline/single-quoted TOML and package include selection must agree
        # in both languages without dropping the concurrent selection policy.
        manifest = vendor / 'Cargo.toml'
        manifest.write_text(manifest.read_text() + "include = [\n 'src/lib.rs',\n]\n")
        (vendor / 'src/ignored.rs').write_text('not retained by include rules')
    environment = dict(os.environ, CARGO_TARGET_DIR=str(tmp_path / 'build-cache'))

    def compiled(location):
        result = subprocess.run([cargo, 'run', '--offline', '--quiet'], cwd=location,
                                env=environment, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    (root / 'contracts').mkdir(exist_ok=True)
    contract = root / 'contracts/input.json'
    contract.write_text('"first"')
    manifest = crate / 'Cargo.toml'
    external_inputs = '["contracts/input.json", "contracts/probe.json"]' if external_schema else '["contracts/input.json"]'
    manifest.write_text(manifest.read_text() + '\n[package.metadata.tofu]\nexternal-inputs = ' + external_inputs + '\n')
    source = crate / 'src/main.rs'
    source.write_text('const INPUT: &str = include_str!("../../../contracts/input.json");\n'
                      'fn main() { assert!(!INPUT.is_empty()); println!("{}", env!("TOFUDB_SOURCE_SHA256")); }\n')
    before = compiled(crate)
    assert before == engines.source_fingerprint(root)
    snapshot = crate / 'target/update-source'
    engines._sync_build_snapshot(root, crate, snapshot)
    assert compiled(snapshot) == before
    staged_contract = snapshot.parent.parent / 'contracts/input.json'
    stamp = staged_contract.stat().st_mtime_ns
    engines._sync_build_snapshot(root, crate, snapshot)
    assert staged_contract.stat().st_mtime_ns == stamp
    contract.write_text('"changed"')
    changed = compiled(crate)
    assert changed != before and changed == engines.source_fingerprint(root)
    engines._sync_build_snapshot(root, crate, snapshot)
    assert compiled(snapshot) == changed
    before = changed
    # This dependency is optional/inactive, but its retained sources still
    # belong to the engine closure and must trigger the build script on retry.
    leaf_source.write_text('pub fn leaf_changed() {}\n')
    after = compiled(crate)
    assert after != before
    assert after == engines.source_fingerprint(root)
    engines._sync_build_snapshot(root, crate, snapshot)
    assert compiled(snapshot) == after
    if external_schema:
        previous=engines.source_fingerprint(root)
        (root / 'contracts/probe.json').write_text('{"version":2}')
        assert compiled(crate)==engines.source_fingerprint(root)!=previous
        engines._sync_build_snapshot(root, crate, snapshot)
        assert compiled(snapshot)==engines.source_fingerprint(root)
    # Source configuration contributes too; documentation and target caches do not.
    (crate / '.cargo/config.toml').write_text('[build]\nincremental = true\n')
    assert compiled(crate) == engines.source_fingerprint(root) != after
    (vendor / 'README.md').write_text('not a compiled source input')
    assert compiled(crate) == engines.source_fingerprint(root)


@pytest.mark.parametrize('name', ['../escape', '/tmp/escape', 'contracts/../escape', 'VERSION/file'])
def test_external_input_rejects_unsafe_declaration(tmp_path, name):
    crate = tmp_path / 'packages/tofu-db'
    crate.mkdir(parents=True)
    (crate / 'Cargo.toml').write_text('[package]\nname="tofu-db"\n[package.metadata.tofu]\nexternal-inputs = ["' + name + '"]\n')
    with pytest.raises(engines.EnginePreparationError, match='namespace'):
        engines.external_source_inputs(tmp_path)


def test_external_snapshot_prunes_only_recorded_inputs_and_refuses_symlinks(tmp_path):
    root = tmp_path / 'application'
    crate = root / 'packages/tofu-db'
    crate.mkdir(parents=True)
    contract = root / 'contracts/input.json'
    contract.parent.mkdir()
    contract.write_text('{}')
    manifest = crate / 'Cargo.toml'
    manifest.write_text('[package]\nname="tofu-db"\n[package.metadata.tofu]\nexternal-inputs = ["contracts/input.json"]\n')
    snapshot = tmp_path / 'owned/source/tofu-db'
    engines._sync_build_snapshot(root, crate, snapshot)
    external = snapshot.parent.parent / 'contracts'
    unrelated = external / 'unrelated.json'
    unrelated.write_text('keep')
    manifest.write_text('[package]\nname="tofu-db"\n')
    engines._sync_build_snapshot(root, crate, snapshot)
    assert not (external / 'input.json').exists()
    assert unrelated.read_text() == 'keep'
    manifest.write_text('[package]\nname="tofu-db"\n[package.metadata.tofu]\nexternal-inputs = ["contracts/input.json"]\n')
    (external / 'input.json').symlink_to(contract)
    with pytest.raises(engines.EnginePreparationError, match='unsafe path'):
        engines._sync_build_snapshot(root, crate, snapshot)
    contract.unlink()
    contract.symlink_to(unrelated)
    with pytest.raises(engines.EnginePreparationError, match='symlink'):
        engines.external_source_inputs(root)


def test_maintained_external_compile_literals_are_declared():
    import re
    declared = {path.resolve() for _, path in engines.external_source_inputs(ROOT)}
    for location, _relative, paths, _inherits in engines.source_layout(ROOT):
        for source in paths:
            if source.suffix != '.rs':
                continue
            for literal in re.findall(r'include_(?:str|bytes)!\(\s*"([^"\n]+)"\s*\)', source.read_text()):
                included = (source.parent / literal).resolve()
                if not included.is_relative_to(location.resolve()):
                    assert included in declared, f'Undeclared external compile input: {source}: {literal}'


def test_external_snapshot_refuses_unowned_target(tmp_path):
    root = tmp_path / 'application'
    crate = root / 'packages/tofu-db'
    crate.mkdir(parents=True)
    (root / 'VERSION').write_text('source')
    (crate / 'Cargo.toml').write_text('[package]\nname="tofu-db"\n[package.metadata.tofu]\nexternal-inputs=["VERSION"]\n')
    snapshot = tmp_path / 'owned/source/tofu-db'
    snapshot.parent.parent.mkdir(parents=True)
    target = snapshot.parent.parent / 'VERSION'
    target.write_text('unowned')
    with pytest.raises(engines.EnginePreparationError, match='not owned'):
        engines._sync_build_snapshot(root, crate, snapshot)
    assert target.read_text() == 'unowned'
