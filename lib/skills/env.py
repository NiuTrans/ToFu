"""lib/skills/env.py — Skill environment/credential bindings (vault-backed).

Skill packages declare the environment variables they need in frontmatter
(OpenClaw ``metadata.openclaw.requires.env`` / ``primaryEnv``, or the legacy
top-level ``requires_env`` list). The values themselves are USER secrets —
they live in the credential vault (:mod:`lib.credentials_vault`,
Fernet-encrypted at rest, never committed, never exported), keyed
``skill.<skill_id>.<env_lower>``.

This module is the ONLY seam that knows that key scheme. Consumers:

* ``lib/memory/storage/_files.py`` eligibility — a declared env var counts
  as satisfied when it is set in the process environment OR configured in
  the vault (so a key the user pasted in Settings unlocks the skill).
* subprocess execution (``run_command`` / ``code_exec``) —
  :func:`exec_env_overlay` projects only each ENABLED skill's declared process
  variables plus its configured owner-scoped vault values into the child
  environment, so a
  skill's documented ``os.environ['SOME_KEY']`` lookup works without exposing
  unrelated machine credentials.
* ``routes/api_v1/skills.py`` — the Settings → Skills configuration UI.

Values are NEVER logged and never returned by any list/status API — only
the vault's redacted hint crosses the wire.
"""

from __future__ import annotations

import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'declared_env',
    'entry_name',
    'env_name_from_entry',
    'skill_env_status',
    'get_skill_env',
    'set_skill_env',
    'delete_skill_env',
    'clear_skill_env',
    'exec_env_overlay',
    'vault_has_env',
]

_ENTRY_PREFIX = 'skill.'
_ENV_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_ID_SANITIZE_RE = re.compile(r'[^a-z0-9_.-]+')


def _norm_skill_id(skill_id: str) -> str:
    """Normalize a skill id for use inside a vault entry name (the vault
    accepts ``^[a-z0-9][a-z0-9_.-]{0,63}$``). Skill ids are directory names
    and almost always already valid; this is the defensive normalization."""
    return _ID_SANITIZE_RE.sub('-', str(skill_id or '').strip().lower()).strip('-._')


def _entry_prefix(skill_id: str, owner_user_id: int | None = None) -> str:
    normalized = _norm_skill_id(skill_id)
    if owner_user_id is None:
        return f'{_ENTRY_PREFIX}{normalized}.'
    from lib.identity import require_user_id
    owner = require_user_id(owner_user_id, context='skill credential')
    return f'{_ENTRY_PREFIX}u{owner}.{normalized}.'


def _read_prefixes(skill_id: str,
                   owner_user_id: int | None = None) -> tuple[str, ...]:
    """Return owner prefix plus the personal legacy prefix when applicable."""
    primary = _entry_prefix(skill_id, owner_user_id)
    if owner_user_id is None:
        return (primary,)
    from lib.identity import PERSONAL_USER_ID, require_user_id
    owner = require_user_id(owner_user_id, context='skill credential')
    legacy = _entry_prefix(skill_id, None)
    return (primary, legacy) if owner == PERSONAL_USER_ID else (primary,)


def entry_name(skill_id: str, env_name: str,
               owner_user_id: int | None = None) -> str:
    """The vault entry name for one skill env binding.

    Env var names are conventionally ``[A-Z0-9_]+``, so lowercasing into the
    vault name and uppercasing back is lossless.
    """
    return (_entry_prefix(skill_id, owner_user_id)
            + str(env_name).strip().lower())


def env_name_from_entry(entry: str, skill_id: str,
                        owner_user_id: int | None = None) -> str | None:
    """Reverse :func:`entry_name` — the env var name, or None when the
    entry does not belong to this skill."""
    value = str(entry or '')
    for prefix in _read_prefixes(skill_id, owner_user_id):
        if value.startswith(prefix):
            return value[len(prefix):].upper()
    return None


def declared_env(skill: dict) -> list[str]:
    """Every env var a skill declares (``requires_env`` + ``primary_env``),
    de-duplicated, order preserved."""
    out: list[str] = []
    for name in (skill.get('requires_env') or []):
        n = str(name).strip()
        if n and n not in out:
            out.append(n)
    primary = str(skill.get('primary_env') or '').strip()
    if primary and primary not in out:
        out.append(primary)
    return out


def set_skill_env(skill_id: str, env_name: str, value: str,
                  owner_user_id: int | None = None) -> dict:
    """Create/update one binding in the vault. Returns redacted metadata.

    Raises ValueError on a malformed env name or empty value (both are
    user-input errors, surfaced by the API as 400).
    """
    name = str(env_name or '').strip()
    if not _ENV_NAME_RE.match(name):
        raise ValueError(f'{name!r} is not a valid environment variable name')
    from lib.credentials_vault import set_entry
    return set_entry(entry_name(skill_id, name, owner_user_id), value,
                     note=f'skill {skill_id} env {name.upper()}')


def delete_skill_env(skill_id: str, env_name: str,
                     owner_user_id: int | None = None) -> bool:
    """Remove one binding. Idempotent."""
    from lib.credentials_vault import delete_entry
    removed = False
    suffix = str(env_name).strip().lower()
    for prefix in _read_prefixes(skill_id, owner_user_id):
        removed = delete_entry(prefix + suffix) or removed
    return removed


def vault_has_env(skill_id: str, env_name: str,
                  owner_user_id: int | None = None) -> bool:
    """True when a value is configured in the vault for this skill+var.

    Used by the eligibility gate — a miss here just means "user has not
    configured it", never an error state.
    """
    from lib.credentials_vault import get_entry
    try:
        suffix = str(env_name).strip().lower()
        return any(
            get_entry(prefix + suffix) is not None
            for prefix in _read_prefixes(skill_id, owner_user_id)
        )
    except Exception as e:
        # Vault trouble (corrupt key file etc.) must not crash a listing —
        # treat as "not configured" and let the skill show its missing-env
        # reason. The vault itself already logged the underlying error.
        logger.debug('[Skills.env] vault probe failed for %s: %s',
                     skill_id, e)
        return False


def get_skill_env(skill_id: str,
                  owner_user_id: int | None = None) -> dict[str, str]:
    """Every configured value for a skill, ``{ENV_NAME: plaintext}``.

    Caller treats the values as secrets. This is the seam subprocess
    execution uses; the Settings UI never calls it.
    """
    from lib.credentials_vault import get_entry, list_entries
    out: dict[str, str] = {}
    # Legacy first, owner-specific last so an explicit owner migration wins.
    prefixes = tuple(reversed(_read_prefixes(skill_id, owner_user_id)))
    entries = list_entries()
    for prefix in prefixes:
        for meta in entries:
            entry = meta.get('name') or ''
            if not entry.startswith(prefix):
                continue
            value = get_entry(entry)
            if value is not None:
                out[entry[len(prefix):].upper()] = value
    return out


def skill_env_status(skill: dict,
                     owner_user_id: int | None = None) -> list[dict]:
    """Redacted per-variable status for the Settings UI:
    ``[{name, declared, configured, hint}]`` — declared vars first, then any
    extra configured ones."""
    declared = declared_env(skill)
    configured = get_skill_env_map(
        skill['id'], owner_user_id=owner_user_id)
    rows = []
    seen = set()
    for name in declared:
        meta = configured.get(name.upper())
        rows.append({
            'name': name,
            'declared': True,
            'configured': meta is not None,
            'hint': (meta or {}).get('hint', ''),
        })
        seen.add(name.upper())
    for upper, meta in sorted(configured.items()):
        if upper in seen:
            continue
        rows.append({
            'name': upper,
            'declared': False,
            'configured': True,
            'hint': meta.get('hint', ''),
        })
    return rows


def get_skill_env_map(skill_id: str,
                      owner_user_id: int | None = None) -> dict[str, dict]:
    """``{ENV_NAME: redacted vault metadata}`` for one skill."""
    from lib.credentials_vault import list_entries
    out: dict[str, dict] = {}
    prefixes = tuple(reversed(_read_prefixes(skill_id, owner_user_id)))
    entries = list_entries()
    for prefix in prefixes:
        for meta in entries:
            entry = meta.get('name') or ''
            if entry.startswith(prefix):
                out[entry[len(prefix):].upper()] = meta
    return out


def clear_skill_env(skill_id: str,
                    owner_user_id: int | None = None) -> int:
    """Delete every vault binding of a skill (uninstall path). Returns the
    number removed. No orphan secrets: uninstalling a skill must not leave
    its keys behind in the vault."""
    from lib.credentials_vault import delete_entry, list_entries
    removed = 0
    for meta in list_entries():
        entry = meta.get('name') or ''
        if (any(entry.startswith(prefix) for prefix in _read_prefixes(
                skill_id, owner_user_id)) and delete_entry(entry)):
            removed += 1
    if removed:
        logger.info('[Skills.env] cleared %d vault binding(s) for %s',
                    removed, skill_id)
    return removed


def exec_env_overlay(project_path: str | None = None,
                     extra_paths: list[str] | None = None,
                     owner_user_id: int | None = None) -> dict[str, str]:
    """Configured env vars of every ENABLED skill, for subprocess injection.

    Disabled skills contribute nothing (a disabled skill is deliberately
    off). Skills hidden by other eligibility gates (missing binary etc.)
    still contribute — their env being present is harmless and keeps a
    configured key working the moment the user installs the binary.

    Never raises: env resolution happens on the subprocess hot path, so any
    failure degrades to an empty overlay with a logged warning.
    """
    try:
        from lib.skills.registry import list_skills
        overlay: dict[str, str] = {}
        for skill in list_skills(
                project_path, extra_paths=extra_paths,
                owner_user_id=owner_user_id):
            if not skill.get('enabled', True):
                continue
            for name in declared_env(skill):
                if name in os.environ:
                    overlay[name] = os.environ[name]
            overlay.update(get_skill_env(
                skill['id'], owner_user_id=owner_user_id))
        return overlay
    except Exception as e:
        logger.warning('[Skills.env] exec env overlay failed: %s', e)
        return {}
