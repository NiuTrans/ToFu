"""Build an isolated ESM feature owner against an injected test service port."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'frontend/src/feature-registry.ts'


def compile_feature_owner(
    esbuild: str | Path,
    source: str | Path,
    output: str | Path,
    workdir: str | Path,
) -> subprocess.CompletedProcess[str]:
    """Compile one owner without reintroducing its former window API.

    Production injects the private service table from ``main.ts``. The harness
    uses its jsdom window as that table so existing behavioral fixtures can
    supply dependencies and inspect registered owners without changing the
    browser-facing application contract.
    """
    workdir = Path(workdir)
    entry = workdir / f'{Path(source).stem}-owner-harness.ts'
    entry.write_text(
        'import { connectFeatureRuntime, featureRegistry } from '
        + json.dumps(REGISTRY.as_posix()) + ';\n'
        + 'import ' + json.dumps(Path(source).resolve().as_posix()) + ';\n'
        + 'const services = window as Window & Record<string, unknown>;\n'
        + 'connectFeatureRuntime('
        + '(name) => services[name], '
        + '(name, value) => { services[name] = value; });\n'
        + 'services.__setFeatureService = '
        + '(name: string, value: unknown) => { featureRegistry[name] = value; };\n',
        encoding='utf-8',
    )
    return subprocess.run(
        [str(esbuild), str(entry), '--bundle', '--format=iife',
         '--platform=browser', f'--outfile={output}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
