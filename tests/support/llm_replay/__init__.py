"""tests/support/llm_replay/ — LLM record/replay testing support.

Records real LLM streaming sessions to JSONL fixtures and replays them by
substituting ONLY the three transport functions (``lib.llm.stream_chat``,
``lib.llm.astream.async_stream_chat``, ``lib.llm.chat.chat``) while the real
agent loop, tool pipeline, dispatch layer, and persistence run.  Replay is
the default in scenario tests (keyless, hermetic); live record requires
``TOFU_LLM_REPLAY=record`` + ``TOFU_PARITY_LIVE=1`` and writes
``script.next.jsonl`` next to the fixture — never over the committed
``script.jsonl``.  The recorded normalized requests live in a shared
content-addressed store (``request_store.py``) so drift errors and re-record
diffs show field-level changes (``diff.py``).  See ``tests/AGENTS.md`` § LLM
record/replay.
"""

from __future__ import annotations

import os

from tests.support.llm_replay.diff import (
    ABSENT,
    Difference,
    describe_difference,
    request_differences,
)
from tests.support.llm_replay.normalize import (
    chat_request_proxy,
    normalize_value,
    normalized_field_digests,
    normalized_request_digest,
)
from tests.support.llm_replay.recorder import RECORD_OUTPUT_NAME, Recorder
from tests.support.llm_replay.replayer import ReplaySession, ScenarioDriftError
from tests.support.llm_replay.request_store import (
    REQUEST_STORE_DIRNAME,
    RequestStoreError,
    load_recorded_request,
    stored_request_path,
)
from tests.support.llm_replay.script import (
    ChatStep,
    ErrorStep,
    ScriptError,
    Step,
    StreamStep,
    load_script,
)

__all__ = [
    'ABSENT',
    'ChatStep',
    'Difference',
    'ErrorStep',
    'RECORD_OUTPUT_NAME',
    'REQUEST_STORE_DIRNAME',
    'Recorder',
    'ReplaySession',
    'RequestStoreError',
    'ScenarioDriftError',
    'ScriptError',
    'Step',
    'StreamStep',
    'chat_request_proxy',
    'describe_difference',
    'load_recorded_request',
    'load_script',
    'mode_from_env',
    'normalize_value',
    'normalized_field_digests',
    'normalized_request_digest',
    'request_differences',
    'stored_request_path',
]

_MODE_RECORD = 'record'
_MODE_OFF = 'off'


def mode_from_env(env=None) -> str:
    """Replay-subsystem mode: ``record`` only with BOTH env gates, else off.

    ``TOFU_LLM_REPLAY=record`` alone is not enough — the parity/live gate
    (``TOFU_PARITY_LIVE=1``) proves the operator intended real provider
    traffic, so a stray replay flag on a keyless box can never turn a
    hermetic scenario run into a network call.
    """
    environ = os.environ if env is None else env
    if (environ.get('TOFU_LLM_REPLAY') == _MODE_RECORD
            and environ.get('TOFU_PARITY_LIVE') == '1'):
        return _MODE_RECORD
    return _MODE_OFF
