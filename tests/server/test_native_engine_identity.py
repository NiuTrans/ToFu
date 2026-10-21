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


def _rust_literal_census(source):
    """Only exact cfg(test) modules prove exclusion; other cfgs stay audited."""
    import re
    from pygments.lexers import RustLexer
    from pygments.lexer import inherit
    from pygments.token import Comment, Error, String

    class LiteralRustLexer(RustLexer):
        # A field colon enters Pygments' type state even before a raw literal.
        tokens = {'typename': [(r'(?s)b?r(#*)".*?"\1', String, '#pop'), inherit]}

    text = source.read_text()
    # The local lexer (a pytest dependency) needs no downloaded Rust grammar.
    # Keep attributes, but blank strings/comments so their braces/guards cannot
    # hide production code. Offsets still address the original include paths.
    pieces = []
    for _, kind, value in LiteralRustLexer().get_tokens_unprocessed(text):
        assert kind not in Error, f'Cannot census Rust source: {source}'
        opaque = kind in String or (kind in Comment and kind not in Comment.Preproc)
        pieces.append(' ' * len(value) if opaque else value)
    code = ''.join(pieces)
    stack, ends = [], {}
    for brace in re.finditer(r'[{}]', code):
        if brace[0] == '{':
            stack.append(brace.start())
        else:
            assert stack, f'Unbalanced Rust source: {source}'
            ends[stack.pop()] = brace.start()
    assert not stack, f'Unbalanced Rust source: {source}'
    modules = list(re.finditer(
        r'((?:#\[[^\]]*\]\s*)*)(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+(\w+)\s*([;{])', code))
    guarded = lambda item: '#[cfg(test)]' in re.sub(r'\s+', '', item[1])
    ranges = [(item.end() - 1, ends[item.end() - 1])
              for item in modules if item[3] == '{' and guarded(item)]
    in_test = lambda offset: any(start < offset < end for start, end in ranges)
    references, literals = [], []
    for item in modules:
        if item[3] != ';':
            continue
        attributes = text[item.start(1):item.end(1)]
        path = re.search(r'#\[\s*path\s*=\s*"([^"\n]+)"\s*\]', attributes)
        base = source.parent if source.name in ('lib.rs', 'main.rs', 'mod.rs', 'build.rs') else source.with_suffix('')
        targets = [source.parent / path[1]] if path else [base / (item[2] + '.rs'), base / item[2] / 'mod.rs']
        references.extend((target.resolve(), guarded(item) or in_test(item.start())) for target in targets)
    for item in re.finditer(r'\b(include_(?:str|bytes)|include)!\s*\(\s*"([^"\n]+)"\s*\)', text):
        if not code[item.start():].startswith(item[1] + '!'):
            continue
        target = (source.parent / item[2]).resolve()
        if item[1] == 'include':
            references.append((target, in_test(item.start())))
        else:
            literals.append((item[2], in_test(item.start())))
    return references, literals


def _assert_external_compile_literals_declared(root):
    declared = {path.resolve() for _, path in engines.external_source_inputs(root)}
    for location, _relative, paths, _inherits in engines.source_layout(root):
        census = {source.resolve(): _rust_literal_census(source) for source in paths if source.suffix == '.rs'}
        incoming = {}
        for source, (references, _literals) in census.items():
            for target, test_only in references:
                if target in census:
                    incoming.setdefault(target, []).append((source, test_only))
        test_sources = set()
        while True:
            proven = {target for target, references in incoming.items()
                      if target.name not in ('lib.rs', 'main.rs', 'build.rs')
                      and all(guarded or parent in test_sources for parent, guarded in references)}
            if proven <= test_sources:
                break
            test_sources.update(proven)
        for source, (_references, literals) in census.items():
            for literal, test_only in literals:
                included = (source.parent / literal).resolve()
                if source not in test_sources and not test_only and not included.is_relative_to(location.resolve()):
                    assert included in declared, f'Undeclared external compile input: {source}: {literal}'


def test_maintained_external_compile_literals_are_declared():
    _assert_external_compile_literals_declared(ROOT)


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


def _literal_source_tree(root, sources):
    crate = root / 'packages/tofu-db'
    (crate / 'src').mkdir(parents=True)
    (crate / 'Cargo.toml').write_text(
        '[package]\nname="tofu-db"\nversion="0.0.0"\n'
        '[package.metadata.tofu]\nexternal-inputs=["contracts/input.json"]\n')
    contract = root / 'contracts/input.json'
    contract.parent.mkdir()
    contract.write_text('{}')
    fixture = root / 'tests/fixtures/input.json'
    fixture.parent.mkdir(parents=True)
    fixture.write_text('{}')
    for name, text in sources.items():
        (crate / 'src' / name).write_text(text)
    return crate, fixture


_FIXTURE_INCLUDE = 'const VECTOR: &str = include_str!("../../../tests/fixtures/input.json");\n'
_CONTRACT_INCLUDE = 'const CONTRACT: &str = include_str!("../../../contracts/input.json");\n'


@pytest.mark.parametrize('sources', [
    {'lib.rs': '#[cfg(test)] mod vectors {\n' + _FIXTURE_INCLUDE + '}\n' + _CONTRACT_INCLUDE},
    {'lib.rs': '#[cfg(test)] mod vectors {\n'
               'const BRACES: &str = r#"} {"#; /* } */\n'
               'fn raw_field() { let _ = Record { bytes: br#"} {\\n"# }; }\nmod nested {\n'
               + _FIXTURE_INCLUDE + '}\n}\n' + _CONTRACT_INCLUDE},
    {'lib.rs': '#[cfg(test)]\n#[path="vectors.rs"]\nmod vectors;\n' + _CONTRACT_INCLUDE,
     'vectors.rs': _FIXTURE_INCLUDE},
    {'lib.rs': '#[cfg(test)]\n#[path="vectors.rs"]\nmod vectors;\n' + _CONTRACT_INCLUDE,
     'vectors.rs': '#[path="nested.rs"] mod nested;\n', 'nested.rs': _FIXTURE_INCLUDE},
], ids=['inline', 'nested-braces', 'path-module', 'transitive-path-module'])
def test_external_literal_census_respects_test_only_modules(tmp_path, sources):
    crate, fixture = _literal_source_tree(tmp_path, sources)
    _assert_external_compile_literals_declared(tmp_path)
    before = engines.source_fingerprint(tmp_path)
    fixture.write_text('{"changed":true}')
    assert engines.source_fingerprint(tmp_path) == before
    snapshot = tmp_path / 'owned/packages/tofu-db'
    engines._sync_build_snapshot(tmp_path, crate, snapshot)
    assert not (snapshot.parent.parent / 'tests/fixtures/input.json').exists()
    assert (snapshot.parent.parent / 'contracts/input.json').read_text() == '{}'


@pytest.mark.parametrize('sources', [
    {'lib.rs': _FIXTURE_INCLUDE},
    {'lib.rs': '#[cfg(test)] mod vectors {}\n' + _FIXTURE_INCLUDE},
    {'lib.rs': '#[cfg(test)] mod vectors {}\nmod production {\n' + _FIXTURE_INCLUDE + '}\n'},
    {'lib.rs': '#[cfg(any(test, feature="production"))] mod vectors {\n' + _FIXTURE_INCLUDE + '}\n'},
    {'lib.rs': '#[cfg(not(test))] mod vectors {\n' + _FIXTURE_INCLUDE + '}\n'},
    {'lib.rs': 'mod tests {\n' + _FIXTURE_INCLUDE + '}\n'},
    {'lib.rs': '// #[cfg(test)]\nmod vectors {\n' + _FIXTURE_INCLUDE + '}\n'},
    {'lib.rs': 'const NOTE: &str = "#[cfg(test)]";\nmod vectors {\n' + _FIXTURE_INCLUDE + '}\n'},
    {'lib.rs': '#[cfg(test)] #[path="vectors.rs"] mod vectors;\n'
               '#[path="vectors.rs"] mod production;\n', 'vectors.rs': _FIXTURE_INCLUDE},
    {'lib.rs': '#[cfg(test)] mod vectors;\ninclude!("vectors.rs");\n', 'vectors.rs': _FIXTURE_INCLUDE},
], ids=['unguarded', 'after-test-module', 'later-production-module', 'feature-or-test',
        'not-test', 'test-name-only', 'comment-guard', 'string-guard', 'shared-module', 'shared-include'])
def test_external_literal_census_never_waives_production_inputs(tmp_path, sources):
    _literal_source_tree(tmp_path, sources)
    with pytest.raises(AssertionError, match='Undeclared external compile input'):
        _assert_external_compile_literals_declared(tmp_path)
