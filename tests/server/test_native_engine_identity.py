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
def test_compiled_identity_matches_python_after_staging_and_transitive_edit(tmp_path, explicit_include):
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

    before = compiled(crate)
    assert before == engines.source_fingerprint(root)
    snapshot = crate / 'target/update-source'
    engines._sync_build_snapshot(root, crate, snapshot)
    assert compiled(snapshot) == before
    # This dependency is optional/inactive, but its retained sources still
    # belong to the engine closure and must trigger the build script on retry.
    leaf_source.write_text('pub fn leaf_changed() {}\n')
    after = compiled(crate)
    assert after != before
    assert after == engines.source_fingerprint(root)
    engines._sync_build_snapshot(root, crate, snapshot)
    assert compiled(snapshot) == after
    # Source configuration contributes too; documentation and target caches do not.
    (crate / '.cargo/config.toml').write_text('[build]\nincremental = true\n')
    assert compiled(crate) == engines.source_fingerprint(root) != after
    (vendor / 'README.md').write_text('not a compiled source input')
    assert compiled(crate) == engines.source_fingerprint(root)
