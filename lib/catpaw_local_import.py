"""CatPaw (Credit Hub) one-click local key import.

The CatPaw/mcopilot CLI (``mc --code``) writes the employee's personal
Credit-Hub credential to a fixed loopback path after one interactive login.
Tofu can therefore onboard the relay with zero copy-paste: read the
``AUTHORIZATION`` token server-side, validate it, and seal it into the
owner-scoped credential envelope.

The path is fixed and server-side only — never accept a client-supplied
location. The plaintext token is never returned to the browser; only a
presence/validity verdict leaves this module.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

#: Fixed, documented CLI config location (see static/provider_templates/
#: catpaw.json ``_gateway_note``). Deliberately not configurable from the
#: request — a client-controlled path would turn this into a file-read oracle.
CATPAW_CLI_CONFIG = os.path.join('~', '.config', 'mcopilot-cli', '.config.yaml')

#: The gateway token is exactly 32 chars (alphanumeric observed; allow -/_).
_TOKEN_RE = re.compile(r'^[A-Za-z0-9_\-]{32}$')


class CatpawLocalImportError(Exception):
    """Raised when no usable CatPaw credential is available locally.

    ``reason`` is a stable machine code the route maps to a user-facing
    i18n key; it never contains the secret or raw file contents.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _config_path() -> str:
    return os.path.expanduser(CATPAW_CLI_CONFIG)


def detect_local_key() -> dict:
    """Return ``{available: bool}`` without touching or returning the secret.

    Used by the status read so the card can show "可一键接入" vs "未检测到".
    """
    try:
        read_local_key()
    except CatpawLocalImportError:
        return {'available': False}
    return {'available': True}


def read_local_key() -> str:
    """Read and validate the AUTHORIZATION token from the CLI config.

    Raises CatpawLocalImportError with a stable reason code on any failure.
    """
    path = _config_path()
    if not os.path.isfile(path):
        raise CatpawLocalImportError('cli_config_missing')
    try:
        import yaml
        with open(path, 'r', encoding='utf-8') as handle:
            data = yaml.safe_load(handle)
    except Exception:
        # Never surface the parse error text — it can embed file contents.
        logger.warning('catpaw local import: unreadable CLI config at %s', path)
        raise CatpawLocalImportError('cli_config_unreadable')
    token = ''
    if isinstance(data, dict):
        token = str(data.get('AUTHORIZATION') or '').strip()
    if not _TOKEN_RE.match(token):
        raise CatpawLocalImportError('authorization_invalid')
    return token


def secret_envelope(api_key: str) -> str:
    """Wrap the token in the canonical credential-secret envelope JSON."""
    import json
    return json.dumps({
        'format': 'tofu.credential-secret/v1',
        'api_key': api_key,
        'oauth': '',
        'extra_headers': {},
    })


_LOGIN_URL_RE = re.compile(r'https?://\S*?/user/login\?uuid=\S+', re.IGNORECASE)


def cli_installed() -> bool:
    """True when the ``mc`` binary is resolvable on PATH."""
    return shutil.which('mc') is not None


def kick_cli_login(timeout_s: float = 8.0) -> dict:
    """Launch ``mc --code`` briefly to surface the interactive SSO login link.

    The CLI prints a ``<host>/user/login?uuid=<uuid>`` URL then blocks waiting
    for the user to complete SSO in a browser. We cannot complete SSO for the
    user, so we capture the link, leave the CLI running detached (it writes
    the config on success), and return the link so the UI can open it.

    Returns ``{started, installed, login_url}``. Never raises — the card
    degrades to manual instructions when the link cannot be captured.
    """
    if shutil.which('mc') is None:
        return {'started': False, 'installed': False, 'login_url': ''}

    captured: list[str] = []

    def _run() -> None:
        try:
            proc = subprocess.Popen(
                ['mc', '--code'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                start_new_session=True,  # detach: survives this route's lifetime
            )
        except Exception:
            logger.warning('catpaw login kick: failed to spawn mc', exc_info=True)
            return
        deadline = time.monotonic() + timeout_s
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                match = _LOGIN_URL_RE.search(line or '')
                if match:
                    captured.append(match.group(0).rstrip())
                if time.monotonic() > deadline or captured:
                    break
        except Exception:
            logger.warning('catpaw login kick: read failed', exc_info=True)
        # Do NOT kill the process: it must stay alive to receive the SSO
        # callback and write the config once the user completes login.

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout_s + 2.0)
    return {
        'started': True,
        'installed': True,
        'login_url': captured[0] if captured else '',
    }


__all__ = [
    'CATPAW_CLI_CONFIG',
    'CatpawLocalImportError',
    'cli_installed',
    'detect_local_key',
    'kick_cli_login',
    'read_local_key',
    'secret_envelope',
]
