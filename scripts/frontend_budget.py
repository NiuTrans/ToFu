#!/usr/bin/env python3
"""Check frontend authoring shape and the complete compressed delivery graph.

Responsibility: enforce existing source, entry, async-module and total-graph
budgets without hiding shared static imports. Immediate startup dynamic roots
come from frontend/authoring-inputs.json. Standalone URL workers are counted
separately under an explicit asset whitelist, never silently omitted.

Source checks cover authored sections; generated delivery files keep their own
generator verification. Budget values below remain the reviewed ceilings;
changing chunk layout cannot bypass them. Ownership and verification rationale:
docs/FRONTEND_ARCHITECTURE.md.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Primary entry identity does not change when Rollup also marks that entry as
# dynamically reachable. These rows have their own reviewed entry budgets.
VITE_PRIMARY_ENTRIES = (
    ('main', 'frontend/src/main.ts'),
    ('admin', 'frontend/src/admin.ts'),
)
VITE_STARTUP_DYNAMIC_ENTRIES = json.loads(
    (ROOT / 'frontend/authoring-inputs.json').read_text(encoding='utf-8')
)['startupDynamicImports']


def _startup_assets(manifest: dict, entry: str) -> set[str]:
    """Count required startup owners and shared static dependencies once."""
    pending = [entry]
    if entry in VITE_STARTUP_DYNAMIC_ENTRIES:
        pending.extend(manifest[entry].get('dynamicImports') or ())
    seen: set[str] = set()
    assets: set[str] = set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        row = manifest[key]
        assets.add(row['file'])
        pending.extend(row.get('imports') or ())
    return assets


def _limit(name: str, default_kib: int) -> int:
    try:
        value = int(os.environ.get(name, '') or default_kib)
    except (TypeError, ValueError):
        value = default_kib
    return max(1, value) * 1024


def _gzip_size(path: Path) -> int:
    return len(gzip.compress(path.read_bytes(), compresslevel=9, mtime=0))


def _vite_graph_and_url_assets(manifest: dict) -> tuple[set[str], set[str]]:
    """Split emitted JS into the module graph and standalone URL assets.

    The graph is what entry/dynamic rows can REACH through
    ``imports``/``dynamicImports`` edges. Vite emits URL-imported workers
    (the pdf.worker, ~405 KiB gzip) as standalone rows with no inbound
    edges: on-demand binaries loaded by their owning library outside the
    module graph — budgeted separately so the graph total measures what it
    claims.
    """
    all_js: dict[str, str] = {}  # file -> first row key (for error context)
    for key, row in manifest.items():
        if not isinstance(row, dict):
            continue
        value = str(row.get('file') or '').replace('\\', '/')
        if not value.endswith(('.js', '.mjs')):
            continue
        if not value.startswith('assets/') or '..' in value.split('/'):
            raise ValueError(f'unsafe Vite asset path: {value!r}')
        all_js.setdefault(value, str(key))

    reachable_keys: set[str] = set()
    stack = [str(key) for key, row in manifest.items()
             if isinstance(row, dict)
             and (row.get('isEntry') or row.get('isDynamicEntry'))]
    while stack:
        key = stack.pop()
        if key in reachable_keys or key not in manifest:
            continue
        reachable_keys.add(key)
        row = manifest[key]
        if not isinstance(row, dict):
            continue
        for field in ('imports', 'dynamicImports'):
            stack.extend(str(value) for value in row.get(field) or ())

    graph_files = {
        str(manifest[key].get('file') or '').replace('\\', '/')
        for key in reachable_keys
        if isinstance(manifest.get(key), dict)
    }
    graph = {path for path in all_js if path in graph_files}
    url_assets = set(all_js) - graph
    return graph, url_assets


# Standalone ``?url`` binaries legitimately emitted OUTSIDE the module graph
# (see _vite_graph_and_url_assets). Membership is explicit on purpose: any
# new graph-unreachable JS chunk must FAIL this gate until a reviewer decides
# it belongs here or back in the graph — otherwise this line silently absorbs
# chunks that drop out of the manifest graph through a new loading mechanism,
# each passing its own per-file budget while the total-graph metric loses
# sight of them. Matched against the emitted file's basename (hashed name).
EXPECTED_URL_ASSET_STEMS = ('pdf.worker.min',)


def _url_asset_whitelist_violations(url_assets: set[str]) -> list[str]:
    """Graph-unreachable JS rows not whitelisted as standalone binaries."""
    return sorted(
        path for path in url_assets
        if not any(stem in Path(path).name for stem in EXPECTED_URL_ASSET_STEMS)
    )


def _check(label: str, size: int, limit: int, failures: list[str]) -> None:
    print(f'{label}: {size / 1024:.1f} KiB gzip '
          f'(budget {limit / 1024:.0f} KiB)')
    if size > limit:
        failures.append(f'{label} is {size - limit} bytes over budget')


def _check_source_shape(failures: list[str]) -> None:
    retained_runtime = ROOT / 'frontend/src/runtime/app-runtime.js'
    retained_sections_root = ROOT / 'frontend/src/runtime/sections'
    application_shell_fragments_root = (
        ROOT / 'frontend/src/application-shell/fragments'
    )
    style_roots = (
        ('application stylesheet', ROOT / 'frontend/src/styles/application',
         'TOFU_BUDGET_RETAINED_STYLES_KIB', 1270),
        ('settings stylesheet', ROOT / 'frontend/src/styles/settings',
         'TOFU_BUDGET_RETAINED_SETTINGS_STYLES_KIB', 142),
    )
    retained_sources = (
        ('HTML application shell', ROOT / 'index.html',
         'TOFU_BUDGET_INDEX_HTML_KIB', 192,
         'extract markup into an explicit owned panel/template'),
    )
    for label, path, environment_name, default_kib, remedy in retained_sources:
        limit = _limit(environment_name, default_kib)
        size = path.stat().st_size
        print(f'{label}: {size / 1024:.1f} KiB '
              f'(budget {limit / 1024:.0f} KiB)')
        if size > limit:
            failures.append(
                f'{label} grew; {remedy} ({size - limit} bytes over)')

    # Shell extraction must reduce the model-facing index rather than provide
    # an unbounded hiding place. Each authored fragment has the same context
    # ceiling as an ordinary source module; the closed marker/file parity gate
    # separately guarantees every fragment is actually served.
    shell_fragment_limit = _limit(
        'TOFU_BUDGET_APPLICATION_SHELL_FRAGMENT_KIB', 100,
    )
    for path in sorted(application_shell_fragments_root.glob('*.html')):
        size = path.stat().st_size
        if size > shell_fragment_limit:
            failures.append(
                f'{path.relative_to(ROOT)} is '
                f'{size - shell_fragment_limit} bytes over the application '
                'shell fragment context budget; split it by UI owner')

    style_section_limit = _limit('TOFU_BUDGET_STYLE_SECTION_KIB', 100)
    for label, root, environment_name, default_kib in style_roots:
        section_paths = sorted(root.glob('*.css'))
        total_size = sum(path.stat().st_size for path in section_paths)
        total_limit = _limit(environment_name, default_kib)
        print(f'{label} authoring sections: {total_size / 1024:.1f} KiB '
              f'(budget {total_limit / 1024:.0f} KiB)')
        if total_size > total_limit:
            failures.append(
                f'{label} sections grew; move rules beside their feature '
                f'owner ({total_size - total_limit} bytes over)')
        for path in section_paths:
            size = path.stat().st_size
            if size > style_section_limit:
                failures.append(
                    f'{path.relative_to(ROOT)} is '
                    f'{size - style_section_limit} bytes over the stylesheet '
                    'section context budget; split it by visual owner')

    # app-runtime.js is generated and deliberately absent from model-facing
    # search. Its ordered section sources are the authoring boundary. Hold both
    # their aggregate and their largest individual context window: a change may
    # move code between sections, but it may not hide new retained-runtime debt.
    retained_section_paths = sorted(retained_sections_root.rglob('*.js'))
    retained_size = sum(path.stat().st_size for path in retained_section_paths)
    # Typed Markdown ownership removes 24,974 retained bytes; ratchet 24 KiB.
    retained_limit = _limit('TOFU_BUDGET_RETAINED_RUNTIME_KIB', 3147)
    print('retained runtime authoring sections: '
          f'{retained_size / 1024:.1f} KiB '
          f'(budget {retained_limit / 1024:.0f} KiB)')
    if retained_size > retained_limit:
        failures.append(
            'retained runtime sections grew; move an owner into a normal '
            f'TypeScript module ({retained_size - retained_limit} bytes over)')

    section_limit = _limit('TOFU_BUDGET_RUNTIME_SECTION_KIB', 239)
    for path in retained_section_paths:
        size = path.stat().st_size
        if size > section_limit:
            failures.append(
                f'{path.relative_to(ROOT)} is {size - section_limit} bytes '
                'over the retained-section context budget; split the logical '
                'owner or migrate it to TypeScript')

    module_limit = _limit('TOFU_BUDGET_VITE_SOURCE_MODULE_KIB', 100)
    oversized: list[tuple[Path, int]] = []
    source_root = ROOT / 'frontend/src'
    for path in source_root.rglob('*'):
        if path.suffix not in {'.ts', '.js'} or not path.is_file():
            continue
        if path == retained_runtime or retained_sections_root in path.parents \
                or '.generated.' in path.name \
                or path.name.endswith('.d.ts'):
            continue
        size = path.stat().st_size
        if size > module_limit:
            oversized.append((path.relative_to(ROOT), size))
    print('largest ordinary JS/TS source-module budget: '
          f'{module_limit / 1024:.0f} KiB')
    for path, size in oversized:
        failures.append(
            f'{path} is {size - module_limit} bytes over the ordinary '
            'source-module budget; split it by responsibility')


def main() -> int:
    from lib.vite_assets import ViteAssetError, validate_vite_artifact

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--source-only', action='store_true',
        help='check model-readable source shape without requiring built assets',
    )
    args = parser.parse_args()
    failures: list[str] = []
    _check_source_shape(failures)
    if args.source_only:
        if failures:
            print('frontend-source-budget: FAILED', file=sys.stderr)
            for failure in failures:
                print(f'  - {failure}', file=sys.stderr)
            return 1
        print('frontend-source-budget: OK')
        return 0

    try:
        manifest = validate_vite_artifact()
        vite_paths, url_asset_paths = _vite_graph_and_url_assets(manifest)
        vite_sizes = {
            path: _gzip_size(ROOT / 'static' / 'vite' / path)
            for path in vite_paths
        }
    except (OSError, KeyError, TypeError, ValueError, ViteAssetError,
            json.JSONDecodeError) as exc:
        print(f'frontend-budget: invalid Vite manifest: {exc}', file=sys.stderr)
        return 1

    for path in _url_asset_whitelist_violations(url_asset_paths):
        failures.append(
            f'Vite URL asset {path} is a graph-unreachable JS chunk outside '
            'the explicit standalone-binary whitelist; wire it into the '
            'module graph or extend EXPECTED_URL_ASSET_STEMS deliberately')
    for path in sorted(url_asset_paths):
        _check(f'Vite URL asset {path}',
               _gzip_size(ROOT / 'static' / 'vite' / path),
               _limit('TOFU_BUDGET_VITE_URL_ASSET_KIB', 410), failures)

    for entry_name, entry_key in VITE_PRIMARY_ENTRIES:
        _check(f'Vite {entry_name} startup',
               sum(vite_sizes[path] for path in _startup_assets(manifest, entry_key)),
               _limit(f'TOFU_BUDGET_VITE_{entry_name.upper()}_KIB',
                      561 if entry_name == 'main' else 16), failures)

    primary_entry_keys = {key for _, key in VITE_PRIMARY_ENTRIES}
    primary_entry_keys.update(
        key for entry in VITE_STARTUP_DYNAMIC_ENTRIES
        for key in manifest[entry].get('dynamicImports') or ())
    for key, row in sorted(manifest.items()):
        if key in primary_entry_keys or not isinstance(row, dict) or not row.get('isDynamicEntry'):
            continue
        chunk_size = vite_sizes[row['file']]
        _check(f'Vite async {key}', chunk_size,
               _limit('TOFU_BUDGET_VITE_CHUNK_KIB', 120), failures)

    # Total means every byte of the module GRAPH, not just named
    # entry/dynamic rows. Rollup creates shared static chunks (for example
    # event-format) that are referenced through ``imports`` and were
    # previously invisible here. Standalone ``?url`` binaries (the
    # pdf.worker) are ratcheted by their own line above, not hidden here.
    _check('total Vite JavaScript', sum(vite_sizes.values()),
           _limit('TOFU_BUDGET_FRONTEND_TOTAL_KIB', 1026), failures)
    if failures:
        print('frontend-budget: FAILED', file=sys.stderr)
        for failure in failures:
            print(f'  - {failure}', file=sys.stderr)
        return 1
    print('frontend-budget: OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
