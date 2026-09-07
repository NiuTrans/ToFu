"""Module-level dispatch convenience functions (facade).

Implementation is split by semantic lane into the sibling ``_api_*``
modules; every historically importable module-level name — including
the plain module imports (``api.time`` et al., pinned by monkeypatch
specs) — is re-exported here so existing consumers are unaffected.

Usage:
    from lib.llm_dispatch import dispatch_chat, dispatch_stream, smart_chat
"""

import copy as copy
import math as math
import os as os
import sys
import threading as threading
import time as time
import types

from collections import defaultdict as defaultdict
from dataclasses import dataclass as dataclass

from lib.llm.stream_result import (
    ProviderStreamResult as ProviderStreamResult,
    ensure_provider_stream_result as ensure_provider_stream_result,
)
from lib.llm_errors import (
    RateLimitError as RateLimitError,
    is_subscription_quota_error as is_subscription_quota_error,
    parse_subscription_retry_after as parse_subscription_retry_after,
)
from lib.log import get_logger as get_logger

from lib.llm_dispatch.factory import get_dispatcher as get_dispatcher

logger = get_logger(__name__)

from lib.llm_dispatch._api_errors import (
    DispatchNoAdmissibleSlot as DispatchNoAdmissibleSlot,
    DispatchRateLimitBudgetExceeded as DispatchRateLimitBudgetExceeded,
    DispatchSharedContentionDeferred as DispatchSharedContentionDeferred,
    DispatchWaitStatus as DispatchWaitStatus,
)

from lib.llm_dispatch._api_budget import (
    _DEFAULT_429_SATURATION_SECS as _DEFAULT_429_SATURATION_SECS,
    _UNREACHABLE_COOLDOWN as _UNREACHABLE_COOLDOWN,
    _force_oauth_token_refresh as _force_oauth_token_refresh,
    _gateway_outage_budget_secs as _gateway_outage_budget_secs,
    _raise_dispatch_exhausted as _raise_dispatch_exhausted,
    _raise_if_429_attempt_budget_exhausted as _raise_if_429_attempt_budget_exhausted,
    _saturation_budget_secs as _saturation_budget_secs,
    _saturation_escalate as _saturation_escalate,
    _unix_time_ns as _unix_time_ns,
    _validate_429_attempt_budget as _validate_429_attempt_budget,
)

from lib.llm_dispatch._api_contention import (
    _DEFAULT_429_RETRY_DELAY_S as _DEFAULT_429_RETRY_DELAY_S,
    _note_shared_contention_recovered as _note_shared_contention_recovered,
    _shared_contention_admission_decision as _shared_contention_admission_decision,
    _shared_contention_retry_delay as _shared_contention_retry_delay,
    _wait_for_shared_contention_admission as _wait_for_shared_contention_admission,
    _wait_for_shared_contention_admission_async as _wait_for_shared_contention_admission_async,
)

from lib.llm_dispatch._api_hygiene import (
    _AUDITED_SEVERITY_DOWNGRADE as _AUDITED_SEVERITY_DOWNGRADE,
    _audit_severity_downgrade as _audit_severity_downgrade,
    _cool_slot_on_premature_close as _cool_slot_on_premature_close,
)

from lib.llm_dispatch._api_chat import (
    dispatch_chat as dispatch_chat,
    pick_key_for_model as pick_key_for_model,
)

from lib.llm_dispatch._api_stream_state import (
    _StreamRetryState as _StreamRetryState,
    _adapt_stream_body_for_slot as _adapt_stream_body_for_slot,
    _cycling_can_ever_serve as _cycling_can_ever_serve,
    _first_output_callbacks as _first_output_callbacks,
    _readjust_thinking_params as _readjust_thinking_params,
    _settle_stream_result as _settle_stream_result,
    _sleep_and_record_queue_wait as _sleep_and_record_queue_wait,
)

from lib.llm_dispatch._api_stream import (
    async_dispatch_stream as async_dispatch_stream,
    dispatch_stream as dispatch_stream,
)

from lib.llm_dispatch._api_multi import (
    _group_by_capability as _group_by_capability,
    dispatch_fastest as dispatch_fastest,
    dispatch_parallel as dispatch_parallel,
    get_dispatch_status as get_dispatch_status,
    smart_chat as smart_chat,
    smart_chat_batch as smart_chat_batch,
)

__all__ = [
    'DispatchNoAdmissibleSlot',
    'DispatchRateLimitBudgetExceeded',
    'DispatchSharedContentionDeferred',
    'pick_key_for_model',
    'dispatch_chat',
    'dispatch_stream',
    'async_dispatch_stream',
    'dispatch_fastest',
    'dispatch_parallel',
    'get_dispatch_status',
    '_group_by_capability',
    'smart_chat',
    'smart_chat_batch',
]

_SHARD_MODULES = (
    'lib.llm_dispatch._api_errors',
    'lib.llm_dispatch._api_budget',
    'lib.llm_dispatch._api_contention',
    'lib.llm_dispatch._api_hygiene',
    'lib.llm_dispatch._api_chat',
    'lib.llm_dispatch._api_stream_state',
    'lib.llm_dispatch._api_stream',
    'lib.llm_dispatch._api_multi',
)


class _ApiFacadeModule(types.ModuleType):
    """Mirror attribute writes into the shard that owns the name.

    Every dispatch operation historically lived in this module, so suites and
    operator tooling patch seams here (``monkeypatch.setattr(api,
    'get_dispatcher', ...)``, ``api.time``, ``api._audit_severity_downgrade``).
    The shards bind those names as module globals; a facade write must reach
    each shard that owns the name or the pre-split patch behavior would
    silently stop applying.
    """

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for shard_name in _SHARD_MODULES:
            shard = sys.modules.get(shard_name)
            if shard is not None and name in shard.__dict__:
                shard.__dict__[name] = value


sys.modules[__name__].__class__ = _ApiFacadeModule
