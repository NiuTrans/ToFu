"""Build the real extension package inside an isolated, caller-owned checkout."""
from pathlib import Path
import shutil
import subprocess


def build_extension_bundle(root: Path, destination: Path, *, store: bool) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root / 'browser_extension', destination / 'browser_extension')
    (destination / 'scripts').mkdir()
    shutil.copy2(root / 'scripts/package_extension.sh', destination / 'scripts/package_extension.sh')
    manifests = destination / 'docs/chrome-web-store'
    manifests.mkdir(parents=True)
    shutil.copy2(root / 'docs/chrome-web-store/manifest.store.json', manifests / 'manifest.store.json')
    subprocess.run(['bash', str(destination / 'scripts/package_extension.sh'),
                    *(['--store'] if store else [])],
                   cwd=destination, check=True, capture_output=True, text=True, timeout=30)
    archives = list((destination / 'dist').glob('*.zip'))
    if len(archives) != 1:
        raise AssertionError(f'Expected one extension archive, found {len(archives)}')
    return archives[0]
