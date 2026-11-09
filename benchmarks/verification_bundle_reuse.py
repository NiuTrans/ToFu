"""Measure real Vite artifact reuse across identical isolated checkouts.

Each process owns its cache and complete synthetic sources. Only implementation
files come from --implementation-root; installed dependencies come from this
checkout. All JavaScript and CSS bytes must agree before clocks count. This is
test preparation, not application or whole-task latency.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
IMPLEMENTATION_FILES = ('tests/_runtime_sections.py', 'scripts/vite_test_bundle.mjs')


def artifact_digest(directory: Path) -> tuple[str, list[str]]:
    files = sorted(path for path in directory.rglob('*') if path.is_file())
    names = [path.relative_to(directory).as_posix() for path in files]
    if 'owner.js' not in names or not any(name.endswith('.css') for name in names):
        raise ValueError('a complete JavaScript and CSS output is required')
    digest = hashlib.sha256()
    for path, name in zip(files, names):
        blob = path.read_bytes()
        digest.update(json.dumps([name, len(blob)]).encode() + b'\0' + blob)
    return digest.hexdigest(), names


def measure(implementation: Path, checkouts: int, collectors: int = 1) -> dict:
    if checkouts < 2:
        raise ValueError('cross-checkout measurement requires at least two checkouts')
    if collectors < 1:
        raise ValueError('each checkout requires a collector')
    frozen = {name: (implementation / name).read_bytes() for name in IMPLEMENTATION_FILES}
    with tempfile.TemporaryDirectory(prefix='tofu-bundle-reuse-') as temporary:
        parent = Path(temporary)
        roots = [parent / f'lane-{index}' for index in range(checkouts)]
        for root in roots:
            for name in IMPLEMENTATION_FILES:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(frozen[name])
                target.chmod((implementation / name).stat().st_mode)
            shutil.copy2(ROOT / 'package-lock.json', root / 'package-lock.json')
            (root / 'package.json').write_text('{"name":"verification-reuse-fixture"}\n')
            (root / 'node_modules').symlink_to((ROOT / 'node_modules').resolve())
            source = root / 'frontend/src'
            source.mkdir(parents=True)
            (source / 'owner.ts').write_text(
                'import "./style.css";\n'
                'export function total(values: number[]): number {\n'
                '  return values.reduce((sum, value) => sum + value, 0);\n}\n')
            (source / 'style.css').write_text('.example { color: rgb(12, 34, 56); }\n')
            subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)

        modules = []
        for index, root in enumerate(roots):
            group = []
            for collector in range(collectors):
                spec = importlib.util.spec_from_file_location(
                    f'bundle_probe_{index}_{collector}', root / IMPLEMENTATION_FILES[0])
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                group.append(module)
            modules.append(group)
        # Honor the implementation's coarse-filesystem correctness window;
        # fixture creation and this wait are outside the measured operation.
        latest = max(path.stat().st_ctime_ns for root in roots for path in root.rglob('*')
                     if path.is_file() and not path.is_symlink())
        window = max(module._BUNDLE_CACHE_RACY_WINDOW_NS for group in modules for module in group)
        remaining = (latest + window + 1 - time.time_ns()) / 1_000_000_000
        if remaining > 0:
            time.sleep(remaining)
        previous = os.environ.get('TOFU_TEST_BUNDLE_CACHE')
        os.environ['TOFU_TEST_BUNDLE_CACHE'] = str(parent / 'cache')
        calls = []
        original_run = subprocess.run

        def observed_run(argv, *args, **kwargs):
            if str(argv[0]).endswith('/scripts/vite_test_bundle.mjs'):
                calls.append(str(argv[0]))
            return original_run(argv, *args, **kwargs)

        subprocess.run = observed_run
        rows = []
        seconds = 0.0

        def compile_module(module, root):
            output = module._DIRECTORY / 'artifact' / 'owner.js'
            started = time.perf_counter()
            module._run_test_bundler(
                [str(root / 'frontend/src/owner.ts'), '--format=cjs', f'--outfile={output}'],
                [output], timeout=60, failure='measurement build failed')
            return output, time.perf_counter() - started

        try:
            for root, group in zip(roots, modules):
                started = time.perf_counter()
                with ThreadPoolExecutor(max_workers=len(group)) as executor:
                    pending = [executor.submit(compile_module, module, root) for module in group]
                    built = [future.result() for future in pending]
                seconds += time.perf_counter() - started
                for output, elapsed in built:
                    digest, artifacts = artifact_digest(output.parent)
                    original_run(['node', '-e',
                        'if (require(process.argv[1]).total([2, 3, 5]) !== 10) process.exit(1)', str(output)],
                        check=True, capture_output=True)
                    rows.append({'seconds': elapsed, 'sha256': digest, 'artifacts': artifacts})
        finally:
            subprocess.run = original_run
            if previous is None:
                os.environ.pop('TOFU_TEST_BUNDLE_CACHE', None)
            else:
                os.environ['TOFU_TEST_BUNDLE_CACHE'] = previous
            for group in modules:
                for module in group:
                    module._cleanup()
        if len({row['sha256'] for row in rows}) != 1:
            raise ValueError('checkouts emitted different artifacts')
        if any((implementation / name).read_bytes() != blob for name, blob in frozen.items()):
            raise ValueError('implementation changed during measurement')
        cached = list((parent / 'cache').rglob('*'))
        files = [path for path in cached if path.is_file()]
        return {'format': 'tofu.verification-bundle-reuse/v1', 'checkouts': checkouts,
                'collectors': collectors, 'builds': len(calls), 'seconds': seconds,
                'cache_bytes': sum(path.stat().st_size for path in files),
                'cache_allocated_bytes': sum(path.stat().st_blocks * 512 for path in cached),
                'cache_files': len(files),
                'lease_directories': len(list((parent / 'cache').glob('.lease-*'))),
                'rows': rows, 'implementation': {
                    name: hashlib.sha256(blob).hexdigest() for name, blob in frozen.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--implementation-root', type=Path, default=ROOT)
    parser.add_argument('--checkouts', type=int, default=6)
    parser.add_argument('--collectors', type=int, default=1)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = measure(args.implementation_root.resolve(), args.checkouts, args.collectors)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ('checkouts', 'collectors', 'builds', 'seconds')}), flush=True)


if __name__ == '__main__':
    main()
