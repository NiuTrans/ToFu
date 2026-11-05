"""Export admitted model artifacts into the independent tofu-search package.

Authority: lib/model_info/local_model_artifacts.json, checked against the
local-artifact schema and the common Model registration boundary before any
output is written. The generated consumer uses only the Python standard
library. This generator does not download weights or change runtime storage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'packages/tofu-search/tofu_search/generated_local_models.py'


def generated_bytes() -> bytes:
    # Direct script execution places scripts/, not the repository, on sys.path.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from lib.model_info.local_artifacts import load_local_model_artifacts

    catalog = load_local_model_artifacts()
    payload = json.dumps(catalog, sort_keys=True, ensure_ascii=True, indent=2) + '\n'
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    return (
        '"""Generated admitted model artifacts; do not edit.\n\n'
        'Source: lib/model_info/local_model_artifacts.json.\n'
        'Generator: scripts/gen_packaged_local_models.py.\n'
        'Reading this manifest grants no invocation or download authority.\n'
        '"""\n'
        'import json\n\n'
        f'CATALOG_SHA256 = {digest!r}\n'
        f'_CATALOG_JSON = {payload!r}\n\n\n'
        'def local_model_artifacts() -> dict:\n'
        '    """Return a detached copy of the build-time admitted artifact catalog."""\n'
        '    return json.loads(_CATALOG_JSON)\n'
    ).encode('utf-8')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    expected = generated_bytes()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != expected:
            raise SystemExit('Regenerate admitted local models: '
                             'python scripts/gen_packaged_local_models.py')
    else:
        OUTPUT.write_bytes(expected)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
