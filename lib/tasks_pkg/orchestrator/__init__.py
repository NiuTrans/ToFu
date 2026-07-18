# HOT_PATH — functions in this package are called per-request.
# Prefer logger.debug() over logger.info(). logger.info() is reserved
# for rare, high-signal events (e.g. content-filter injection, per-round diagnostics).
"""Task orchestrator — main run_task loop coordinating LLM calls and tool execution.

Also exposes ``_run_single_turn()`` — a reusable primitive that executes one
full LLM-tool cycle (setup → tool loop → finalization) on an existing task
dict.  ``endpoint.py`` uses it to drive the outer work→review→revise loop.

This module is a **facade-preserving package** (mirrors ``compaction/``):
the ~2800-line monolith was split into cohesive sub-modules, but the import
path ``lib.tasks_pkg.orchestrator`` is UNCHANGED and every symbol a
test/consumer imports (``run_task``, ``build_body``, ``drain_peer_messages_into``,
``_run_single_turn``, and every private helper) is re-exported here verbatim.

Sub-modules
-----------
* ``_finalize`` — post-loop finalization + per-turn helpers
  (``_finalize_and_emit_done``, ``_discard_pretool_prose``,
  ``_check_suspicious_completion``, ``_emit_tool_round_phase``,
  ``_finalize_dangling_tool_rounds``, ``_maybe_auto_retry_turn``,
  ``_maybe_append_sources_footer``, ``_SRC_URL_RE``).
* ``_run`` — the giant ``run_task`` loop, kept as ONE whole function.
* ``_turn`` — ``drain_peer_messages_into`` + ``_run_single_turn``.

REBINDABLE binding (mandatory)
------------------------------
``build_body: BodyBuilder = _build_body_impl`` is a module-level protocol
binding that tests/consumers may reassign
(``lib.tasks_pkg.orchestrator.build_body = my_stub``).  It lives HERE on the
facade, and the loop (``_run.run_task``) + finalizer + turn primitives resolve
it THROUGH this facade at call time (``import lib.tasks_pkg.orchestrator as
_o; _o.build_body(...)``) so a reassignment steers every call site.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.protocols import BodyBuilder

logger = get_logger(__name__)

# ── REBINDABLE protocol binding — the canonical home is the facade ──
#   Consumers may reassign ``orchestrator.build_body``; every caller resolves
#   it through this module object at call time, so the reassignment steers the
#   loop.  Keep this BEFORE importing the sub-modules: they do
#   ``import lib.tasks_pkg.orchestrator as _o`` at import time (binding the
#   partially-initialised module) and read ``_o.build_body`` only at call time.
from lib.llm import build_body as _build_body_impl

build_body: BodyBuilder = _build_body_impl  # type: explicit protocol binding


# ── Sub-module symbols (re-exported verbatim — import path preserved) ──
from lib.tasks_pkg.orchestrator._finalize import (  # noqa: E402,F401
    _discard_pretool_prose,
    _check_suspicious_completion,
    _emit_tool_round_phase,
    _finalize_dangling_tool_rounds,
    _maybe_auto_retry_turn,
    _maybe_append_sources_footer,
    _finalize_and_emit_done,
    _SRC_URL_RE,
    # back-compat re-exports the monolith exposed at module scope:
    _repair_json,
    _compute_write_breakdown,
    _ENVELOPE_MAX_TOKENS,
    _READ_DROP_WASTE_TOKENS,
    _run_commit_round_async,
)

from lib.tasks_pkg.orchestrator._run import run_task  # noqa: E402,F401

from lib.tasks_pkg.orchestrator._turn import (  # noqa: E402,F401
    drain_peer_messages_into,
    _run_single_turn,
)


# ── Module-scope import surface the monolith exposed as ``orchestrator.X`` ──
#   The pre-split module imported these at top level, so consumers/tests that
#   do ``import lib.tasks_pkg.orchestrator as orch; orch.<name>`` kept working.
#   Re-export them from the sub-module namespace so that surface is preserved
#   (``derive_round_modified_files`` is imported directly by some debug tools).
from lib.tasks_pkg.orchestrator._run import (  # noqa: E402,F401
    AbortedError,
    append_event,
    checkpoint_task_partial,
    persist_task_result,
    stream_llm_response,
    _strip_base64_for_snapshot,
    derive_round_modified_files,
    _spawn_async_commit_round,
    _spawn_async_profile_consolidation,
    EventType,
    build_event,
    tool_label,
)
