"""Compatibility facade for the three focused backend role contracts.

New orchestration internals should import ``_role_axes``, ``_role_specs`` or
``_role_personas`` directly. This module keeps historical imports stable for
tests, extensions and rolling consumers.
"""

from lib.orchestration._role_axes import (  # noqa: F401
    EXECUTION_OPTION_ORDER,
    DEFAULT_ROLE_ISOLATION,
    DEFAULT_ROLE_TIER,
    KNOWN_ROLES,
    VALID_EMITS,
    VALID_ISOLATION,
    VALID_SCOPES,
    VALID_TIERS,
    VERIFIER_ROLES,
    _USER_EMIT_ROLES,
    resolve_emits,
    resolve_isolation,
    resolve_scope,
    resolve_tier,
)
from lib.orchestration._role_personas import role_persona  # noqa: F401
from lib.orchestration._role_specs import (  # noqa: F401
    MAX_LIST_ITEM_LEN,
    MAX_LIST_ITEMS,
    MAX_OBJECTIVE_LEN,
    ROLE_PARAM_SCHEMA,
    VALID_PARAM_KINDS,
    _f,
    _GENERIC_ROLE_SCHEMA,
    _objective_field,
    _ROLE_INFRA_KEYS,
    _validate_role_params,
    role_param_schema,
)
