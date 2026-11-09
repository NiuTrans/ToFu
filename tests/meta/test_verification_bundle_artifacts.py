"""Real bundler receipts retain side artifacts and location-sensitive inputs."""
import json
from pathlib import Path
import subprocess

import pytest

from benchmarks.verification_bundle_reuse import artifact_digest
from tests import _runtime_sections as runtime
from tests._jsdom import require_node_modules

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('kind', ['portable', 'location-sensitive', 'public-copy'])
def test_real_vite_receipts_cover_emitted_files_and_refuse_unobserved_copies(tmp_path, kind):
    require_node_modules('vite')
    (tmp_path / 'package.json').write_text('{"name":"bundle-artifact-fixture"}')
    source = tmp_path / 'owner.ts'
    source.write_text('import "./style.css";\nexport const answer: number = 42;\n'
                      + ('export const origin = () => import.meta.url;\n'
                         if kind == 'location-sensitive' else ''))
    (tmp_path / 'style.css').write_text('.fixture { color: red; }\n')
    if kind == 'public-copy':
        (tmp_path / 'public').mkdir()
        (tmp_path / 'public/extra.txt').write_text('copied outside Rollup')
    output = tmp_path / 'output/owner.js'
    manifest = tmp_path / 'receipt.json'
    result = subprocess.run(
        [str(ROOT / 'scripts/vite_test_bundle.mjs'), str(source), '--format=cjs',
         f'--outfile={output}', f'--inputs-manifest={manifest}'],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(manifest.read_text())
    assert receipt['format'] == 'tofu.test-bundle/v1'
    assert {str(source), str(tmp_path / 'style.css')} <= set(receipt['inputs'])
    assert str(output) in receipt['outputs']
    assert any(name.endswith('.css') for name in receipt['outputs'])
    assert receipt['complete'] is (kind != 'public-copy')
    assert receipt['portable'] is (kind != 'location-sensitive')
    if receipt['complete']:
        assert set(receipt['outputs']) == {str(path) for path in output.parent.rglob('*') if path.is_file()}
    checked = subprocess.run(['node', '-e',
        'if (require(process.argv[1]).answer !== 42) process.exit(1)', str(output)],
        capture_output=True, text=True, timeout=60)
    assert checked.returncode == 0, checked.stderr


def test_measurement_cannot_qualify_javascript_without_its_styles(tmp_path):
    (tmp_path / 'owner.js').write_text('exports.answer = 42;')
    with pytest.raises(ValueError, match='complete JavaScript and CSS'):
        artifact_digest(tmp_path)
    css = tmp_path / 'style.css'
    css.write_text('.fixture { color: red; }')
    initial, _ = artifact_digest(tmp_path)
    css.write_text('.fixture { color: blue; }')
    assert artifact_digest(tmp_path)[0] != initial


@pytest.mark.parametrize('entrypoint', ['native-module', 'import-port'])
def test_real_owner_reuses_executable_artifacts_across_checkouts(tmp_path, monkeypatch, entrypoint):
    require_node_modules('vite', 'jsdom')
    source_name = 'frontend/src/html-safety.ts'
    monkeypatch.setenv('TOFU_TEST_BUNDLE_CACHE', str(tmp_path / 'cache'))
    monkeypatch.setattr(runtime, '_CACHE', {})
    monkeypatch.setattr(runtime, '_input_digest_memo', {})
    monkeypatch.setattr(runtime, '_frontend_source_listing', lambda: (source_name + '\0').encode())
    # The test owns every source and does not change it during a build.
    monkeypatch.setattr(runtime, '_bundle_clock_ns', lambda: runtime.time.time_ns() + 10_000_000_000)
    original_run = subprocess.run
    builds = []

    def observed_run(argv, **kwargs):
        if str(argv[0]).endswith('/scripts/vite_test_bundle.mjs'):
            builds.append(argv)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, 'run', observed_run)
    artifacts = []
    for index in range(2):
        root = tmp_path / f'checkout-{index}'
        for name in ('tests/_runtime_sections.py', 'scripts/vite_test_bundle.mjs',
                     'package.json', 'package-lock.json', source_name):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
            target.chmod((ROOT / name).stat().st_mode)
        (root / 'node_modules').symlink_to((ROOT / 'node_modules').resolve())
        monkeypatch.setattr(runtime, 'ROOT', root)
        monkeypatch.setattr(runtime, '__file__', str(root / 'tests/_runtime_sections.py'))
        monkeypatch.setattr(runtime, '_DIRECTORY', root / 'private-view')
        output = (runtime.native_module_path('owner.js', root / source_name)
                  if entrypoint == 'native-module' else runtime._import_port_path(root / source_name))
        checked = subprocess.run(['node', '-e',
            'require(process.argv[1]); if (escapeHtml("<>&") !== "&lt;&gt;&amp;") process.exit(1)', output],
            capture_output=True, text=True, timeout=60)
        assert checked.returncode == 0, checked.stderr
        artifacts.append(Path(output).read_bytes())
    assert artifacts[0] == artifacts[1]
    assert len(builds) == 1


@pytest.mark.parametrize('entrypoint', ['feature-module', 'feature-graph'])
def test_feature_entries_share_exact_artifacts_and_rebuild_changed_owners(tmp_path, monkeypatch, entrypoint):
    require_node_modules('vite', 'jsdom')
    source_name = 'frontend/src/features/orchestration/selection-focus.ts'
    registry_name = 'frontend/src/features/orchestration/registry.ts'
    monkeypatch.setenv('TOFU_TEST_BUNDLE_CACHE', str(tmp_path / 'cache'))
    monkeypatch.setattr(runtime, '_CACHE', {})
    monkeypatch.setattr(runtime, '_input_digest_memo', {})
    monkeypatch.setattr(runtime, '_frontend_source_listing',
                        lambda: (source_name + '\0' + registry_name + '\0').encode())
    monkeypatch.setattr(runtime, '_bundle_clock_ns', lambda: runtime.time.time_ns() + 10_000_000_000)
    original_run = subprocess.run
    builds, artifacts = [], []

    def observed_run(argv, **kwargs):
        if str(argv[0]).endswith('/scripts/vite_test_bundle.mjs'):
            builds.append(argv)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, 'run', observed_run)
    for index in range(3):
        root = tmp_path / f'checkout-{index}'
        for name in ('tests/_runtime_sections.py', 'scripts/vite_test_bundle.mjs',
                     'package.json', 'package-lock.json', source_name, registry_name):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
            target.chmod((ROOT / name).stat().st_mode)
        if index == 2:
            source = root / source_name
            source.write_text(source.read_text().replace('return true;', 'return false;'))
        (root / 'node_modules').symlink_to((ROOT / 'node_modules').resolve())
        monkeypatch.setattr(runtime, 'ROOT', root)
        monkeypatch.setattr(runtime, '__file__', str(root / 'tests/_runtime_sections.py'))
        monkeypatch.setattr(runtime, '_DIRECTORY', root / 'private-view')
        if entrypoint == 'feature-module':
            output = runtime.native_module_path('focus.js', root / source_name)
        else:
            output = runtime.native_module_graph([
                ('focus.js', root / source_name), ('registry.js', root / registry_name)])
        checked = subprocess.run(['node', '-e',
            'require(process.argv[1]); let calls = 0; '
            'const doc = {getElementById: () => ({focus: () => calls++})}; '
            'const result = createOrchestrationSelectionFocus({document: doc}).focus(); '
            'if (result !== (process.argv[2] === "true") || calls !== 1 '
            '|| orchestrationRegistry.createOrchestrationSelectionFocus '
            '!== createOrchestrationSelectionFocus) process.exit(1)', output,
            'true' if index < 2 else 'false'], capture_output=True, text=True, timeout=60)
        assert checked.returncode == 0, checked.stderr
        artifacts.append(Path(output).read_bytes())
        assert len(builds) == (1 if index < 2 else 2)
    assert artifacts[0] == artifacts[1]
    assert artifacts[2] != artifacts[1]
