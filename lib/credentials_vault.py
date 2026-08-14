"""lib/credentials_vault.py — The operator credential vault.

One sanctioned place for machine/release credentials (GitHub PAT, PyPI
token, …) that previously lived scattered across ad-hoc files
(``.secrets/github_token``, ``.secrets/pypirc``) or — worst — hardcoded in
committable source. Owner directive 2026-08-05: a dedicated, secure place
for user credentials; never committed, never shipped by export.

Properties
----------
* **Encrypted at rest** — every value is a Fernet token (AES-128-CBC +
  HMAC, ``cryptography`` is already a hard dep). The store file on disk
  contains no plaintext.
* **Key separation** — the key lives in its own chmod-600 file
  (``.credentials_vault.key``) next to the store. A copied store file (a
  data/ backup, a stray ``cp -r``) is useless without the key file.
* **Never in the repo, never exported** — both files live under
  ``data/config/``, which ``.gitignore`` and ``export.py`` exclude
  wholesale. A guard test pins that.
* **Redaction by default** — :func:`list_entries` returns metadata + a
  hint (``ghp_…3V8``), never the value. The plaintext leaves the process
  only through an explicit reveal call, which the API layer audit-logs.
* **Values are never logged** — not at debug, not in exceptions.

Persistence: ``data/config/credentials_vault.json`` via
:mod:`lib.json_store` (atomic, locked)::

    {"version": 1,
     "entries": {"github_token": {"ct": "gAAA…", "hint": "ghp_…3V8",
                                  "note": "…", "created_at": …,
                                  "updated_at": …}}}

Public API
----------
  set_entry(name, value, note=None) → dict (metadata)
  get_entry(name)                   → str | None     (PLAINTEXT — handle with care)
  delete_entry(name)                → bool
  list_entries()                    → list[dict]     (metadata only)
  bootstrap_from_legacy(secrets_dir) → list[str]     (names imported)

Model-facing seam (the agent must be able to DISCOVER and USE stored
credentials without ever seeing them):

  resolve_exec_env()   → {ENV_VAR: plaintext} for explicitly selected vault
                         entries on one run_command invocation
  build_vault_index()  → the ``<credential_vault>`` system-prompt block
                         (NAMES + env vars + notes ONLY — never values),
                         spliced by system_context/_inject.py so the model
                         always knows which credentials exist
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

__all__ = [
    'set_entry',
    'get_entry',
    'delete_entry',
    'list_entries',
    'bootstrap_from_legacy',
    'normalize_name',
    'env_var_name',
    'resolve_run_command_env',
    'resolve_exec_env',
    'exec_env_overlay',
    'build_vault_index',
]

# config_path() returns str; the vault uses Path methods (.exists etc.), and
# tests monkeypatch these with Path objects — keep the defaults Path too, or
# the production-only codepath dies on 'str' object has no attribute 'exists'
# (bitten 2026-08-05: every test redirected the paths, so only the real run
# saw it).
_STORE_PATH = Path(config_path('credentials_vault.json'))
_KEY_PATH = Path(config_path('.credentials_vault.key'))
_STORE_VERSION = 1
_MAX_ENTRIES = 128
_MAX_VALUE_BYTES = 8192

_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,63}$')

_lock = threading.RLock()
_fernet = None  # lazily constructed Fernet instance


def normalize_name(name: str) -> str:
    """Validate/normalize an entry name. Raises ValueError on unusable input.

    Names double as lookup keys in automation (export.py reads
    ``github_token``), so they are lowercase filename-safe identifiers.
    """
    n = str(name or '').strip().lower()
    if not n:
        raise ValueError('name is required')
    if not _NAME_RE.match(n):
        raise ValueError(
            f'{n!r} is not a valid credential name — use lowercase letters, '
            'digits, dot/dash/underscore, 1-64 chars, starting alphanumeric.')
    return n


def _hint(value: str) -> str:
    """A non-sensitive identifier hint: enough to recognise WHICH token this
    is, never enough to use it."""
    v = value.strip()
    if len(v) <= 8:
        return '****'
    return f'{v[:4]}…{v[-4:]}'


def _load_fernet():
    """Build the Fernet instance, minting the key file on first use.

    The key file is created with mode 600 from the start (open + os.chmod,
    no umask race). A corrupted key file is a hard error — silently minting
    a new one would orphan every stored secret and look like "the vault
    emptied itself", the worst possible failure shape for a credential
    store.
    """
    global _fernet
    if _fernet is not None:
        return _fernet
    with _lock:
        if _fernet is not None:
            return _fernet
        from cryptography.fernet import Fernet
        if _KEY_PATH.exists():
            raw = _KEY_PATH.read_bytes().strip()
            try:
                _fernet = Fernet(raw)
            except (ValueError, TypeError) as e:
                logger.error('[Vault] key file %s is unreadable (%s) — NOT '
                             'minting a replacement (that would orphan every '
                             'stored credential). Fix or delete it manually.',
                             _KEY_PATH, e)
                raise
        else:
            key = Fernet.generate_key()
            fd = os.open(str(_KEY_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
            os.chmod(_KEY_PATH, 0o600)
            logger.info('[Vault] minted new key file %s (mode 600)', _KEY_PATH)
            _fernet = Fernet(key)
        return _fernet


def _encrypt(value: str) -> str:
    return _load_fernet().encrypt(value.encode('utf-8')).decode('ascii')


def _decrypt(ct: str) -> str:
    return _load_fernet().decrypt(ct.encode('ascii')).decode('utf-8')


def _read_store(*, strict: bool = False) -> dict:
    store = read_json(_STORE_PATH, default=None, strict=strict)
    if isinstance(store, dict) and isinstance(store.get('entries'), dict):
        return store
    if strict and _STORE_PATH.exists():
        from lib.json_store import JsonStoreReadError
        raise JsonStoreReadError(
            f'existing credential vault has an invalid shape: {_STORE_PATH}')
    return {'version': _STORE_VERSION, 'entries': {}}


def _write_store(store: dict) -> None:
    update_json_atomic(_STORE_PATH, lambda _: store, default=store)
    try:
        os.chmod(_STORE_PATH, 0o600)
    except OSError as e:
        logger.debug('[Vault] chmod 600 on store failed: %s', e)


def _meta(name: str, row: dict) -> dict:
    """The redacted public view of one entry — no ciphertext, no value."""
    return {
        'name': name,
        'hint': str(row.get('hint') or '****'),
        'note': str(row.get('note') or ''),
        'created_at': float(row.get('created_at') or 0.0),
        'updated_at': float(row.get('updated_at') or 0.0),
    }


def set_entry(name: str, value: str, *, note: Optional[str] = None) -> dict:
    """Create or update a credential. Returns its redacted metadata.

    Raises ValueError on a bad name or an empty/oversized value — a vault
    that silently stores an empty string produces the "why is the token
    empty" debugging session downstream.
    """
    n = normalize_name(name)
    v = str(value or '').strip()
    if not v:
        raise ValueError('value is required (empty credentials are not stored)')
    if len(v.encode('utf-8')) > _MAX_VALUE_BYTES:
        raise ValueError(f'value exceeds {_MAX_VALUE_BYTES} bytes')
    with _lock:
        store = _read_store()
        entries = store['entries']
        row = entries.get(n)
        if row is None:
            if len(entries) >= _MAX_ENTRIES:
                raise ValueError(f'vault quota reached (max {_MAX_ENTRIES})')
            row = {'created_at': time.time()}
            entries[n] = row
        row['ct'] = _encrypt(v)
        row['hint'] = _hint(v)
        if note is not None:
            row['note'] = str(note).strip()[:200]
        row.setdefault('note', '')
        row['updated_at'] = time.time()
        _write_store(store)
        out = _meta(n, row)
    audit_log('credential_vault_set', name=n)
    logger.info('[Vault] set credential name=%s', n)
    return out


def get_entry(name: str) -> Optional[str]:
    """Return the PLAINTEXT value, or None. This is the only read path —
    callers must treat the return as a secret (never log it, never put it
    in an exception message)."""
    try:
        n = normalize_name(name)
    except ValueError as e:
        logger.debug('[Vault] get rejected name %r: %s', name, e)
        return None
    with _lock:
        row = _read_store()['entries'].get(n)
        if not row or not row.get('ct'):
            return None
        try:
            return _decrypt(row['ct'])
        except Exception as e:
            # Wrong/corrupted key or tampered ciphertext. Loud at error level
            # (a credential that cannot be decrypted IS an operational
            # incident) but the message must never carry the ciphertext.
            logger.error('[Vault] decrypt failed for name=%s: %s', n, e)
            return None


def delete_entry(name: str) -> bool:
    """Remove an entry. Idempotent; returns True iff it existed."""
    try:
        n = normalize_name(name)
    except ValueError as e:
        logger.debug('[Vault] delete rejected name %r: %s', name, e)
        return False
    with _lock:
        store = _read_store()
        if n not in store['entries']:
            return False
        del store['entries'][n]
        _write_store(store)
    audit_log('credential_vault_delete', name=n)
    logger.info('[Vault] deleted credential name=%s', n)
    return True


def list_entries() -> list[dict]:
    """All entries, redacted (metadata + hint only), sorted by name."""
    with _lock:
        entries = _read_store()['entries']
        return [_meta(n, r) for n, r in sorted(entries.items())]


# ── Model-facing seam: discovery index + subprocess env overlay ──────────
# The agent must be able to DISCOVER which credentials exist and USE them
# in run_command shells — without a value ever entering the prompt, chat,
# or logs. Two channels, both derived from the one store:
#   • build_vault_index() — the <credential_vault> system-prompt block:
#     NAMES + env vars + notes ONLY (byte-stable, sorted, cache-safe).
#   • resolve_exec_env()  — resolves ONLY the entry names selected by one
#     run_command invocation; no general credential is ambient in every child.
# Skill-scoped entries (``skill.*``) are owned by lib.skills.env and are
# excluded from both channels (its overlay maps them with exact env names).

_ENV_VAR_SANITIZE_RE = re.compile(r'[^A-Z0-9]+')
_WS_RE = re.compile(r'\s+')
SKILL_ENTRY_PREFIX = 'skill.'


def env_var_name(entry: str) -> str:
    """Map a vault entry name to its subprocess environment variable
    (``github_token`` → ``GITHUB_TOKEN``, ``npm-token`` → ``NPM_TOKEN``)."""
    return _ENV_VAR_SANITIZE_RE.sub('_', str(entry or '').upper()).strip('_')


def _general_entry_names() -> list[str]:
    return [m.get('name') or '' for m in list_entries()
            if (m.get('name') or '')
            and not (m.get('name') or '').startswith(SKILL_ENTRY_PREFIX)]


def resolve_run_command_env(names) -> tuple[set[str], dict[str, str]]:
    """Return ``(strip_vars, selected_overlay)`` from one strict snapshot.

    Existing-store read/parse/shape failures raise, so the caller can reject
    execution before spawning instead of inheriting a credential by accident.
    """
    if names is None:
        names = []
    if not isinstance(names, (list, tuple)):
        raise ValueError('credentials must be an array of vault entry names')
    if len(names) > 16:
        raise ValueError('credentials accepts at most 16 entries')

    requested: list[str] = []
    for raw in names:
        if not isinstance(raw, str):
            raise ValueError('each credential must be a vault entry name string')
        name = normalize_name(raw)
        if name.startswith(SKILL_ENTRY_PREFIX):
            raise ValueError(
                f'{name!r} is skill-scoped and cannot be requested by run_command')
        if name not in requested:
            requested.append(name)

    with _lock:
        entries = _read_store(strict=True)['entries']
        available = {name for name in entries
                     if not name.startswith(SKILL_ENTRY_PREFIX)}
        strip_vars = {env_var_name(name) for name in available
                      if env_var_name(name)}
        overlay: dict[str, str] = {}
        owners: dict[str, str] = {}
        for name in requested:
            if name not in available:
                raise ValueError(f'vault credential {name!r} does not exist')
            var = env_var_name(name)
            previous = owners.get(var)
            if previous is not None:
                raise ValueError(
                    f'vault credentials {previous!r} and {name!r} both map to ${var}')
            row = entries.get(name) or {}
            ct = row.get('ct')
            if not ct:
                raise ValueError(
                    f'vault credential {name!r} could not be resolved')
            try:
                value = _decrypt(ct)
            except Exception as e:
                logger.error('[Vault] decrypt failed for name=%s: %s', name, e)
                raise ValueError(
                    f'vault credential {name!r} could not be resolved') from None
            owners[var] = name
            overlay[var] = value

    if requested:
        audit_log('credential_vault_use', names=','.join(requested))
    return strip_vars, overlay


def resolve_exec_env(names) -> dict[str, str]:
    """Resolve selected entries for trusted callers needing only the overlay."""
    return resolve_run_command_env(names)[1]


def exec_env_overlay() -> dict[str, str]:
    """Plaintext values of every general entry for trusted internal callers.

    ``run_command`` does not use this broad helper: it calls
    :func:`resolve_exec_env` with the names explicitly requested by that tool
    invocation. This compatibility API remains for trusted release/export
    integrations and never raises.
    """
    try:
        return resolve_exec_env(_general_entry_names())
    except Exception as e:
        logger.warning('[Vault] exec env overlay failed: %s', e)
        return {}


def build_vault_index() -> str:
    """The ``<credential_vault>`` system-prompt block — the model's discovery
    seam. NAMES + env vars + notes ONLY; values and hints never ride the
    prompt. Byte-stable for a fixed entry set (sorted by name, whitespace
    collapsed) so prompt caching survives; changes only on user-driven CRUD.
    ``''`` when the vault holds no general entries (or on failure) — the
    caller then splices nothing."""
    try:
        rows = [m for m in list_entries()
                if not (m.get('name') or '').startswith(SKILL_ENTRY_PREFIX)]
    except Exception as e:
        logger.warning('[Vault] index build failed: %s', e)
        return ''
    if not rows:
        return ''
    lines = [
        '<credential_vault>',
        'The user stores machine credentials (API tokens, PATs, …) in an '
        'encrypted vault on this server. The entries below exist — NAMES '
        'ONLY; the values are never shown to you. To use one, include its '
        'entry name in run_command\'s `credentials` array; only those selected '
        'values are injected into that child process under the variable named '
        'after `→`. Reference the variable, never the value (e.g. '
        '`curl -H "Authorization: Bearer $VAR" …`; tools like `gh`, `pip`, '
        '`twine` read their standard variables natively).',
        '',
    ]
    for m in rows:
        name = m.get('name') or ''
        note = _WS_RE.sub(' ', str(m.get('note') or '')).strip()
        line = f'- {name} → ${env_var_name(name)}'
        if note:
            line += f' ({note})'
        lines.append(line)
    lines += [
        '',
        'NEVER print, echo, copy, or write a credential value into chat, '
        'files, command output, or logs — if a command would expose one '
        '(e.g. `echo $VAR`, `env`, `printenv`), do not run it. If a '
        'credential you need is missing, tell the user to add it in '
        'Settings → Advanced → 凭证保管库.',
        '</credential_vault>',
    ]
    return '\n'.join(lines)


def bootstrap_from_legacy(secrets_dir: Path) -> list[str]:
    """One-shot import of the ad-hoc ``.secrets/`` files into the vault.

    Fills only MISSING entries (an existing vault entry always wins), so it
    is idempotent and safe to call on every boot / export. Returns the names
    it imported. The legacy files are left in place — the vault is a copy,
    and deleting someone's only token file from under them is not this
    function's call to make.
    """
    imported: list[str] = []
    secrets_dir = Path(secrets_dir)
    sources = {
        'github_token': secrets_dir / 'github_token',
    }
    for name, path in sources.items():
        try:
            if get_entry(name) is not None:
                continue
            if not path.is_file():
                continue
            value = path.read_text(encoding='utf-8').strip()
            if not value:
                continue
            set_entry(name, value, note=f'imported from {path.name}')
            imported.append(name)
        except (OSError, ValueError) as e:
            logger.warning('[Vault] legacy import of %s failed: %s', path, e)
    # PyPI token lives inside a pypirc INI blob, not a bare file.
    try:
        if get_entry('pypi_token') is None:
            pypirc = secrets_dir / 'pypirc'
            if pypirc.is_file():
                m = re.search(r'^\s*password\s*=\s*(\S+)\s*$',
                              pypirc.read_text(encoding='utf-8'), re.M)
                if m:
                    set_entry('pypi_token', m.group(1),
                              note='imported from pypirc')
                    imported.append('pypi_token')
    except (OSError, ValueError) as e:
        logger.warning('[Vault] legacy import of pypirc failed: %s', e)
    if imported:
        logger.info('[Vault] bootstrapped from legacy .secrets: %s',
                    ', '.join(imported))
    return imported
