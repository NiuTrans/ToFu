"""Codex-managed login adapter, used locally and by the desktop agent.

Entry: read_local_login. Native Codex discovers its home and owns renewal;
Tofu reads only access tokens, never imports refresh tokens or starts login.
Dependencies: stdlib + codex_command_login. Unsupported stores fail closed.
"""
from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
import stat
import time
import tomllib

_MAX_FILE_BYTES = 1024 * 1024


def _read_private(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
            raise ValueError('invalid credential file')
        if os.name != 'nt' and (info.st_uid != os.getuid()
                                or info.st_mode & 0o022):
            raise ValueError('unsafe credential ownership or permissions')
        data = stream.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            raise ValueError('credential file too large')
        return data


def _claims(token: object) -> dict:
    if not isinstance(token, str) or len(token) > 65536:
        return {}
    try:
        part = token.split('.')[1]
        value = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, UnicodeError, RecursionError):
        return {}


def _read_file_login(*, expected_account_id: str = '', environment=None,
                     home: Path | None = None, command_settings=None,
                     command_verified: bool = False) -> dict:
    """Return a bounded access-only snapshot or a non-secret failure reason.

    JWT fields are unverified metadata, not authorization. Only the fixed
    subscription upstream validates this bearer. No arbitrary path is accepted
    from a bridge command; environment/home are local deployment/test seams.
    """
    environment = os.environ if environment is None else environment
    root = Path(environment.get('CODEX_HOME') or (home or Path.home()) / '.codex')
    if not root.is_absolute():
        return {'shared_login_issue': 'invalid_codex_home'}
    try:
        config = root / 'config.toml'
        settings = tomllib.loads(_read_private(config.resolve(strict=True) if command_verified else config).decode()) if config.exists() else {}
        settings.update(command_settings or {})
        if settings.get('cli_auth_credentials_store', 'file') != 'file':
            return {'shared_login_issue': 'credential_store_unsupported'}
        # Managed auth policies are interpreted by Codex, not bypassed here.
        if settings.get('forced_login_method') not in (None, 'chatgpt'):
            return {'shared_login_issue': 'login_method_restricted'}
        path = root / 'auth.json'
        # Codex itself has selected this home. Resolve local symlink indirection,
        # then still require an owned regular non-writable credential target.
        data = json.loads(_read_private(path.resolve(strict=True) if command_verified else path))
        if not isinstance(data, dict) or data.get('auth_mode') not in (None, 'chatgpt'):
            return {'shared_login_issue': 'not_chatgpt_login'}
        tokens = data.get('tokens')
        if not isinstance(tokens, dict):
            return {'shared_login_issue': 'not_chatgpt_login'}
        access = tokens.get('access_token')
        claims = _claims(access)
        identity = _claims(tokens.get('id_token'))
        auth = claims.get('https://api.openai.com/auth') or {}
        identity_auth = identity.get('https://api.openai.com/auth') or {}
        if not isinstance(auth, dict) or not isinstance(identity_auth, dict):
            raise ValueError('invalid account metadata')
        account = tokens.get('account_id') or auth.get('chatgpt_account_id')
        if not isinstance(account, str) or not account or len(account) > 256:
            raise ValueError('missing account identity')
        for declared in (auth.get('chatgpt_account_id'), identity_auth.get('chatgpt_account_id')):
            if declared and declared != account:
                return {'shared_login_issue': 'account_mismatch'}
        required = settings.get('forced_chatgpt_workspace_id') or expected_account_id
        if (required and required != account) or (expected_account_id and account != expected_account_id):
            return {'shared_login_issue': 'account_changed'}
        expire = claims.get('exp')
        if type(expire) not in (int, float) or not math.isfinite(expire) or expire <= 0:
            raise ValueError('unknown access-token expiry')
        if expire <= time.time():
            return {'shared_login_issue': 'codex_access_expired', 'account_id': account}
        return {'access_token': access, 'account_id': account, 'expire': expire,
                'email': str(identity.get('email') or '')[:320],
                'plan_type': str(auth.get('chatgpt_plan_type') or identity_auth.get('chatgpt_plan_type') or '')[:64],
                'credential_source': 'local_codex', 'shared_login_issue': ''}
    except FileNotFoundError:
        return {'shared_login_issue': 'codex_not_signed_in'}
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        # Never include parser exceptions: they can embed credential contents.
        return {'shared_login_issue': 'codex_credentials_unreadable'}


def read_local_login(*, expected_account_id: str = '', environment=None,
                     home: Path | None = None, refresh: bool = False) -> dict:
    """Automatically follow the installed Codex command, not a guessed path."""
    from lib.oauth import codex_command_login as native
    from lib.oauth.codex_command_login import command_context, renew_command_login
    deadline = time.monotonic() + native._DEADLINE_SECONDS
    context = command_context(environment=environment, home=home, retry_failed=refresh)
    issue = context.get('shared_login_issue')
    if issue:
        # Existing file-only installations keep their explicit local seam when
        # the native command is absent. A present but failing command is not
        # permission to borrow a different account from a stale default file.
        if issue == 'codex_command_missing':
            return _read_file_login(expected_account_id=expected_account_id,
                                    environment=environment, home=home)
        return {'shared_login_issue': issue}
    root = Path(context['codex_home'])
    snapshot = _read_file_login(expected_account_id=expected_account_id,
        environment={'CODEX_HOME': str(root)}, command_settings=context['settings'],
        command_verified=True)
    if refresh and snapshot.get('shared_login_issue') == 'codex_access_expired':
        # A changed/malformed source never authorizes renewal or rebinding.
        renewed = renew_command_login(root, expected_account_id=snapshot['account_id'],
            settings=context['settings'], environment=environment, home=home,
            timeout_seconds=max(0, deadline - time.monotonic()))
        if renewed.get('shared_login_issue'):
            return {'shared_login_issue': renewed['shared_login_issue'],
                    'account_id': snapshot.get('account_id', '')}
        snapshot = _read_file_login(expected_account_id=expected_account_id or snapshot['account_id'],
            environment={'CODEX_HOME': str(root)}, command_settings=renewed['settings'],
            command_verified=True)
    return snapshot
