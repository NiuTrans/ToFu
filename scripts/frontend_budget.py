#!/usr/bin/env python3
"""Frontend source-shape and compressed-resource budgets used by CI.

Defaults are a ratchet just above the measured 2026-08-27 baseline.  Operators
may tighten them through environment variables; increasing a limit is an
explicit reviewable CI change rather than an accidental bundle regression.

2026-08-20 baseline re-measurement (reviewed): 1538.7 KiB total, ratcheted to
1560 KiB.  The +38 KiB growth since 2026-08-14 is deliberate feature delivery
(orchestration studio/task-mode controllers, paper lane, memory panel — all
async chunks loaded on demand), NOT shell weight: the main entry went DOWN
715.0 → 672.5 KiB in the same window after dead-code removal (87 functions),
dead i18n key cleanup (-14.7 KiB), and the highlight.js eager/lazy grammar
split.  First-paint cost is the metric that matters; total counts bytes that
40 on-demand chunks may never ship to a given session.

2026-08-27 delivery re-measurement (reviewed): Project Brain moved from the
retained main runtime into an on-demand chunk, removing
102,852 raw bytes from the main entry. Locale dictionaries now ship as
content-hashed JSON data rather than JavaScript modules: the chosen language
is fetched once and no dictionary is parsed as code. This reduced total Vite
JavaScript from 1,601,399 to 1,430,160 gzip bytes while the main entry measured
589,025 gzip bytes. Project Brain content translation also moved from retained
JavaScript to a typed, explicitly bounded owner; its final lazy chunk is
29,268 gzip bytes. The retained-runtime, main-entry, ordinary async-chunk and
total-JavaScript ceilings were ratcheted to 3,554, 580, 120 and 1,410 KiB.
The 2026-08-28 typed branding decomposition then lowered the retained source
ceiling to 3,431 KiB without increasing delivery budgets; removing the
duplicate retained HTTP-result projector tightened it to 3,428 KiB, and the
typed bounded-work pool lowered it to 3,425 KiB. Removing the cookie-capture
adapter tightened the ceiling to 3,424 KiB; moving the SSE reader into the
lazy typed Paper graph tightened it to 3,420 KiB, and replacing the retained
translation guard with a bounded typed registry tightened it to 3,416 KiB.
Moving conversation catalog queries behind explicit typed inputs tightened the
retained ceiling to 3,415 KiB. Separating Marked policy from dead cache-console
helpers tightened it again to 3,413 KiB. Retiring the unreferenced message-era
translation presentation model tightened it to 3,410 KiB. Moving My Context
preference actions behind an injected typed controller tightened it to
3,409 KiB, and moving shared image actions behind a lifecycle-owned typed DOM
controller tightened it to 3,407 KiB. Moving send-preparation transient state
into an injected conversation controller tightened it to 3,405 KiB.
The subsequent retained-owner pass reached 3,350 KiB; moving My Day's
owner-scoped cache, digest and reminder lifecycle into typed lazy/background
owners tightened the current ceiling to 3,341 KiB. Paper's always-together
typed panel owners now share one composition chunk, reducing total Vite
JavaScript without changing the 1,410 KiB delivery ceiling.
Later retained-owner and availability migrations tightened the source ceiling
to 3,300 KiB. Moving all application dialogs into one lifecycle-owned typed
controller lowers the current ceiling to 3,289 KiB without changing delivery
budgets. Moving Turn provenance HTML and inline-Markdown policy out of the
retained tool renderer lowers the current ceiling to 3,271 KiB; the typed owner
remains in the existing main graph, so delivery ceilings stay unchanged. The
write-gate refusal presenter then lowers the source ceiling to 3,266 KiB.
The bounded tool-result presenter lowers it to 3,240 KiB, and the typed search
presenter plus its composition port tighten the current shared-tree ceiling to
3,224 KiB without changing delivery budgets. Image, browser-JavaScript, and
command-execution presentation owners then lower the retained ceiling to
3,202 KiB. The subsequent bounded write-approval owner lowers it to 3,195 KiB
and joins their explicit static `tool-presentation` chunk; it remains
startup-required and measures 22.8 KiB gzip. The independently cached main
entry measures 559.2 KiB under its 561 KiB ratchet, while the total-JavaScript
graph measures 1,406.8 KiB under its 1,410 KiB ceiling and counts that eager
chunk exactly once.
Moving the four synthetic context-injection lanes behind one bounded typed
presenter then lowers the retained source ceiling to 3,177 KiB. The expanded
eager policy chunk measures 25.2 KiB gzip, while the main entry is 557.1 KiB
and the complete JavaScript graph is 1,407.2 KiB under the unchanged delivery
ceilings.
Moving Human Guidance normalization, cards, outcome rows, and delegated action
strings behind one bounded typed presenter lowers the retained source ceiling
again to 3,171 KiB. Guidance response I/O and translation remain lifecycle
owners rather than presentation dependencies. The isolated gzip-9 graph
measures the eager tool-policy chunk at 26.8 KiB, the independently cacheable
main entry at 556.4 KiB, and total JavaScript at 1,408.0 KiB under the
unchanged delivery ceilings.

The source checks are architecture guardrails for model-only maintenance:
ordinary modules stay small enough to inspect in one bounded read, while the
retained migration runtime has a separate shrinking ratchet. Generated files
are checked by their own generators and are excluded from the module-size cap.
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


def _limit(name: str, default_kib: int) -> int:
    try:
        value = int(os.environ.get(name, '') or default_kib)
    except (TypeError, ValueError):
        value = default_kib
    return max(1, value) * 1024


def _gzip_size(path: Path) -> int:
    return len(gzip.compress(path.read_bytes(), compresslevel=9, mtime=0))


def _vite_javascript_paths(manifest: dict) -> set[str]:
    """Return every unique emitted JS asset, including shared static chunks."""
    paths: set[str] = set()
    for row in manifest.values():
        if not isinstance(row, dict):
            continue
        value = str(row.get('file') or '').replace('\\', '/')
        if not value.endswith(('.js', '.mjs')):
            continue
        if not value.startswith('assets/') or '..' in value.split('/'):
            raise ValueError(f'unsafe Vite asset path: {value!r}')
        paths.add(value)
    return paths


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
    retained_limit = _limit('TOFU_BUDGET_RETAINED_RUNTIME_KIB', 3171)
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
        vite_paths = _vite_javascript_paths(manifest)
        vite_sizes = {
            path: _gzip_size(ROOT / 'static' / 'vite' / path)
            for path in vite_paths
        }
    except (OSError, KeyError, TypeError, ValueError, ViteAssetError,
            json.JSONDecodeError) as exc:
        print(f'frontend-budget: invalid Vite manifest: {exc}', file=sys.stderr)
        return 1

    for entry_name, entry_key in (
            ('main', 'frontend/src/main.ts'), ('admin', 'frontend/src/admin.ts')):
        _check(f'Vite {entry_name} entry', vite_sizes[manifest[entry_key]['file']],
               _limit(f'TOFU_BUDGET_VITE_{entry_name.upper()}_KIB',
                      561 if entry_name == 'main' else 16), failures)

    for key, row in sorted(manifest.items()):
        if not isinstance(row, dict) or not row.get('isDynamicEntry'):
            continue
        chunk_size = vite_sizes[row['file']]
        _check(f'Vite async {key}', chunk_size,
               _limit('TOFU_BUDGET_VITE_CHUNK_KIB', 120), failures)

    # Total means every byte the build emits, not just named entry/dynamic
    # rows. Rollup creates shared static chunks (for example event-format) that
    # are referenced through ``imports`` and were previously invisible here.
    _check('total Vite JavaScript', sum(vite_sizes.values()),
           _limit('TOFU_BUDGET_FRONTEND_TOTAL_KIB', 1410), failures)
    if failures:
        print('frontend-budget: FAILED', file=sys.stderr)
        for failure in failures:
            print(f'  - {failure}', file=sys.stderr)
        return 1
    print('frontend-budget: OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
