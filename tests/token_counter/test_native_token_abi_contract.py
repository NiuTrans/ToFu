"""Token ABI constants derive from one canonical encoding/status/lifetime contract."""
import json
import subprocess
import sys
from pathlib import Path
import pytest
pytestmark=pytest.mark.unit
ROOT=Path(__file__).resolve().parents[2]
def test_generated_token_abi_matches_canonical_contract():
    subprocess.run([sys.executable,str(ROOT/'scripts/gen_native_token_count_abi.py'),'--check'],check=True)
    contract=json.loads((ROOT/'contracts/native_token_count_abi_v1.json').read_text())
    assert contract['abi_version']==1
    assert len(set(contract['status'].values()))==len(contract['status'])
    assert set(contract['encodings'])=={'CL100K','O200K'}
