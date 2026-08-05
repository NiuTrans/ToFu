"""tests/test_credentials_vault.py — the credential vault's contract.

WHAT THIS PINS (owner directive 2026-08-05: "一个专门放凭证的地方,做好安全性,
绝不把凭证开发到公共平台"):

1. ENCRYPTED AT REST — the store file on disk must not contain the plaintext
   (Fernet token), and the key lives in a SEPARATE chmod-600 file so a copied
   store is useless without it.
2. REDACTION BY DEFAULT — list_entries()/GET list never emit a value or the
   ciphertext; the only plaintext egress is the audited reveal path.
3. NEVER PUBLISHED — the store + key live under data/config/, which is
   gitignored and export-excluded wholesale.
4. CONSUMERS RESOLVE THROUGH THE VAULT — export.py's GitHub-token loader
   prefers env, then the vault, then the legacy .secrets file.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = pytest.mark.unit

_SECRET = 'ghp_TestToken0123456789abcdefXYZ9'  # not a real credential


@pytest.fixture(autouse=True)
def _isolated_vault():
    """Point the vault at a tmp store+key — never touch the real data/config."""
    import lib.credentials_vault as vault
    prev_store, prev_key, prev_fernet = vault._STORE_PATH, vault._KEY_PATH, vault._fernet
    tmp = Path(tempfile.mkdtemp())
    vault._STORE_PATH = tmp / 'credentials_vault.json'
    vault._KEY_PATH = tmp / '.credentials_vault.key'
    vault._fernet = None
    yield vault
    vault._STORE_PATH, vault._KEY_PATH, vault._fernet = prev_store, prev_key, prev_fernet


# ── 1. CRUD + encryption at rest ──

def test_set_get_delete_roundtrip(_isolated_vault):
    v = _isolated_vault
    meta = v.set_entry('github_token', _SECRET, note='export push')
    assert meta['name'] == 'github_token'
    assert meta['hint'] == f'{_SECRET[:4]}…{_SECRET[-4:]}'
    assert 'value' not in meta and 'ct' not in meta
    assert v.get_entry('github_token') == _SECRET
    assert v.delete_entry('github_token') is True
    assert v.get_entry('github_token') is None
    assert v.delete_entry('github_token') is False, 'delete must be idempotent'


def test_store_file_on_disk_holds_no_plaintext(_isolated_vault):
    v = _isolated_vault
    v.set_entry('pypi_token', _SECRET)
    raw = v._STORE_PATH.read_bytes()
    assert _SECRET.encode() not in raw, 'plaintext leaked into the store file'
    assert raw.strip(), 'store must exist on disk'


def test_files_are_owner_only(_isolated_vault):
    v = _isolated_vault
    v.set_entry('github_token', _SECRET)
    key_mode = stat.S_IMODE(os.stat(v._KEY_PATH).st_mode)
    store_mode = stat.S_IMODE(os.stat(v._STORE_PATH).st_mode)
    assert key_mode == 0o600, f'key file mode {key_mode:o} must be 600'
    assert store_mode == 0o600, f'store file mode {store_mode:o} must be 600'


def test_copied_store_without_the_key_is_useless(_isolated_vault):
    """Key separation: a leaked store file alone must decrypt to NOTHING."""
    v = _isolated_vault
    v.set_entry('github_token', _SECRET)
    ct = v._read_store()['entries']['github_token']['ct']

    from cryptography.fernet import Fernet, InvalidToken
    other = Fernet(Fernet.generate_key())
    with pytest.raises(InvalidToken):
        other.decrypt(ct.encode('ascii'))


def test_wrong_key_surfaces_as_none_with_an_error_log(_isolated_vault, caplog):
    """A swapped key file must fail LOUDLY (error log) and return None —
    never silently mint a fresh key and orphan every stored credential."""
    v = _isolated_vault
    v.set_entry('github_token', _SECRET)
    from cryptography.fernet import Fernet
    v._KEY_PATH.write_bytes(Fernet.generate_key())
    v._fernet = None
    with caplog.at_level('ERROR'):
        assert v.get_entry('github_token') is None
    assert any('decrypt failed' in r.message for r in caplog.records)


def test_values_survive_a_fresh_process_read(_isolated_vault):
    """Persistence: dropping the in-memory Fernet and re-reading from disk."""
    v = _isolated_vault
    v.set_entry('github_token', _SECRET)
    v._fernet = None  # simulate a process restart (key reloads from disk)
    assert v.get_entry('github_token') == _SECRET


# ── 2. Validation ──

@pytest.mark.parametrize('bad', ['', '   ', None, 'has space', 'slash/x', 'UPPER..ok?',
                                 '中文名', '-lead-dash', 'x' * 65])
def test_bad_names_rejected(_isolated_vault, bad):
    with pytest.raises(ValueError):
        _isolated_vault.set_entry(bad, _SECRET)


def test_name_normalizes_to_lowercase(_isolated_vault):
    v = _isolated_vault
    assert v.normalize_name('GitHub_Token') == 'github_token'


def test_empty_value_rejected(_isolated_vault):
    with pytest.raises(ValueError, match='value'):
        _isolated_vault.set_entry('github_token', '   ')


def test_oversized_value_rejected(_isolated_vault):
    with pytest.raises(ValueError, match='bytes'):
        _isolated_vault.set_entry('big', 'x' * 9000)


# ── 3. Redaction ──

def test_list_is_fully_redacted(_isolated_vault):
    v = _isolated_vault
    v.set_entry('github_token', _SECRET)
    v.set_entry('pypi_token', 'pypi-AgEIcHlwaS5vcmcCJGFiYzAxMjM0NTY3ODk')
    rows = v.list_entries()
    assert [r['name'] for r in rows] == ['github_token', 'pypi_token']
    import json
    blob = json.dumps(rows)
    assert _SECRET not in blob and 'pypi-AgEI' not in blob
    for r in rows:
        assert set(r) <= {'name', 'hint', 'note', 'created_at', 'updated_at'}, (
            f'unexpected key in redacted row: {set(r)}')


def test_short_values_get_a_full_mask_hint(_isolated_vault):
    assert _isolated_vault.set_entry('tiny', 'abc')['hint'] == '****'


# ── 4. Legacy bootstrap ──

def test_bootstrap_imports_legacy_secrets(_isolated_vault, tmp_path):
    v = _isolated_vault
    secrets = tmp_path / '.secrets'
    secrets.mkdir()
    (secrets / 'github_token').write_text(_SECRET, encoding='utf-8')
    (secrets / 'pypirc').write_text(
        '[pypi]\n  username = __token__\n  password = pypi-LEGACY123\n', encoding='utf-8')
    imported = v.bootstrap_from_legacy(secrets)
    assert set(imported) == {'github_token', 'pypi_token'}
    assert v.get_entry('github_token') == _SECRET
    assert v.get_entry('pypi_token') == 'pypi-LEGACY123'


def test_bootstrap_never_overwrites_an_existing_entry(_isolated_vault, tmp_path):
    v = _isolated_vault
    v.set_entry('github_token', 'vault-wins-value')
    secrets = tmp_path / '.secrets'
    secrets.mkdir()
    (secrets / 'github_token').write_text(_SECRET, encoding='utf-8')
    assert v.bootstrap_from_legacy(secrets) == []
    assert v.get_entry('github_token') == 'vault-wins-value'


def test_bootstrap_is_a_noop_without_legacy_files(_isolated_vault, tmp_path):
    assert _isolated_vault.bootstrap_from_legacy(tmp_path / 'nope') == []


# ── 5. HTTP surface (envelope + auth + reveal audit) ──

def _admin_token():
    from lib.api_keys import create_key
    _row, token = create_key(name='vault-test', scopes=[], admin=True)
    return token


def test_vault_routes_are_registered(flask_client):
    """The blueprint must be mounted (an unmount shows up as 404, not a
    feature-shaped error). NOTE: the test env runs with auth-mode OFF, so
    an unauthenticated request serves 200 here — the auth gate itself is
    covered generically by the api surface, not by this suite."""
    r = flask_client.get('/api/v1/credentials')
    assert r.status_code == 200
    assert r.get_json()['ok'] is True


def test_vault_http_roundtrip(_isolated_vault, flask_client):
    tok = _admin_token()
    auth = {'Authorization': f'Bearer {tok}'}

    r = flask_client.post('/api/v1/credentials', headers=auth,
                          json={'name': 'github_token', 'value': _SECRET,
                                'note': 'export push'})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['ok'] is True
    assert 'value' not in body['credential']

    r = flask_client.get('/api/v1/credentials', headers=auth)
    listing = r.get_json()
    assert listing['ok'] is True
    names = [c['name'] for c in listing['credentials']]
    assert 'github_token' in names
    assert _SECRET not in str(listing), 'list must never echo a value'

    r = flask_client.post('/api/v1/credentials/github_token/reveal', headers=auth)
    assert r.get_json()['value'] == _SECRET

    r = flask_client.delete('/api/v1/credentials/github_token', headers=auth)
    assert r.get_json()['ok'] is True
    r = flask_client.post('/api/v1/credentials/github_token/reveal', headers=auth)
    assert r.status_code == 404


def test_vault_rejects_bad_input_with_400(_isolated_vault, flask_client):
    tok = _admin_token()
    auth = {'Authorization': f'Bearer {tok}'}
    r = flask_client.post('/api/v1/credentials', headers=auth,
                          json={'name': '', 'value': 'x'})
    assert r.status_code == 400
    r = flask_client.post('/api/v1/credentials', headers=auth,
                          json={'name': 'ok_name', 'value': ''})
    assert r.status_code == 400


# ── 6. Never published ──

def test_default_paths_are_path_objects():
    """The module defaults must be Path (config_path returns str; the vault
    calls .exists()/.read_bytes() on them — a str default only breaks in
    production, where no test fixture redirected the paths). Reloads the
    module so the check sees the real DEFAULTS, not this suite's tmp
    redirection."""
    import importlib

    import lib.credentials_vault as vault
    reloaded = importlib.reload(vault)
    assert isinstance(reloaded._STORE_PATH, Path)
    assert isinstance(reloaded._KEY_PATH, Path)


def test_vault_files_live_under_the_excluded_data_dir():
    """Both DEFAULT paths must resolve under <data>/config/ — the wholesale
    gitignore + export exclusion covers data/ and nothing else must be
    relied on. (Reads the defaults from config_path, not the module attrs,
    so the tmp-redirecting isolation fixture cannot confuse it.)"""
    from lib.config_dir import config_path
    from lib.runtime_paths import data_root
    root = os.path.realpath(data_root())
    for p in (config_path('credentials_vault.json'),
              config_path('.credentials_vault.key')):
        real = os.path.realpath(str(p))
        assert real.startswith(root + os.sep), (
            f'{p} escapes the data dir — it would be committable/exportable')


@pytest.mark.skipif(is_opensource_build(),
                    reason='export.py is not shipped in opensource builds')
def test_data_dir_is_export_excluded():
    root = Path(__file__).resolve().parent.parent
    import importlib.util
    spec = importlib.util.spec_from_file_location('_export_probe', root / 'export.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert 'data' in set(mod.ALWAYS_EXCLUDE_DIRS)


# ── 7. export.py resolves through the vault ──

@pytest.mark.skipif(is_opensource_build(),
                    reason='export.py is not shipped in opensource builds')
def test_export_token_loader_prefers_env(_isolated_vault):
    import export as exp
    with mock.patch.dict(os.environ, {'TOFU_GH_TOKEN': 'env-wins'}):
        assert exp._load_gh_token() == 'env-wins'


@pytest.mark.skipif(is_opensource_build(),
                    reason='export.py is not shipped in opensource builds')
def test_export_token_loader_reads_the_vault_second(_isolated_vault):
    import export as exp
    _isolated_vault.set_entry('github_token', _SECRET)
    env = {k: v for k, v in os.environ.items() if k != 'TOFU_GH_TOKEN'}
    with mock.patch.dict(os.environ, env, clear=True):
        assert exp._load_gh_token() == _SECRET
