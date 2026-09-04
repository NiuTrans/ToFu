"""lib/llm_dispatch/config — Default slot configurations and model aliases.

Contains the static configuration tables that seed the slot pool before
benchmark data is loaded.  These are **reference tables** — they describe
known model capabilities / RPM / cost metadata so that *any* configured
model benefits from pre-seeded data.  They do NOT control which models
are "active" — that is driven entirely by owner-scoped model-routing v2
ProviderAccess and Deployment resources.

This package is a facade split across cohesive sub-modules::

    _pricing.py  — PRICING_TIERS / MANAGED_TIER_TAGS +
                   get_pricing_tiers / is_model_cheap / reevaluate_pricing_tags
    _slots.py    — DEFAULT_SLOT_CONFIGS reference table
    _aliases.py  — MODEL_ALIAS_GROUPS + MODEL_ALIASES lookup map

Every public symbol is re-exported here so the import path is UNCHANGED::

    from lib.llm_dispatch.config import DEFAULT_SLOT_CONFIGS, MODEL_ALIAS_GROUPS
    from lib.llm_dispatch.config import get_pricing_tiers, reevaluate_pricing_tags
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Pricing tiers & resolution ───────────────────────────────
from lib.llm_dispatch.config._pricing import (  # noqa: F401
    PRICING_TIERS,
    MANAGED_TIER_TAGS,
    _NON_CHAT_CAPS,
    _resolve_prices,
    _tier_matches,
    get_pricing_tiers,
    is_model_cheap,
    reevaluate_pricing_tags,
)

# ── Default slot configuration table ─────────────────────────
from lib.llm_dispatch.config._slots import (  # noqa: F401
    DEFAULT_SLOT_CONFIGS,
)

# ── Model alias groups & lookup map ──────────────────────────
from lib.llm_dispatch.config._aliases import (  # noqa: F401
    MODEL_ALIAS_GROUPS,
    MODEL_ALIASES,
)

# Preserve the original module's __all__ verbatim (byte-identical set) so
# `from lib.llm_dispatch.config import *` and lib.llm_dispatch's
# `from .config import *` re-export exactly the same public surface.
__all__ = [
    'DEFAULT_SLOT_CONFIGS',
    'MODEL_ALIASES',
    'MODEL_ALIAS_GROUPS',
    'PRICING_TIERS',
    'MANAGED_TIER_TAGS',
    'is_model_cheap',
    'get_pricing_tiers',
    'reevaluate_pricing_tags',
]
