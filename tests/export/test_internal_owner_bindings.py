"""An Internal export never carries the exporting owner's channel bindings (docs/CHANNELS.md C6, C12).

The credential vault travels with its key so colleagues' installs work out of
the box (owner decision 2026-11-01), but the reserved ``channel.u<owner>.*``
entries are the owner's own channel bindings: his 微信 ClawBot session token,
his bot platform secrets and his Settings robot secret. On a colleague's
server whose administrator is also owner 1 they would bind that server to the
exporting owner's accounts, so they stay behind. Every value is synthetic.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

export = pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

import export_pkg._handoff as handoff  # noqa: E402
import export_pkg._internal as internal  # noqa: E402

pytestmark = pytest.mark.unit

SHARED = {'github_token': 'ghp_FAKEsharedTeamToken0001', 'skill.sandbox-direct-access.token': 'sbx-fake-0002'}
OWNER_BOUND = {'channel.u1.wechat.token': 'fake-clawbot-token-0003', 'channel.u1.dx.robot': 'fake-robot-0004',
               'channel.u2.qq.secret': 'fake-qq-secret-0005'}


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    import lib.credentials_vault as module

    def at(config_dir: Path):
        monkeypatch.setattr(module, '_STORE_PATH', config_dir / 'credentials_vault.json')
        monkeypatch.setattr(module, '_KEY_PATH', config_dir / '.credentials_vault.key')
        monkeypatch.setattr(module, '_fernet', None)
        return module
    return at


@pytest.fixture()
def source(tmp_path, monkeypatch, vault):
    root = tmp_path / 'owner-host'
    (root / 'data' / 'config').mkdir(parents=True)
    store = vault(root / 'data' / 'config')
    for name, value in {**SHARED, **OWNER_BOUND}.items():
        store.set_entry(name, value)
    monkeypatch.setattr(internal, 'ROOT', root)
    return root


def test_the_carried_vault_keeps_shared_entries_and_drops_owner_channel_bindings(source, tmp_path, vault):
    dest = tmp_path / 'colleague'
    internal._copy_carried_config(dest, 'internal')
    carried = dest / 'data' / 'config' / 'credentials_vault.json'
    assert carried.stat().st_mode & 0o777 == 0o600
    assert (dest / 'data' / 'config' / '.credentials_vault.key').is_file()
    store = vault(dest / 'data' / 'config')
    assert sorted(entry['name'] for entry in store.list_entries()) == sorted(SHARED)
    assert all(store.get_entry(name) == value for name, value in SHARED.items())
    raw = carried.read_text(encoding='utf-8')
    assert not any(name in raw for name in OWNER_BOUND)
    # The exporting host keeps its own vault untouched.
    assert sorted(entry['name'] for entry in vault(source / 'data' / 'config').list_entries()) == sorted(
        {**SHARED, **OWNER_BOUND})


def test_the_handoff_state_tarball_carries_the_same_filtered_vault(source, tmp_path, vault, monkeypatch):
    import tarfile
    (source / 'data' / 'config' / 'server_config.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(handoff, 'ROOT', source)
    # The live provider authority and the owner's MCP store are read from the running app: not here.
    monkeypatch.setattr(handoff, '_stage_carried_model_routing', lambda dest, mode: None)
    monkeypatch.setattr(handoff, '_stage_portable_mcp_config', lambda dest: None)
    tar_path = tmp_path / 'tofu_state.tar.gz'
    handoff._write_state_tarball(tar_path, 'internal')
    with tarfile.open(tar_path) as archive:
        member = archive.extractfile('data/config/credentials_vault.json')
        names = set(json.loads(member.read().decode('utf-8'))['entries'])
    assert names == set(SHARED)


def test_an_unreadable_vault_fails_the_internal_export_closed(source, tmp_path):
    (source / 'data' / 'config' / 'credentials_vault.json').write_text('{not json', encoding='utf-8')
    with pytest.raises(RuntimeError):
        internal._copy_carried_config(tmp_path / 'colleague', 'internal')
    assert not (tmp_path / 'colleague' / 'data' / 'config' / 'credentials_vault.json').exists()
