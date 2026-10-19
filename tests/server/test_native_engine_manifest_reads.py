"""Source identity parses each manifest once per operation with no stale cache."""
from collections import Counter
from pathlib import Path
import subprocess

import pytest

from serverctl_pkg import native_engine as engines

pytestmark = pytest.mark.unit


@pytest.fixture
def source_tree(tmp_path):
    for name in ('tofu-db', 'dependency', 'leaf'):
        crate = tmp_path / 'packages' / name
        (crate / 'src').mkdir(parents=True)
        (crate / 'src/lib.rs').write_text('pub fn fixture() {}\n')
        (crate / 'Cargo.toml').write_text(
            f'[package]\nname = "{name}"\nversion = "0.0.0"\nedition = "2021"\n')
    root = tmp_path / 'packages/tofu-db/Cargo.toml'
    root.write_text(root.read_text() + '''
[dependencies]
dependency = { path = "../dependency" }
[dev-dependencies]
leaf = { path = "../leaf" }
[target.'cfg(unix)'.build-dependencies]
dependency = { path = "../dependency" }
[package.metadata.tofu]
external-inputs = ["contracts/fixture.json"]
''')
    dependency = tmp_path / 'packages/dependency/Cargo.toml'
    dependency.write_text(dependency.read_text() + '\n[dependencies]\nleaf = { path = "../leaf" }\n')
    (tmp_path / 'contracts').mkdir()
    (tmp_path / 'contracts/fixture.json').write_text('{"fixture":true}\n')
    return tmp_path


def observed_reader(monkeypatch):
    original = engines._read_manifest
    calls = []

    def read(path):
        calls.append(Path(path).resolve())
        return original(path)

    monkeypatch.setattr(engines, '_read_manifest', read)
    return calls


@pytest.mark.parametrize('module', ['sitecustomize', 'json'])
@pytest.mark.parametrize('entry', ['pythonpath', 'working_directory'])
def test_manifest_child_does_not_execute_ambient_python_code(
        source_tree, tmp_path, monkeypatch, module, entry):
    ambient = tmp_path / 'ambient-python'
    ambient.mkdir()
    marker = tmp_path / 'ambient-code-executed'
    (ambient / (module + '.py')).write_text(
        f"open({str(marker)!r}, 'w').write('ambient startup code ran')\n"
        "raise RuntimeError('ambient code must not own native source identity')\n")
    if entry == 'pythonpath':
        monkeypatch.setenv('PYTHONPATH', str(ambient))
    else:
        monkeypatch.chdir(ambient)
    manifest = source_tree / 'packages/tofu-db/Cargo.toml'

    result = engines._read_manifest(manifest)

    assert result['package'] == 'tofu-db'
    assert result['external_inputs'] == ['contracts/fixture.json']
    assert not marker.exists()


def test_fingerprint_reuses_successful_parses_only_within_one_operation(source_tree, monkeypatch):
    calls = observed_reader(monkeypatch)
    first = engines.source_fingerprint(source_tree)
    expected = {path.resolve() for path in (source_tree / 'packages').glob('*/Cargo.toml')}
    assert Counter(calls) == Counter({path: 1 for path in expected})
    calls.clear()
    assert engines.source_fingerprint(source_tree) == first
    assert Counter(calls) == Counter({path: 1 for path in expected})
    leaf = source_tree / 'packages/leaf/Cargo.toml'
    leaf.write_text(leaf.read_text().replace('0.0.0', '0.0.1'))
    assert engines.source_fingerprint(source_tree) != first


def test_standalone_layout_parses_each_manifest_once(source_tree, monkeypatch):
    calls = observed_reader(monkeypatch)
    layout = engines.source_layout(source_tree)
    assert len(layout) == 3
    assert Counter(calls) == Counter({location / 'Cargo.toml': 1 for location, _, _, _ in layout})
    root = source_tree / 'packages/tofu-db/Cargo.toml'
    root.write_text(root.read_text().replace('contracts/fixture.json', 'contracts/new.json'))
    (source_tree / 'contracts/new.json').write_text('{}')
    assert engines.external_source_inputs(source_tree) == [('contracts/new.json', source_tree / 'contracts/new.json')]


def test_failed_parse_does_not_poison_next_fingerprint(source_tree):
    first = engines.source_fingerprint(source_tree)
    leaf = source_tree / 'packages/leaf/Cargo.toml'
    original = leaf.read_text()
    leaf.write_text('invalid TOML')
    with pytest.raises(engines.EnginePreparationError, match='source manifest'):
        engines.source_fingerprint(source_tree)
    leaf.write_text(original)
    assert engines.source_fingerprint(source_tree) == first


@pytest.mark.parametrize('failure', [
    subprocess.TimeoutExpired('manifest fixture', 10),
    subprocess.CalledProcessError(1, 'manifest fixture'),
])
def test_parse_subprocess_keeps_timeout_failure_and_join_boundary(source_tree, monkeypatch, failure):
    commands = []

    def fail(command, **options):
        commands.append((command, options))
        raise failure

    monkeypatch.setattr(engines.subprocess, 'run', fail)
    with pytest.raises(engines.EnginePreparationError) as caught:
        engines.source_fingerprint(source_tree)
    assert caught.value.__cause__ is failure
    assert len(commands) == 1
    command, options = commands[0]
    assert '-c' in command and Path(command[-1]).name == 'Cargo.toml'
    assert options['timeout'] == 10 and options['check'] and options['capture_output']


def test_changed_external_declaration_and_new_dependency_are_not_cached(source_tree):
    before = engines.source_fingerprint(source_tree)
    root = source_tree / 'packages/tofu-db/Cargo.toml'
    root.write_text(root.read_text().replace('contracts/fixture.json', '../escape.json'))
    with pytest.raises(engines.EnginePreparationError, match='escapes'):
        engines.source_fingerprint(source_tree)
    root.write_text(root.read_text().replace('../escape.json', 'contracts/fixture.json'))
    assert engines.source_fingerprint(source_tree) == before
    root.write_text(root.read_text() + '\n[build-dependencies]\nmissing = { path = "../missing" }\n')
    with pytest.raises(engines.EnginePreparationError, match='no manifest'):
        engines.source_fingerprint(source_tree)
