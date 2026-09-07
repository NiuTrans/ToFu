"""lib/relay_config.py — Single source of truth for relay deployment policy.

Tofu can run in three shapes (see :mod:`lib.auth_mode` for the auth
gate). When the gate is ``multi-user`` the operator is running a
*relay* — serving multiple end-users. This module owns the *policy*
for that relay, persisted at ``data/config/relay.json``::

    {
      "signup_enabled": false,
      "signup_default_role": "user",
      "signup_welcome_credit_micro": 0,
      "billing_enabled": true
    }

The two questions a relay operator answers:

``signup_enabled``
    May the public self-register at ``/api/v1/users/signup``? Default
    ``false`` — the operator onboards users via the admin console or
    redemption codes.

``model_relay_enabled``
    May tenant users invoke the **operator's** model slot pool (the
    ``chat`` scope: ``/api/v1/chat/completions`` + the OpenAI/Anthropic
    compat surfaces)? When ``false`` this is a **BYO-only** deployment —
    users MUST attach their own model endpoint (``/api/v1/providers`` →
    ``agents:run``) and can never fall back to our keys/credits.

    Enforced at TWO layers (defense-in-depth):
      1. **Scope mint** — signup/login/admin-mint never grant ``chat``
         (nor the ``providers`` exception). A BYO-only key physically
         lacks the scope, so ``require_scope('chat')`` 403s it.
      2. **Request gate** — the chat + compat routes also refuse with
         ``model_relay_disabled`` even if a stale pre-flag key still
         carries ``chat``. The scope strip is the primary control; this
         is the belt-and-braces backstop.

    Default ``true`` (full model relay). Setting ``billing_enabled=false``
    does NOT imply this — an operator may run a free model relay, or a
    paid BYO-metering relay; the two flags are independent.

``billing_enabled``
    Does Tofu act as a **paying intermediary** (we supply the model
    key, meter token usage, and charge the user's credit wallet) — or
    is it an **agent-only** relay where each user brings their OWN model
    endpoint (BYO provider, see :mod:`lib.byo_providers`) and we never
    touch money?

    * ``true``  (default) → full relay. Chat requests reserve/settle
      credits; the admin console shows Pricing / Redeem-codes / Payments
      panels; the customer dashboard shows the wallet.
    * ``false`` → agent-only. The billing hot path is a no-op even for
      tenant users (identical to a personal install), the money-moving
      billing routes return 404, and the billing UI panels are hidden.
      Users authenticate + run agents with their own keys.

Default is ``true`` for backward compatibility: existing relay
deployments that predate this flag keep billing on.

Override priority (highest first):
    1. ``TOFU_RELAY_BILLING`` env var (1/0/true/false) — wins over the
       file. Lets CI / containers lock the mode without editing config.
    2. ``data/config/relay.json``.
    3. Defaults above.

This module is a no-op for personal / private installs — it's only
consulted on the multi-user relay paths.
"""

from __future__ import annotations

import os
from typing import Optional

from lib.config_dir import config_path
from lib.json_store import read_json
from lib.log import get_logger

logger = get_logger(__name__)

_STORE_PATH = config_path('relay.json')

_DEFAULTS = {
    'signup_enabled': False,
    'signup_default_role': 'user',
    'signup_welcome_credit_micro': 0,
    'billing_enabled': True,
    'model_relay_enabled': True,
}


def _env_bool(name: str) -> Optional[bool]:
    """Read a tri-state boolean env var. Returns None when unset."""
    raw = os.environ.get(name, '')
    if raw == '':
        return None
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def get_settings() -> dict:
    """Return the merged relay settings (defaults ← file ← env).

    Always returns a dict containing at least the keys in
    :data:`_DEFAULTS`. Reads are cheap (``json_store`` caches by mtime);
    no in-process cache here so an operator editing ``relay.json`` takes
    effect on the next request without a restart.
    """
    out = dict(_DEFAULTS)
    raw = read_json(_STORE_PATH, default=None)
    if isinstance(raw, dict):
        for k in _DEFAULTS:
            if k in raw:
                out[k] = raw[k]
    # Env override for the billing flag (the one most worth locking in a
    # container / CI). Other fields stay file-driven.
    env_billing = _env_bool('TOFU_RELAY_BILLING')
    if env_billing is not None:
        out['billing_enabled'] = env_billing
    env_model = _env_bool('TOFU_RELAY_MODEL')
    if env_model is not None:
        out['model_relay_enabled'] = env_model
    return out


def signup_enabled() -> bool:
    return bool(get_settings().get('signup_enabled'))


def signup_default_role() -> str:
    role = str(get_settings().get('signup_default_role') or 'user')
    return role


def signup_welcome_credit_micro() -> int:
    try:
        return int(get_settings().get('signup_welcome_credit_micro') or 0)
    except (TypeError, ValueError) as e:
        logger.debug('[Relay] bad signup_welcome_credit_micro: %s', e)
        return 0


def billing_enabled() -> bool:
    """True when this relay charges credits (full-relay / intermediary mode).

    False = agent-only relay (users bring their own model keys; Tofu
    never moves money). Defaults to True for backward compatibility.
    """
    return bool(get_settings().get('billing_enabled'))


def model_relay_enabled() -> bool:
    """True when tenant users may invoke the operator's model slot pool.

    False = BYO-only deployment: the ``chat`` scope is withheld at mint
    time and the chat/compat routes refuse, forcing users onto their own
    model endpoint via ``agents:run``. Defaults to True (full model relay).
    """
    return bool(get_settings().get('model_relay_enabled'))


def public_summary() -> dict:
    """The subset safe to expose on the public capabilities surface."""
    s = get_settings()
    return {
        'signup_enabled': bool(s.get('signup_enabled')),
        'billing_enabled': bool(s.get('billing_enabled')),
        'model_relay_enabled': bool(s.get('model_relay_enabled')),
    }


__all__ = [
    'get_settings', 'signup_enabled', 'signup_default_role',
    'signup_welcome_credit_micro', 'billing_enabled', 'model_relay_enabled',
    'public_summary',
]
