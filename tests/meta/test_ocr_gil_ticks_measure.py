"""The OCR tick-gap benchmark runs this tree and reports comparable evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit
_ROOT = Path(os.path.abspath(__file__)).parents[2]


def test_ocr_gil_ticks_measures_this_tree():
    pymupdf = pytest.importorskip('pymupdf', exc_type=ImportError)
    try:
        pymupdf.get_tessdata()
    except Exception as error:  # Hosts without Tesseract data cannot run OCR.
        pytest.skip(f'Tesseract language data unavailable: {error}')
    environment = {name: value for name, value in os.environ.items() if name != 'PYTHONPATH'}
    completed = subprocess.run(
        [sys.executable, str(_ROOT / 'benchmarks' / 'ocr_gil_ticks.py'),
         '--images', '1', '--lines', '1', '--cpu-threads', '0'],
        capture_output=True, text=True, timeout=300, check=True, env=environment)
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report['assetsModule'] == str(_ROOT / 'lib' / 'knowledge' / 'assets.py')
    assert report['images'] == 1 and report['outputChars'][0] > 0
    assert len(report['outputSha256']) == 1 and report['burstTicks'] > 0
    assert report['gapsOver1s'] == 0
