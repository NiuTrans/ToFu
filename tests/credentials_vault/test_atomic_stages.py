"""Cross-process uniqueness and cleanup of immutable credential versions."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from lib import credentials_vault as vault

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, '_STORE_PATH', tmp_path / 'vault.json')
    monkeypatch.setattr(vault, '_KEY_PATH', tmp_path / '.key')
    monkeypatch.setattr(vault, '_fernet', None)
    return vault


def test_immutable_create_preserves_existing_secret_without_count_quota(isolated):
    isolated.create_entry('skill.unique', 'first', note='as:0:fixture')
    with pytest.raises(ValueError, match='already exists'):
        isolated.create_entry('skill.unique', 'replacement')
    assert isolated.get_entry('skill.unique') == 'first'
    for index in range(129):
        isolated.create_entry(f'skill.other{index}', 'second')
    assert len(isolated.list_entries()) == 130
    assert isolated.get_entry('skill.other128') == 'second'
    assert os.stat(isolated._STORE_PATH).st_mode & 0o777 == 0o600


def test_simultaneous_processes_preserve_every_unrelated_entry(isolated):
    script = '''
from pathlib import Path
import sys
from lib import credentials_vault as vault
vault._STORE_PATH=Path(sys.argv[1]);vault._KEY_PATH=Path(sys.argv[2]);vault._fernet=None
prefix=sys.argv[3]
for index in range(34):
    vault.set_entry(prefix+str(index),'private-fixture-value')
    vault.create_entry('skill.'+prefix+str(index),'private-staged-fixture')
    vault.delete_entry(prefix+str(index))
'''
    processes = [subprocess.Popen([sys.executable, '-c', script, str(isolated._STORE_PATH),
                                  str(isolated._KEY_PATH), f'p{index}_'],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for index in range(4)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr.decode()
    assert {row['name'] for row in isolated.list_entries()} == {
        f'skill.p{process}_{index}' for process in range(4) for index in range(34)}
    assert all(isolated.get_entry(row['name']) == 'private-staged-fixture'
               for row in isolated.list_entries())
    assert 'private-staged-fixture' not in isolated._STORE_PATH.read_text()


def test_corrupt_store_is_not_replaced_by_staging_or_cleanup(isolated):
    isolated._STORE_PATH.write_text('{broken')
    before = isolated._STORE_PATH.read_bytes()
    for operation in [lambda: isolated.create_entry('skill.new', 'secret'),
                      lambda: isolated.set_entry('other', 'secret'),
                      lambda: isolated.delete_entry('skill.new')]:
        with pytest.raises(Exception):
            operation()
        assert isolated._STORE_PATH.read_bytes() == before
