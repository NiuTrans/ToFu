"""Handlers for owner-scoped range/search access to large tool results."""

from __future__ import annotations

import json

from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.manager import task_user_id
from lib.log import get_logger
from lib.tool_result_artifacts import ToolResultArtifactRepository
from lib.tools.contracts import ToolContractError
from lib.tools.tool_result_artifacts import (
    TOOL_RESULT_ARTIFACT_CONTRACTS,
    TOOL_RESULT_ARTIFACT_NAMES,
)


_CONTRACTS = {contract.name: contract
              for contract in TOOL_RESULT_ARTIFACT_CONTRACTS}
logger = get_logger(__name__)

#: A continuation read must come back at a size the dispatch pipeline can
#: pass through untouched. ``budget_tool_result_v2`` re-artifacts ANY result
#: over ``TOOL_RESULT_V2_MAX_TOKENS - 1_000`` (compaction._budget), so an
#: 8k+-token read response was replaced by ANOTHER artifactRef pointer — the
#: model then had to read the pointer to read the pointer, and the UI showed
#: a COMPACTED L0 row on the very tool whose job is bounded continuation.
#: Clamping here, at the layer that owns the cursor contract, keeps every
#: response inside the pass-through band so ``nextCursor`` is the ONLY
#: continuation mechanism. Keep <= TOOL_RESULT_V2_MAX_TOKENS - 1_000 (a test
#: pins the alignment).
_RESPONSE_TOKEN_BUDGET = 7_000


def _count_tokens(text: str, model: str) -> int:
    try:
        from lib.token_counter import count_text
        return max(0, int(count_text(text, model=model or '')))
    except Exception as exc:
        logger.debug('[ToolArtifact] token counter fallback: %s', exc)
        return max(1, (len(text) + 3) // 4) if text else 0


def _serialize(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _fit_to_budget(result: dict, model: str) -> str:
    """Shrink an over-budget read response by shortening its content field.

    Only ``read_tool_artifact`` responses can overflow (search results are
    capped at a handful of small items). The next cursor is the byte offset
    of the returned prefix — a char prefix always ends on a UTF-8 boundary,
    and byte offsets are the repository's cursor unit — so concatenating
    continuations still reconstructs the artifact exactly.
    """
    text = _serialize(result)
    if _count_tokens(text, model) <= _RESPONSE_TOKEN_BUDGET:
        return text
    visible = result.get('content')
    if not isinstance(visible, str) or not visible:
        return text
    offset = int(result.get('offset') or 0)
    low, high, best = 0, len(visible) - 1, None
    while low <= high:
        mid = (low + high) // 2
        prefix = visible[:mid]
        candidate = dict(result)
        candidate['content'] = prefix
        candidate['truncated'] = True
        candidate['nextCursor'] = str(offset + len(prefix.encode('utf-8')))
        text = _serialize(candidate)
        if _count_tokens(text, model) <= _RESPONSE_TOKEN_BUDGET:
            best = text
            low = mid + 1
        else:
            high = mid - 1
    if best is not None:
        return best
    stub = dict(result)
    stub['content'] = ''
    stub['truncated'] = True
    stub['nextCursor'] = str(offset)
    return _serialize(stub)


@tool_registry.tool_set(
    TOOL_RESULT_ARTIFACT_NAMES,
    category="artifacts",
    description="Bounded continuation for large tool results",
)
def _handle_tool_result_artifact(task, tc, fn_name, tc_id, fn_args, rn,
                                 round_entry, cfg, project_path,
                                 project_enabled, all_tools=None):
    contract = _CONTRACTS[fn_name]
    raw_arguments = fn_args if isinstance(fn_args, dict) else {}
    try:
        arguments = contract.validate_arguments(fn_args)
        try:
            cursor = int(arguments.get("cursor") or 0)
        except (TypeError, ValueError) as exc:
            raise ToolContractError(
                "invalid_cursor", "cursor must be a non-negative integer",
                path="$.cursor") from exc
        if cursor < 0:
            raise ToolContractError(
                "invalid_cursor", "cursor must be non-negative", path="$.cursor")
        repository = ToolResultArtifactRepository()
        if fn_name == "read_tool_artifact":
            result = repository.read_range(
                user_id=task_user_id(task),
                artifact_ref=arguments["artifact_ref"],
                offset=cursor,
                limit=arguments.get("limit", 8192),
            )
        else:
            result = repository.search(
                user_id=task_user_id(task),
                artifact_ref=arguments["artifact_ref"],
                query=arguments["query"],
                cursor=cursor,
                limit=arguments.get("limit", 8),
            )
        if result is None:
            result = {
                "status": "error",
                "error": {
                    "code": "artifact_unavailable",
                    "retryable": False,
                    "nextAction": (
                        "Re-run the source tool if this evidence is required."),
                },
            }
        else:
            result = {"status": "ok", **result}
    except ToolContractError as exc:
        result = {"status": "error", "error": exc.to_dict()}
    except Exception as exc:
        logger.warning(
            '[ToolArtifact] owner-scoped read failed: %s', exc, exc_info=True)
        result = {
            "status": "error",
            "error": {
                "code": "artifact_store_unavailable",
                "retryable": True,
                "nextAction": "Retry once, then re-run the source tool.",
            },
        }
    if result.get("status") == "ok":
        content = _fit_to_budget(
            result, task.get('model', '') if isinstance(task, dict) else '')
    else:
        content = _serialize(result)
    meta = _build_simple_meta(
        fn_name, content, source="Tool Result Artifact",
        title=str(raw_arguments.get("artifact_ref") or "")[:96],
        snippet=("bounded artifact result" if result.get("status") == "ok"
                 else "artifact read failed"),
        # No success badge: the row's generic token badge already reports
        # what this read cost, and the old "📎 evidence" label was contract
        # jargon that answered none of the user's questions.
        badge=("" if result.get("status") == "ok" else "❌ failed"),
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, content, False
