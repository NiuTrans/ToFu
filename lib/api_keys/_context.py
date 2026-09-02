"""lib/api_keys/_context.py — AuthContext + scope vocabulary.

Defines the closed scope enum (:data:`ALL_SCOPES`), the resolved
per-request auth state (:class:`AuthContext`), the synthetic open-mode
principal (:func:`local_admin_context`), and scope normalisation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lib.identity import PERSONAL_USER_ID, require_user_id
from lib.log import get_logger

logger = get_logger(__name__)

# Closed scope vocabulary. Adding a new scope must always go through
# this list — a route asking for an unknown scope is a programming bug
# and ``require_scope`` raises at registration time.
ALL_SCOPES = frozenset({
    'chat',           # /api/v1/chat/completions, /v1/chat/completions
    'tasks',          # /api/v1/tasks/* (start, poll, abort)
    'conversations',  # /api/v1/conversations/*
    'files',          # /api/v1/files/* (uploads, attachments)
    'providers',      # /api/v1/providers/* (BYO model endpoint CRUD)
    'agents:paper',
    'agents:translate',
    'agents:swarm',
    'agents:scheduler',
    'agents:memory',
    'agents:browser',
    'agents:bridge',  # X-Bridge-Secret per-user token (RWA P4a, poll auth)
    'agents:search',
    'agents:trading',
    'agents:image',
    'agents:mcp',
    'agents:run',     # /api/v1/agent/run (single-call agent runtime)
    'webhooks',       # /api/v1/webhooks/*
    'capabilities',   # /api/v1/capabilities (read-only, public-by-default)
    'usage',          # /api/v1/usage (per-key analytics)
    'admin',          # full surface, includes /api/v1/keys/*
})

# 'admin' implicitly grants every scope below it.
_ADMIN_SCOPE = 'admin'


@dataclass
class AuthContext:
    """Resolved auth state for the current request.

    ``account_user_id`` is the opaque account subject shown by account APIs.
    ``owner_user_id`` is the positive integer used by repositories.  They are
    deliberately separate and must never be coerced into one another.

    ``via_open_mode`` is True for requests that came through the auth
    gate while ``lib.auth_mode`` reports ``mode=open`` — there is no
    real credential, but downstream ``require_scope`` decorators must
    keep working uniformly, so the synthetic context is tagged with
    full admin scope (see :func:`local_admin_context`).
    """
    key_id: str = ''
    name: str = ''
    scopes: frozenset = field(default_factory=frozenset)
    rate_limit_rpm: int = 0
    rate_limit_tpd: int = 0
    via_open_mode: bool = False
    owner_user_id: int | None = None
    account_user_id: str = ''
    tenant_id: str | None = None  # evolution seam; no tenant product behavior yet

    def __post_init__(self) -> None:
        if self.owner_user_id is not None:
            self.owner_user_id = require_user_id(
                self.owner_user_id, context='authentication owner')
        self.account_user_id = str(self.account_user_id or '').strip()
        if len(self.account_user_id) > 256:
            raise ValueError('account_user_id must be at most 256 characters')
        if self.account_user_id and self.owner_user_id is None:
            raise ValueError('account identity requires owner_user_id')
        normalized_tenant = str(self.tenant_id or '').strip()
        if len(normalized_tenant) > 256:
            raise ValueError('tenant_id must be at most 256 characters')
        self.tenant_id = normalized_tenant or None

    def has_scope(self, scope: str) -> bool:
        if self.via_open_mode:
            return True
        if _ADMIN_SCOPE in self.scopes:
            return True
        return scope in self.scopes

    @property
    def is_authenticated(self) -> bool:
        return bool(self.key_id) or self.via_open_mode


def local_admin_context() -> 'AuthContext':
    """Synthetic full-privilege context for ``mode=open`` requests.

    The key_id is the literal ``'local'`` so usage tracking, idempotency
    keys, and audit logs share one stable principal across an open-mode
    deployment without needing a persisted credential row.
    """
    return AuthContext(
        key_id='local', name='local',
        scopes=frozenset({_ADMIN_SCOPE}),
        rate_limit_rpm=0, rate_limit_tpd=0,
        via_open_mode=True,
        owner_user_id=PERSONAL_USER_ID,
    )


def _normalise_scopes(scopes: list) -> frozenset:
    cleaned = set()
    for s in scopes or ():
        if not isinstance(s, str):
            continue
        s = s.strip()
        if not s:
            continue
        if s == _ADMIN_SCOPE or s in ALL_SCOPES:
            cleaned.add(s)
        else:
            logger.warning('[ApiKeys] dropping unknown scope %r', s)
    return frozenset(cleaned)
