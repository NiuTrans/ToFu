"""lib.billing.users — User accounts for multi-tenant deployments.

A "user" is the principal that a wallet belongs to. Each user owns N
API keys (see :mod:`lib.api_keys`); every key carries a ``user_id``
field that ties requests to the user's wallet for billing.

Personal/private installs leave the table empty and never call into
this module — the unified auth gate's ``local_admin_context()`` covers
the single-user case.

Authentication
--------------
Two paths, both optional:
  * **Email + password** — bcrypt-hashed, stored locally. Sufficient
    for small relays; pair with email-verify before granting keys.
  * **OIDC / SSO** — looked up via ``metadata.oidc_sub``. Wired in a
    later phase; this module only stores the foreign key.

Roles
-----
Closed enum: ``user`` (default), ``admin`` (full Tofu admin scope).
A future ``ops`` role for read-only support staff is planned but not
implemented.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

from lib.ids import short_id
from lib.log import audit_log, get_logger
from lib.storage import StorageError, get_storage_client

logger = get_logger(__name__)


USER_ROLES = frozenset({'user', 'admin'})
USER_STATUSES = frozenset({'active', 'suspended', 'deleted'})

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


@dataclass(frozen=True)
class User:
    id: str
    email: str
    display_name: str
    role: str
    status: str
    created_at: int
    last_login_at: int
    email_verified: bool
    metadata: dict

    @classmethod
    def from_row(cls, row) -> 'User':
        if hasattr(row, 'keys'):
            md = row['metadata']
        else:
            md = row[9]
        if isinstance(md, dict):
            md_dict = md
        else:
            try:
                md_dict = json.loads(md) if md else {}
            except (json.JSONDecodeError, TypeError) as e:
                logger.debug('[Users] Malformed metadata JSON, defaulting: %s', e)
                md_dict = {}
        if hasattr(row, 'keys'):
            return cls(
                id=row['id'], email=row['email'],
                display_name=row['display_name'] or '',
                role=row['role'], status=row['status'],
                created_at=int(row['created_at']),
                last_login_at=int(row['last_login_at']),
                email_verified=bool(row['email_verified']),
                metadata=md_dict,
            )
        return cls(
            id=row[0], email=row[1],
            display_name=row[3] or '', role=row[4], status=row[5],
            created_at=int(row[6]), last_login_at=int(row[7]),
            email_verified=bool(row[8]), metadata=md_dict,
        )


# ── Password hashing ─────────────────────────────────────────────────
# bcrypt is the right answer; we degrade to PBKDF2-SHA256 if the
# operator hasn't installed `bcrypt` so the relay still boots cleanly.
# Both paths produce a self-describing hash string ("scheme$rest").

def _hash_password(plaintext: str) -> str:
    if not plaintext:
        return ''
    try:
        import bcrypt  # type: ignore
        h = bcrypt.hashpw(plaintext.encode('utf-8'),
                          bcrypt.gensalt(rounds=12))
        return 'bcrypt$' + h.decode('utf-8')
    except ImportError as e:
        logger.debug('[Users] bcrypt unavailable, using pbkdf2 fallback: %s', e)
        salt = os.urandom(16)
        dk = hashlib.pbkdf2_hmac('sha256',
                                 plaintext.encode('utf-8'),
                                 salt, 200_000)
        return 'pbkdf2$' + salt.hex() + '$' + dk.hex()


def _verify_password(plaintext: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        scheme, rest = stored.split('$', 1)
    except ValueError as e:
        logger.debug('[Users] Malformed password hash (no scheme prefix): %s', e)
        return False
    if scheme == 'bcrypt':
        try:
            import bcrypt  # type: ignore
            return bcrypt.checkpw(plaintext.encode('utf-8'),
                                  rest.encode('utf-8'))
        except (ImportError, ValueError) as e:
            logger.debug('[Users] bcrypt verify failed: %s', e)
            return False
    if scheme == 'pbkdf2':
        try:
            salt_hex, dk_hex = rest.split('$', 1)
        except ValueError as e:
            logger.debug('[Users] Malformed pbkdf2 hash: %s', e)
            return False
        dk = hashlib.pbkdf2_hmac('sha256',
                                 plaintext.encode('utf-8'),
                                 bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    logger.warning('[Users] Unknown password scheme: %s', scheme)
    return False


# ── CRUD ─────────────────────────────────────────────────────────────

def _new_user_id() -> str:
    return short_id('usr_')


def create_user(
    email: str,
    *,
    password: str = '',
    display_name: str = '',
    role: str = 'user',
    metadata: Optional[dict] = None,
) -> User:
    """Create a user row. Raises ``ValueError`` on bad input or
    duplicate email.
    """
    email = (email or '').strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError(f'Invalid email: {email!r}')
    if role not in USER_ROLES:
        raise ValueError(f'Invalid role: {role!r}')
    user_id = _new_user_id()
    now = int(time.time())
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError('metadata must be an object')
    password_hash = _hash_password(password)
    try:
        document = get_storage_client(write=True).command(
            'tenant.user.create', {
                'user_id': user_id, 'email': email,
                'password_hash': password_hash,
                'display_name': display_name or '', 'role': role,
                'metadata': metadata or {}, 'created_at': now,
            }, f'tenant.user.create:{user_id}',
        )
    except StorageError as exc:
        if exc.code == 'database_conflict':
            raise ValueError('email already registered') from None
        raise
    audit_log('user_created', user_id=user_id, role=role)
    logger.info('[Users] created user_id=%s role=%s', user_id, role)
    return User.from_row(document)


def get_user(user_id: str) -> Optional[User]:
    if not user_id:
        return None
    document = get_storage_client().query(
        'tenant.user.get', {'user_id': user_id})
    return User.from_row(document) if document is not None else None


def find_user(*, email: str = '') -> Optional[User]:
    if not email:
        return None
    document = get_storage_client().query(
        'tenant.user.get', {'email': (email or '').strip().lower()})
    return User.from_row(document) if document is not None else None


def list_users(*, limit: int = 100, offset: int = 0,
               status: str = '') -> List[User]:
    if status:
        if status not in USER_STATUSES:
            raise ValueError(f'Invalid status: {status!r}')
    rows = get_storage_client().query('tenant.user.list', {
        'limit': max(1, min(int(limit), 1000)),
        'offset': max(0, int(offset)), 'status': status,
    })
    return [User.from_row(row) for row in rows]


def set_user_status(user_id: str, status: str) -> User:
    if status not in USER_STATUSES:
        raise ValueError(f'Invalid status: {status!r}')
    document = get_storage_client(write=True).command(
        'tenant.user.set_status', {'user_id': user_id, 'status': status},
        f'tenant.user.set_status:{uuid.uuid4().hex}',
    )
    if document is None:
        raise ValueError(f'No such user: {user_id}')
    audit_log('user_status_changed', user_id=user_id, status=status)
    return User.from_row(document)


def update_user_role(user_id: str, role: str) -> User:
    if role not in USER_ROLES:
        raise ValueError(f'Invalid role: {role!r}')
    document = get_storage_client(write=True).command(
        'tenant.user.set_role', {'user_id': user_id, 'role': role},
        f'tenant.user.set_role:{uuid.uuid4().hex}',
    )
    if document is None:
        raise ValueError(f'No such user: {user_id}')
    audit_log('user_role_changed', user_id=user_id, role=role)
    return User.from_row(document)


def authenticate(email: str, password: str) -> Optional[User]:
    """Return the user iff the password matches and the account is active."""
    normalized_email = (email or '').strip().lower()
    if not normalized_email:
        return None
    material = get_storage_client().query(
        'tenant.user.authentication', {'email': normalized_email})
    if material is None:
        return None
    user = User.from_row(material['user'])
    if user is None or user.status != 'active':
        return None
    if not _verify_password(password, material['password_hash']):
        email_fingerprint = hashlib.sha256(
            normalized_email.encode('utf-8')).hexdigest()[:16]
        audit_log('user_login_failed', email_fingerprint=email_fingerprint)
        return None
    get_storage_client(write=True).command(
        'tenant.user.record_login', {
            'user_id': user.id, 'last_login_at': int(time.time()),
        }, f'tenant.user.record_login:{uuid.uuid4().hex}',
    )
    audit_log('user_login_ok', user_id=user.id)
    return user


__all__ = [
    'User', 'USER_ROLES', 'USER_STATUSES',
    'create_user', 'get_user', 'find_user', 'list_users',
    'set_user_status', 'update_user_role', 'authenticate',
]
