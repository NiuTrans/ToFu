"""Execute behavior probes against the real browser-extension worker."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess


_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = Path(__file__).with_name('_browser_extension_contract_probe.js')
_BACKGROUND = _ROOT / 'browser_extension' / 'background.js'


def run_extension_probe(mode: str, *, timeout: int = 30) -> dict:
    node = shutil.which('node')
    if not node:
        raise RuntimeError('node is required for browser-extension probes')
    completed = subprocess.run(
        [node, str(_HARNESS), mode, str(_BACKGROUND)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)
