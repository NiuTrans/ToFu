#!/usr/bin/env python3
"""Build a verified release wheelhouse for the current native Python platform.

Entrypoint: CLI --root --output. Uses the installer's artifact authority and
committed uv.lock constraints; outputs archive + SHA-256, never application state.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bootstrap_pkg.install_artifacts import prepare_python_wheels, prepare_search_wheel
from bootstrap_pkg import python_wheels


def build(root: Path, output: Path) -> Path:
    root = root.resolve()
    search = prepare_search_wheel(root, sys.executable)
    with tempfile.TemporaryDirectory(prefix='.release-wheels-', dir=root) as temporary:
        stage = Path(temporary)
        requirements = stage / 'requirements.txt'
        source = (root / 'requirements.txt').read_text()
        source = re.sub(r'^\s*tofu-search[<>=].*$', '', source, flags=re.M)
        requirements.write_text(source + '\n' + str(search) + '\n')
        # No network re-resolution: published dependency versions come from uv.lock.
        subprocess.run(['uv', 'export', '--frozen', '--no-dev', '--no-emit-local',
                        '--extra', 'app', '--extra', 'worker', '--no-hashes',
                        '--output-file', str(stage / 'install-constraints.txt')],
                       cwd=root, check=True, timeout=60, stdout=sys.stderr)
        old = os.environ.get('TOFU_PYTHON_BUNDLE_NO_DOWNLOAD')
        os.environ['TOFU_PYTHON_BUNDLE_NO_DOWNLOAD'] = '1'
        try:
            house = stage / 'wheelhouse'
            prepare_python_wheels(sys.executable, requirements, house)
            # Certify the shipped artifacts in an EMPTY interpreter environment.
            # A builder's preinstalled packages cannot hide an omitted dependency.
            candidate = stage / 'verify-env'
            subprocess.run(['uv', 'venv', '--python', sys.executable, str(candidate)],
                           check=True, timeout=60, stdout=sys.stderr)
            interpreter = candidate / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            subprocess.run(['uv', 'pip', 'install', '--python', str(interpreter),
                            '--no-index', '--find-links', str(house), '--no-deps',
                            '--require-hashes', '-r', str(house / 'install.lock')],
                           check=True, timeout=300, stdout=sys.stderr)
            subprocess.run([str(interpreter), '-c',
                            'import quart, hypercorn, lxml.etree, fitz, PIL, '
                            'cryptography, orjson, playwright, tofu_search'],
                           check=True, timeout=60)
            subprocess.run([str(interpreter), str(root / 'scripts/verify_pdf_stack.py')],
                           check=True, timeout=60)
            subprocess.run([str(interpreter), '-m', 'bootstrap_pkg.runtime_checks'],
                           cwd=root, check=True, timeout=60)
            return python_wheels.package(house, requirements,
                                         python_wheels.identity(sys.executable),
                                         (root / 'VERSION').read_text().strip(), output)
        finally:
            if old is None:
                os.environ.pop('TOFU_PYTHON_BUNDLE_NO_DOWNLOAD', None)
            else:
                os.environ['TOFU_PYTHON_BUNDLE_NO_DOWNLOAD'] = old


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', required=True, type=Path)
    arguments = parser.parse_args()
    print(build(arguments.root, arguments.output.resolve()))


if __name__ == '__main__':
    main()
