"""Unreadable or malformed undo history is not an empty authoritative journal."""
import json
from pathlib import Path

import pytest

from lib.project_mod import modifications as mods

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('payload', [b'{broken', b'[]', b'{"modifications":{}}', b'{"modifications":[null]}'])
def test_invalid_history_is_preserved_and_cannot_be_overwritten(tmp_path, payload):
    base = str(tmp_path)
    session = Path(mods._get_session_dir(base))
    journal = session / 'modifications.json'
    journal.write_bytes(payload)
    prior_recovery = session / 'modifications.json.corrupt'
    prior_recovery.write_bytes(b'older recovery evidence')
    mods._mods_cache.pop(str(session), None)
    with pytest.raises((ValueError, OSError)):
        mods.get_modifications(base)
    assert journal.read_bytes() == payload
    assert prior_recovery.read_bytes() == b'older recovery evidence'
    assert str(session) not in mods._mods_cache
    assert mods._record_modification(base, 'write_file', 'new') is False
    assert journal.read_bytes() == payload


def test_permission_failure_is_not_renamed_as_corruption(tmp_path, monkeypatch):
    import builtins
    session = Path(mods._get_session_dir(str(tmp_path)))
    journal = session / 'modifications.json'
    payload = b'{"modifications":[]}'
    journal.write_bytes(payload)
    mods._mods_cache.pop(str(session), None)
    original = builtins.open
    def denied(path, *args, **kwargs):
        if str(path) == str(journal):
            raise PermissionError('injected denial')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', denied)
    with pytest.raises(PermissionError):
        mods.get_modifications(str(tmp_path))
    assert journal.read_bytes() == payload
    assert not (session / 'modifications.json.corrupt').exists()


def test_other_corrupt_history_does_not_break_project_resolution(tmp_path):
    bad = tmp_path / 'bad'
    good = tmp_path / 'good'
    bad.mkdir()
    good.mkdir()
    bad_session = Path(mods._get_session_dir(str(bad)))
    (bad_session / 'modifications.json').write_text('{"modifications":[null]}')
    (bad_session / 'project.json').write_text('[]')
    assert mods._record_modification(str(good), 'write_file', 'file', task_id='find-good')
    assert mods.resolve_base_path(task_id='find-good') == str(good)
    assert (bad_session / 'modifications.json').read_text() == '{"modifications":[null]}'
