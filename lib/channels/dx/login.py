"""Which SSO login session the 大象 launcher uses for an account's login MIS.

Responsibility: map the owner's chosen 大象 MIS (docs/CHANNELS.md C6) to the
launcher child's SSO environment, so every launcher command (``whoami``,
``create_group``, the bridge) logs in as that account. The host's shared
DataFE session is reused when it belongs to that MIS; any other MIS uses the
owner's own session file for it under ``<data>/channels/dx/sso/u<owner>/``.
The SSO adapter (``packages/tofu-mtsso-auth``) reuses an existing session and
requests one 大象 login approval only when there is none; only the DataFE SDK
writes these files. Tofu never opens them: the shared session's MIS and
whether a login needs an approval are the adapter's own answers, computed in
a child process, so no session content enters this process.

Entry points: ``normalize_mis``, ``login_session`` (``LoginSession``),
``approval_required``, ``sso_owner``, ``account_login_mis``, ``identity_bound``.
Dependencies: ``lib.runtime.runtime_paths.data_root``, the repository's
``packages/tofu-mtsso-auth`` source run with this interpreter, the contract's
``Mis`` pattern.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from lib.channels.model import CONTRACT, ChannelError
from lib.log import get_logger

logger = get_logger(__name__)

_MIS = re.compile(CONTRACT['$defs']['Mis']['pattern'])
# Every variable the SSO adapter reads as its MIS target (tofu_mtsso_auth.session):
# two different values make it refuse the configuration, so the launcher child
# carries exactly one.
_MIS_KEYS = ('MTSSO_MIS', 'MLP_MIS', 'LLM_MIS', 'HOPE_USERNAME', 'XUECHENG_MIS')
_SESSION_FILE_KEY = 'MTSSO_SESSION_TOKEN_FILE'
_ADAPTER_SOURCE = Path(__file__).resolve().parents[3] / 'packages' / 'tofu-mtsso-auth' / 'src'
_SHARED_SESSION_MIS = ('import json\n'
                       'from tofu_mtsso_auth import discover_login_mis\n'
                       'print(json.dumps({"mis": discover_login_mis()}))\n')
# The 大象 ticket audience the launcher exchanges (dx-tofu.sh MEITUAN_SSO_TOKEN_XM_XAI).
_DX_AUDIENCE = 'xm-xai'
_SESSION_STATUS = ('import json, sys\n'
                   'from tofu_mtsso_auth import session_status\n'
                   'status = session_status(sys.argv[1], [sys.argv[2]], mis=sys.argv[3])\n'
                   'print(json.dumps({"auth_state": status.get("auth_state")}))\n')
# Adapter auth states in which a business call waits for a 大象 approval: none
# can be reused, or one for that MIS is already outstanding.
_APPROVAL_STATES = frozenset({'login_required', 'pending'})


@dataclass(frozen=True)
class LoginSession:
    """The launcher child's environment for one login MIS.

    ``mis`` is None when no MIS was chosen and the host has no shared session
    to log in with.
    """
    mis: str | None
    environment: dict[str, str]


def normalize_mis(value: Any) -> str:
    """A MIS from owner input: trimmed, lowercase, contract ``Mis``."""
    mis = value.strip().lower() if isinstance(value, str) else ''
    if not _MIS.fullmatch(mis):
        raise ChannelError('invalid_update', 'mis must be a 大象 MIS account such as zhangsan03')
    return mis


def account_login_mis(account: Mapping[str, Any] | None) -> str | None:
    """The MIS an account logs in as: the chosen one, else its bound identity's."""
    account = account or {}
    return account.get('mis') or (account.get('identity') or {}).get('mis') or None


def identity_bound(account: Mapping[str, Any] | None) -> bool:
    """Whether the account's verified identity belongs to its chosen login MIS."""
    account = account or {}
    identity = account.get('identity') or {}
    return bool(identity.get('uid')) and (not account.get('mis') or identity.get('mis') == account.get('mis'))


def sso_owner(owner: int) -> str:
    """The explicit SSO owner of a Tofu owner's launcher commands (``DX_TOFU_OWNER``)."""
    return f'tofu:{int(owner)}'


def owner_session_file(owner: int, mis: str) -> str:
    from lib.runtime.runtime_paths import data_root
    return os.path.join(data_root(), 'channels', 'dx', 'sso', f'u{int(owner)}', f'{mis}.json')


def _private_directory(path: str) -> None:
    # The SDK refuses a session directory that its group or others may write.
    for directory in (os.path.dirname(path), path):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)


def _adapter_answer(source: str, environment: Mapping[str, str], *args: str) -> dict[str, Any]:
    """Run ``source`` against the SSO adapter in a child process; its JSON answer ({} on failure)."""
    env = dict(environment)
    env['PYTHONPATH'] = str(_ADAPTER_SOURCE)
    completed = subprocess.run([sys.executable, '-c', source, *args], cwd=tempfile.gettempdir(),
                               env=env, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if completed.returncode != 0:
        lines = completed.stderr.decode('utf-8', 'replace').strip().splitlines()
        logger.warning('[Channels] dx SSO adapter query failed (exit %s): %s',
                       completed.returncode, lines[-1] if lines else '')
        return {}
    try:
        value = json.loads(completed.stdout)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def shared_session_mis(environment: Mapping[str, str]) -> str:
    """MIS of the host's shared DataFE session ('' when none is usable)."""
    env = {key: value for key, value in environment.items() if key not in _MIS_KEYS}
    mis = _adapter_answer(_SHARED_SESSION_MIS, env).get('mis')
    return mis if isinstance(mis, str) and _MIS.fullmatch(mis) else ''


def approval_required(owner: int, session: LoginSession) -> bool:
    """Whether logging in as ``session.mis`` waits for a 大象 login approval.

    The adapter's ``session_status`` answers without requesting one (SDK
    refresh, official introspection, exchange). An unknown answer is False:
    the launcher command itself then reports what went wrong.
    """
    if session.mis is None:
        return False
    answer = _adapter_answer(_SESSION_STATUS, session.environment, sso_owner(owner), _DX_AUDIENCE, session.mis)
    return answer.get('auth_state') in _APPROVAL_STATES


def login_session(owner: int, mis: str | None, environment: Mapping[str, str]) -> LoginSession:
    """The launcher environment that logs ``owner`` in as ``mis``.

    ``mis`` None selects the host's shared session account. TODO(enterprise):
    the shared session belongs to the host user, yet any owner naming its MIS
    reuses it; a multi-user deployment must restrict that reuse to that user.
    """
    shared = shared_session_mis(environment)
    target = mis or shared or None
    if target is None:
        return LoginSession(None, dict(environment))
    env = {key: value for key, value in environment.items() if key not in _MIS_KEYS}
    env['MTSSO_MIS'] = target
    if target != shared:
        path = owner_session_file(owner, target)
        _private_directory(os.path.dirname(path))
        env[_SESSION_FILE_KEY] = path
    return LoginSession(target, env)


__all__ = ['LoginSession', 'account_login_mis', 'approval_required', 'identity_bound', 'login_session',
           'normalize_mis', 'owner_session_file', 'shared_session_mis', 'sso_owner']
