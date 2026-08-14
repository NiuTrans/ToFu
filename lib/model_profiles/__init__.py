"""Evidence-backed model profiles and conservative role selection.

This package is deliberately separate from
:mod:`lib.model_info.capability_taxonomy`: taxonomy answers whether a model is
chat/non-chat, while profiles describe relative quality, evidence, cost and
role suitability.
"""

from lib.log import get_logger

logger = get_logger(__name__)

from lib.model_profiles._catalog import configured_model_profiles  # noqa: E402,F401
from lib.model_profiles._profile import (  # noqa: E402,F401
    build_model_profile,
    infer_model_family,
)
from lib.model_profiles._selection import (  # noqa: E402,F401
    select_model_for_tier,
)

__all__ = [
    'build_model_profile',
    'configured_model_profiles',
    'infer_model_family',
    'select_model_for_tier',
]
