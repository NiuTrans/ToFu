"""Native additive labels and Python truth fixtures cannot silently drift."""
import json
from pathlib import Path
import subprocess
import sys
import pytest

pytestmark=pytest.mark.unit
ROOT=Path(__file__).resolve().parents[2]


def test_additive_labels_and_oracle_match_current_python_authority():
    subprocess.run([sys.executable,str(ROOT/'scripts/gen_native_usage_aggregate.py'),'--check'],check=True)
    fixture=json.loads((ROOT/'packages/tofu-runtime/tests/fixtures/python_usage_aggregate.json').read_text())
    assert fixture['format']=='tofu.python-additive-usage-oracle/v1'
    assert len(fixture['cases'])>=100
