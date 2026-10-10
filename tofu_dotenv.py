"""Canonical, dependency-free parser for Tofu's project ``.env`` file.

Responsibility:
  - define the one dotenv syntax shared by startup, bootstrap, diagnostics,
    support bundles, and standalone data tools;
  - define the canonical spelling of boolean environment values;
  - load only valid environment-variable names and never overwrite an explicit
    process environment value.

Entry points: ``parse_dotenv``, ``read_dotenv_values``, ``load_dotenv_file``,
``update_dotenv_file`` (atomic, private writes; caller serializes writers).
Dependencies: Python standard library only, so startup repair can import it
before third-party packages are known to work.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping
import tempfile
from pathlib import Path


_ENVIRONMENT_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
MAX_DOTENV_BYTES = 256 * 1024
ENV_BOOLEAN_TRUE_VALUES = frozenset(('1', 'true', 'yes', 'on', 'enabled'))
ENV_BOOLEAN_FALSE_VALUES = frozenset(('0', 'false', 'no', 'off', 'disabled'))


def parse_env_boolean(value: object) -> bool | None:
    """Parse one explicit environment boolean; return ``None`` if ambiguous."""
    normalized = str(value or '').strip().lower()
    if normalized in ENV_BOOLEAN_TRUE_VALUES:
        return True
    if normalized in ENV_BOOLEAN_FALSE_VALUES:
        return False
    return None


def _parse_value(raw_value: str) -> str:
    # Keep this deliberately smaller than a shell parser: Tofu does not expand
    # variables or execute substitutions from a configuration file. The quote
    # trimming matches the lifecycle manager's pre-existing stdlib-only reader.
    return raw_value.strip().strip('"').strip("'")


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse Tofu's intentionally small, explicit dotenv syntax.

    Supported forms are ``NAME=value`` with optional surrounding single/double
    quotes. Full-line comments, invalid names, and non-assignment lines are
    ignored; ``#`` inside a value remains data. Later assignments win.
    """
    values: dict[str, str] = {}
    for raw_line in str(text).splitlines():
        line = raw_line.strip().lstrip('\ufeff')
        if not line or line.startswith('#'):
            continue
        name, separator, raw_value = line.partition('=')
        name = name.strip()
        if not separator or not _ENVIRONMENT_NAME_RE.fullmatch(name):
            continue
        values[name] = _parse_value(raw_value)
    return values


def read_dotenv_values(path: str | os.PathLike[str]) -> dict[str, str]:
    """Read a dotenv file; a missing optional file is an empty configuration."""
    source = Path(path)
    try:
        size = source.stat().st_size
        if size > MAX_DOTENV_BYTES:
            raise OSError(
                f'{source}: .env exceeds the {MAX_DOTENV_BYTES}-byte limit')
        text = source.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as exc:
        raise OSError(f'{source}: .env must be valid UTF-8') from exc
    return parse_dotenv(text)


def update_dotenv_file(
        path: str | os.PathLike[str], updates: Mapping[str, str],
        *, remove: tuple[str, ...] = ()) -> None:
    """Atomically update canonical assignments, preserving unrelated content.

    Values must round-trip without newline injection. Callers serialize writers
    (installer directory lock or repair-page mutex); no process env is changed.
    """
    target = Path(path)
    if target.is_symlink():
        raise ValueError('Refusing to rewrite a symlinked .env')
    for name, value in updates.items():
        if (not _ENVIRONMENT_NAME_RE.fullmatch(name) or not isinstance(value, str)
                or any(char in value for char in '\x00\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029')
                or parse_dotenv(f'{name}={value}').get(name) != value):
            raise ValueError('Configuration value cannot round-trip through dotenv')
    try:
        with target.open('rb') as source:
            raw = source.read(MAX_DOTENV_BYTES + 1)
    except FileNotFoundError:
        raw = b''
    if len(raw) > MAX_DOTENV_BYTES:
        raise ValueError('.env exceeds its size budget')
    keys = set(updates) | set(remove)
    text = ''.join(line for line in raw.decode('utf-8').splitlines(keepends=True)
                   if not keys.intersection(parse_dotenv(line)))
    if text and not text.endswith('\n'):
        text += '\n'
    text += ''.join(f'{name}={value}\n' for name, value in updates.items())
    encoded = text.encode('utf-8')
    if len(encoded) > MAX_DOTENV_BYTES:
        raise ValueError('.env exceeds its size budget')
    if encoded == raw and target.exists():
        target.chmod(0o600)
        return
    fd, temporary = tempfile.mkstemp(prefix='.env-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_dotenv_file(
        path: str | os.PathLike[str],
        environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Load file values without overriding the caller's explicit environment."""
    values = read_dotenv_values(path)
    target = os.environ if environ is None else environ
    for name, value in values.items():
        if name not in target:
            target[name] = value
    return values
