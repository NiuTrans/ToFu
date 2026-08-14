"""Resolve migrated vanilla-JS owners from the retained Vite runtime.

Node/jsdom guards should ask for a logical migrated source name instead of
reaching into the deleted ``static/js`` tree.  The helper materializes only
the requested section, preserving the old harness isolation boundary while
keeping ``frontend/src/runtime/app-runtime.js`` as the sole source of truth.
"""

from __future__ import annotations

import atexit
from functools import lru_cache
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'frontend' / 'src' / 'runtime' / 'app-runtime.js'
_DIRECTORY = Path(tempfile.mkdtemp(prefix='tofu-runtime-sections-'))
_LEGACY_ROOT = Path(tempfile.mkdtemp(prefix='tofu-frontend-test-root-'))
_CACHE: dict[str, Path] = {}


@atexit.register
def _cleanup() -> None:
    shutil.rmtree(_DIRECTORY, ignore_errors=True)
    shutil.rmtree(_LEGACY_ROOT, ignore_errors=True)


@lru_cache(maxsize=None)
def runtime_section(name: str, *, scope_prelude: bool = True) -> str:
    source = RUNTIME.read_text(encoding='utf-8')
    marker = f'/* ===== migrated source: {name} ===== */'
    start = source.find(marker)
    if start < 0:
        raise AssertionError(f'migrated runtime section not found: {name}')
    candidates = [source.find('/* ===== migrated source:', start + len(marker))]
    # The final migrated section is followed by the typed runtime shell rather
    # than another source marker.  Keep isolated classic-script harnesses out
    # of that ESM-only epilogue (which contains ``export`` declarations).
    candidates.append(source.find('\nexport async function loadFeatureFlags()',
                                  start + len(marker)))
    ends = [candidate for candidate in candidates if candidate >= 0]
    end = min(ends) if ends else len(source)
    body = source[start:end]
    if scope_prelude:
        body = (
            'var runtimeScope = typeof window !== "undefined" '
            '? window : globalThis;\n' + body)
    return body


def runtime_section_names() -> list[str]:
    source = RUNTIME.read_text(encoding='utf-8')
    return re.findall(r'/\* ===== migrated source: (.+?) ===== \*/', source)


def runtime_section_path(name: str, *, scope_prelude: bool = True) -> str:
    key = f'{name}:{scope_prelude}'
    cached = _CACHE.get(key)
    if cached is not None:
        return str(cached)
    body = runtime_section(name, scope_prelude=scope_prelude)
    if scope_prelude:
        path = _DIRECTORY / name
    else:
        digest = hashlib.sha256(key.encode()).hexdigest()[:12]
        path = _DIRECTORY / '.raw' / f'{Path(name).stem}-{digest}.js'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')
    _CACHE[key] = path
    return str(path)


def runtime_sections_dir() -> str:
    """Materialize a logical-path test view of every migrated JS section."""
    for name in runtime_section_names():
        runtime_section_path(name)
    return str(_DIRECTORY)


def native_module_path(name: str, source: str | Path) -> str:
    """Bundle one migrated TypeScript owner as an isolated classic test view.

    The production graph stays ESM.  Legacy jsdom fixtures can use this
    adapter while they are migrated: named exports are copied onto
    ``globalThis`` so the fixture exercises the native implementation without
    recreating the deleted ``static/js`` source tree in the repository.
    """
    source_path = Path(source)
    if not source_path.is_absolute():
        source_path = ROOT / source_path
    key = f'native:{name}:{source_path}'
    cached = _CACHE.get(key)
    if cached is not None:
        return str(cached)
    esbuild = ROOT / 'node_modules' / '.bin' / 'esbuild'
    if not esbuild.is_file():
        raise AssertionError('esbuild is required to materialize native test modules')
    path = _DIRECTORY / name
    path.parent.mkdir(parents=True, exist_ok=True)
    global_name = 'TofuNativeTest_' + hashlib.sha256(key.encode()).hexdigest()[:12]
    compile_source = source_path
    footer = f'Object.assign(globalThis,{global_name});'
    orchestration_dir = ROOT / 'frontend' / 'src' / 'features' / 'orchestration'
    features_dir = ROOT / 'frontend' / 'src' / 'features'
    if source_path.parent == orchestration_dir:
        entry = _DIRECTORY / '.native-entry' / f'{global_name}.ts'
        entry.parent.mkdir(parents=True, exist_ok=True)
        registry = orchestration_dir / 'registry.ts'
        entry.write_text(
            f'import * as owner from {source_path.as_posix()!r};\n'
            f'import {{ orchestrationRegistry }} from {registry.as_posix()!r};\n'
            'export { owner, orchestrationRegistry };\n',
            encoding='utf-8',
        )
        compile_source = entry
        footer = (
            f'Object.assign(globalThis,{global_name}.owner,'
            f'{global_name}.orchestrationRegistry);')
    elif features_dir in source_path.parents:
        entry = _DIRECTORY / '.native-entry' / f'{global_name}.ts'
        entry.parent.mkdir(parents=True, exist_ok=True)
        registry = ROOT / 'frontend' / 'src' / 'feature-registry.ts'
        entry.write_text(
            f'import * as owner from {source_path.as_posix()!r};\n'
            f'import {{ featureRegistry }} from {registry.as_posix()!r};\n'
            'export { owner, featureRegistry };\n',
            encoding='utf-8',
        )
        compile_source = entry
        footer = (
            f'Object.assign(globalThis,{global_name}.owner,'
            f'{global_name}.featureRegistry);')
    result = subprocess.run(
        [str(esbuild), str(compile_source), '--bundle', '--format=iife',
         '--platform=browser', f'--global-name={global_name}',
         f'--footer:js={footer}',
         f'--outfile={path}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            f'failed to compile native test module {source_path}:\n{result.stderr}')
    _CACHE[key] = path
    return str(path)


def native_module_graph(entries: list[tuple[str, str | Path]]) -> None:
    """Materialize several native owners as one shared classic-script graph.

    Some legacy fixtures still evaluate logical owners one file at a time.  A
    native ESM graph, however, has one module-private compatibility registry.
    Put the bundle at the first logical path and harmless placeholders at the
    remaining paths so those fixtures preserve their evaluation order without
    accidentally creating one registry per owner.
    """
    if not entries:
        return
    resolved = [
        (name, source if Path(source).is_absolute() else ROOT / source)
        for name, source in entries
    ]
    key = 'native-graph:' + '|'.join(
        f'{name}:{Path(source)}' for name, source in resolved)
    if _CACHE.get(key) is not None:
        return
    esbuild = ROOT / 'node_modules' / '.bin' / 'esbuild'
    if not esbuild.is_file():
        raise AssertionError('esbuild is required to materialize native test modules')
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    global_name = f'TofuNativeGraph_{digest}'
    entry = _DIRECTORY / '.native-entry' / f'{global_name}.ts'
    entry.parent.mkdir(parents=True, exist_ok=True)
    orchestration_dir = ROOT / 'frontend' / 'src' / 'features' / 'orchestration'
    imports = [
        f'import * as owner{index} from {Path(source).as_posix()!r};'
        for index, (_, source) in enumerate(resolved)
    ]
    imports.append(
        'import { orchestrationRegistry } from '
        f'{(orchestration_dir / "registry.ts").as_posix()!r};')
    exports = ', '.join(
        [f'owner{index}' for index in range(len(resolved))]
        + ['orchestrationRegistry'])
    entry.write_text(
        '\n'.join(imports) + f'\nexport {{ {exports} }};\n',
        encoding='utf-8',
    )
    first = _DIRECTORY / resolved[0][0]
    first.parent.mkdir(parents=True, exist_ok=True)
    owners = ','.join(
        f'{global_name}.owner{index}' for index in range(len(resolved)))
    footer = (
        f'Object.assign(globalThis,{owners},{global_name}.orchestrationRegistry);'
        f'globalThis.orchestrationRegistry={global_name}.orchestrationRegistry;')
    result = subprocess.run(
        [str(esbuild), str(entry), '--bundle', '--format=iife',
         '--platform=browser', f'--global-name={global_name}',
         f'--footer:js={footer}', f'--outfile={first}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            f'failed to compile native test graph:\n{result.stderr}')
    for name, _ in resolved[1:]:
        placeholder = _DIRECTORY / name
        placeholder.parent.mkdir(parents=True, exist_ok=True)
        placeholder.write_text(
            '/* owner loaded by the shared native test graph */\n',
            encoding='utf-8',
        )
    _CACHE[key] = first


def orchestration_legacy_test_root() -> str:
    """Return a temporary pre-Vite-shaped root backed by current owners.

    This exists solely for older Node fixtures whose harness code still joins
    ``ROOT/static/js``.  Retained sources are materialized from app-runtime;
    native orchestration entries are bundled in one esbuild invocation.  No
    compatibility files are created in the repository or shipped bundle.
    """
    key = 'orchestration-legacy-test-root'
    if _CACHE.get(key) is not None:
        return str(_LEGACY_ROOT)
    runtime_sections_dir()
    esbuild = ROOT / 'node_modules' / '.bin' / 'esbuild'
    if not esbuild.is_file():
        raise AssertionError('esbuild is required to materialize native test modules')
    orchestration_dir = ROOT / 'frontend' / 'src' / 'features' / 'orchestration'
    entry_dir = _DIRECTORY / '.native-batch-entry'
    entry_dir.mkdir(parents=True, exist_ok=True)
    entries: list[Path] = []
    for source in sorted(orchestration_dir.glob('*.ts')):
        if source.name.endswith('.d.ts') or source.name == 'registry.ts':
            continue
        legacy_name = (f'{source.stem}.js'
                       if source.stem == 'task-mode'
                       or source.stem.startswith('task-mode-')
                       else f'orchestration-{source.stem}.js')
        if (_DIRECTORY / legacy_name).is_file():
            continue
        entry = entry_dir / (Path(legacy_name).stem + '.ts')
        entry.write_text(
            f'import * as owner from {source.as_posix()!r};\n'
            'import { orchestrationRegistry } from '
            f'{(orchestration_dir / "registry.ts").as_posix()!r};\n'
            'Object.assign(globalThis, owner, orchestrationRegistry);\n'
            '(globalThis as any).orchestrationRegistry = orchestrationRegistry;\n',
            encoding='utf-8',
        )
        entries.append(entry)
    if entries:
        result = subprocess.run(
            [str(esbuild), *map(str, entries), '--bundle', '--format=iife',
             '--platform=browser', f'--outdir={_DIRECTORY}',
             '--log-level=warning'],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise AssertionError(
                f'failed to compile native orchestration test view:\n'
                f'{result.stderr}')
    static_dir = _LEGACY_ROOT / 'static'
    static_dir.mkdir(parents=True, exist_ok=True)
    js_link = static_dir / 'js'
    if not js_link.exists():
        js_link.symlink_to(_DIRECTORY, target_is_directory=True)
    modules_link = _LEGACY_ROOT / 'node_modules'
    if not modules_link.exists():
        modules_link.symlink_to(ROOT / 'node_modules', target_is_directory=True)
    for name in ('frontend', 'lib', 'scripts', 'templates'):
        source = ROOT / name
        target = _LEGACY_ROOT / name
        if source.exists() and not target.exists():
            target.symlink_to(source, target_is_directory=True)
    index_source = ROOT / 'index.html'
    index_target = _LEGACY_ROOT / 'index.html'
    if index_source.is_file() and not index_target.exists():
        index_target.symlink_to(index_source)
    repository_static = ROOT / 'static'
    if repository_static.is_dir():
        for source in repository_static.iterdir():
            if source.name == 'js':
                continue
            target = static_dir / source.name
            if not target.exists():
                target.symlink_to(source, target_is_directory=source.is_dir())
    _CACHE[key] = _LEGACY_ROOT
    return str(_LEGACY_ROOT)
