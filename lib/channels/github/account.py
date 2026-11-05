"""GitHub channel account: Settings view and commands, token custody (C8).

Responsibility: the owner-facing GitHub account surface used by the HTTP
routes — the view (status, identity, credential name with the vault's
redacted hint, API base, poll schedule, Global Router) and the update
(enable/disable, paste a token or choose an existing credential vault entry,
a GitHub Enterprise Server API base while disabled). Any token is verified
before it is used: ``GET /user`` resolves the identity and ``GET
/notifications`` proves it can read notifications (only a personal access
token (classic) can). A pasted token is stored in the owner-scoped vault
entry ``channel.u<owner>.github.token``; ``lib.credentials_vault`` reserves
``channel.`` entries from agents and generic credential routes. The value is
never returned, logged or written to channel storage.

Entry points: ``account_view``, ``update_account``, ``probe``,
``vault_entry_name``, ``normalize_api_base_url``, ``web_base_url``.
Dependencies: ``ChannelStore``, ``client``, ``lib.credentials_vault``,
``delivery.global_router_binding``, ``supervisor.notify_account_changed``.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from lib.channels.github.adapter import CHANNEL, api_base_url
from lib.channels.github.client import (
    DEFAULT_API_BASE_URL, GitHubAuthError, GitHubClient, GitHubError, GitHubHTTPError,
    GitHubRateLimited, GitHubUnavailable, iso_seconds,
)
from lib.channels.model import ChannelError, session_owns_status
from lib.channels.store import ChannelStore, now_ms

_UPDATE_FIELDS = frozenset({'enabled', 'token', 'credentialName', 'apiBaseUrl'})
_UNSET = object()


def vault_entry_name(owner: int) -> str:
    return f'channel.u{int(owner)}.{CHANNEL}.token'


def normalize_api_base_url(value: Any) -> str:
    if value is None:
        return DEFAULT_API_BASE_URL
    text = str(value).strip().rstrip('/')
    parts = urlsplit(text)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise ChannelError('invalid_update', 'apiBaseUrl must be an https URL, for example '
                                             'https://github.example.com/api/v3')
    return text


def web_base_url(api_base: str) -> str:
    if api_base == DEFAULT_API_BASE_URL:
        return 'https://github.com'
    if api_base.endswith('/api/v3'):
        return api_base[:-len('/api/v3')]
    parts = urlsplit(api_base)
    return f'{parts.scheme}://{parts.netloc}'


def probe(api_base: str, token: str, *, transport=None) -> dict[str, Any]:
    """Verify ``token``: identity from ``GET /user``, notification access, GitHub's poll interval."""
    client = GitHubClient(api_base, lambda: token, transport=transport)
    try:
        user = client.get('/user')
        body = user.json() or {}
        if not isinstance(body.get('id'), int) or not body.get('login'):
            raise ChannelError('identity_unavailable', 'GitHub GET /user returned no account')
        try:
            notifications = client.get('/notifications', params={'per_page': 1})
        except GitHubHTTPError as error:
            raise ChannelError('token_unsupported',
                               'This token cannot read GitHub notifications; use a personal access '
                               'token (classic) with the repo (or notifications) scope') from error
    except GitHubAuthError as error:
        raise ChannelError('auth_failed', 'GitHub rejected the token') from error
    except GitHubRateLimited as error:
        raise ChannelError('rate_limited', f'GitHub rate limit; try again after '
                                           f'{iso_seconds(error.resume_at_ms)}') from error
    except GitHubUnavailable as error:
        raise ChannelError('github_unavailable', str(error)) from error
    except GitHubError as error:
        raise ChannelError('github_unavailable', str(error)) from error
    return {'identity': {'uid': str(body['id']), 'mis': str(body['login']), 'name': str(body.get('name') or '')},
            'pollIntervalS': notifications.poll_interval_s,
            'serverNowMs': notifications.date_ms or user.date_ms}


def _vault_hint(name: str) -> str:
    from lib.credentials_vault import list_entries
    return next((str(entry.get('hint') or '') for entry in list_entries() if entry.get('name') == name), '')


def account_view(owner: int, *, store: ChannelStore | None = None) -> dict[str, Any]:
    """Contract ``GitHubAccountView``."""
    from lib.channels.delivery import RouterUnavailable, global_router_binding
    store = store or ChannelStore(owner)
    account = store.account(CHANNEL) or {}
    status = dict(account.get('status') or {})
    credential = account.get('credential') or None
    poll = account.get('poll') or {}
    try:
        router_id = global_router_binding(store.owner, create=False)['conversationId']
    except RouterUnavailable:
        router_id = None
    api_base = api_base_url(account)
    return {
        'channel': CHANNEL,
        'enabled': bool(account.get('enabled')),
        'identity': account.get('identity') or None,
        'status': {'state': status.get('state') or 'disabled', 'detail': status.get('detail') or '',
                   'updatedAtMs': int(status.get('updatedAtMs') or 0),
                   'connectedAtMs': status.get('connectedAtMs')},
        'credential': None if not credential else {
            'name': str(credential['name']), 'hint': _vault_hint(str(credential['name'])),
            'managed': bool(credential.get('managed'))},
        'apiBaseUrl': api_base,
        'webBaseUrl': web_base_url(api_base),
        'poll': {'intervalS': poll.get('intervalS'), 'lastPollAtMs': poll.get('lastAtMs'),
                 'nextPollAtMs': poll.get('nextAtMs')},
        'routerConversationId': router_id,
    }


def _chosen_credential(owner: int, name: Any) -> tuple[dict[str, Any], str]:
    from lib.credentials_vault import get_entry, normalize_name
    from lib.error_envelope import BadRequest
    try:
        entry = normalize_name(name)
    except BadRequest as error:
        raise ChannelError('invalid_update', str(error)) from error
    own = vault_entry_name(owner)
    if entry.startswith(('skill.', 'channel.')) and entry != own:
        raise ChannelError('invalid_update', 'That credential vault entry is reserved')
    token = get_entry(entry)
    if not token:
        raise ChannelError('credential_missing', f'No credential vault entry named {entry}')
    return {'name': entry, 'managed': entry == own}, token


def update_account(owner: int, update: dict[str, Any], *, store: ChannelStore | None = None,
                   prober=None) -> dict[str, Any]:
    """Apply ``GitHubAccountUpdate``; every token is verified before it is stored or used."""
    store = store or ChannelStore(owner)
    unknown = set(update) - _UPDATE_FIELDS
    if unknown:
        raise ChannelError('invalid_update', f'Unknown fields: {sorted(unknown)}')
    enabled = update.get('enabled')
    if enabled is not None and not isinstance(enabled, bool):
        raise ChannelError('invalid_update', 'enabled must be a boolean')
    prober = prober or probe
    account = store.account(CHANNEL) or {}
    api_base = api_base_url(account)
    if 'apiBaseUrl' in update:
        if account.get('enabled') and enabled is not False:
            raise ChannelError('invalid_update', 'Disable the GitHub account before changing its API base URL')
        api_base = normalize_api_base_url(update['apiBaseUrl'])
    credential: Any = _UNSET
    token = None
    if 'token' in update:
        token = update['token']
        if not isinstance(token, str) or not token.strip() or len(token) > 255 or any(c.isspace() for c in token.strip()):
            raise ChannelError('invalid_update', 'token must be a GitHub personal access token')
        token = token.strip()
        credential = {'name': vault_entry_name(store.owner), 'managed': True}
    elif 'credentialName' in update:
        if update['credentialName'] is None:
            credential = None
        else:
            credential, token = _chosen_credential(store.owner, update['credentialName'])
    if enabled is True and credential is None:
        raise ChannelError('invalid_update', 'The account cannot be enabled without a credential')
    if token is None and (enabled is True or 'apiBaseUrl' in update) and credential is _UNSET:
        current = account.get('credential') or {}
        if current.get('name'):
            from lib.credentials_vault import get_entry
            token = get_entry(str(current['name']))
        if enabled is True and not token:
            raise ChannelError('credential_missing', 'Add a GitHub token before enabling the account')
    probed = prober(api_base, token) if token else None
    if 'token' in update:
        from lib.credentials_vault import set_entry
        set_entry(vault_entry_name(store.owner), token, note=f'GitHub channel token (Tofu owner {store.owner})')
    previous = account.get('credential') or {}
    if (credential is not _UNSET and previous.get('managed')
            and (credential is None or credential['name'] != previous.get('name'))):
        from lib.credentials_vault import delete_entry
        delete_entry(str(previous['name']))
    moment = now_ms()

    def change(value):
        keep_status = session_owns_status(value)
        if 'apiBaseUrl' in update:
            value['apiBaseUrl'] = None if api_base == DEFAULT_API_BASE_URL else api_base
        if credential is not _UNSET:
            value['credential'] = credential
        if probed is not None:
            value['identity'] = probed['identity']
            poll = dict(value.get('poll') or {})
            if probed.get('pollIntervalS') is not None:
                poll['intervalS'] = probed['pollIntervalS']
            poll['lastAtMs'] = moment
            # The probe read /notifications: GitHub's interval applies from now.
            poll['nextAtMs'] = moment + int(poll['intervalS']) * 1000 if poll.get('intervalS') else None
            value['poll'] = poll
        if enabled is True:
            value['enabled'] = True
            if value.get('frontierAtMs') is None:
                # Ingress starts at enable time; earlier activity is not replayed.
                value['frontierAtMs'] = (probed or {}).get('serverNowMs') or moment
            if not keep_status:
                value['status'] = {'state': 'starting', 'detail': '', 'updatedAtMs': moment,
                                   'connectedAtMs': None}
        elif enabled is False or (credential is None and value.get('enabled')):
            value['enabled'] = False
            value['status'] = {'state': 'disabled', 'detail': '', 'updatedAtMs': moment,
                               'connectedAtMs': None}
        elif probed is not None and value.get('enabled') and not keep_status:
            # A verified replacement token clears needs_login; the session restarts.
            value['status'] = {'state': 'starting', 'detail': '', 'updatedAtMs': moment,
                               'connectedAtMs': None}
        return value
    store.update_account(CHANNEL, change)
    from lib.channels.github.supervisor import notify_account_changed
    notify_account_changed(store.owner)
    return account_view(store.owner, store=store)


__all__ = ['account_view', 'normalize_api_base_url', 'probe', 'update_account', 'vault_entry_name',
           'web_base_url']
