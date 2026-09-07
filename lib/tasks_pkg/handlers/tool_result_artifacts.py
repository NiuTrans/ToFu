"""Handlers for owner-scoped range/search access to large tool results.

Single-call arguments remain wire-compatible. Native ``reads`` / ``searches``
batches execute through the shared bounded runner, isolate per-item failures,
and return input-ordered results under one aggregate token budget.
"""

from __future__ import annotations

import json

from lib.log import get_logger
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.handlers._adapter import run_batch_concurrent
from lib.tasks_pkg.manager import task_user_id
from lib.tool_result_artifacts import ToolResultArtifactRepository
from lib.tools.contracts import ToolContractError
from lib.tools.tool_result_artifacts import (
    TOOL_RESULT_ARTIFACT_CONTRACTS,
    TOOL_RESULT_ARTIFACT_NAMES,
)


_CONTRACTS = {contract.name: contract
              for contract in TOOL_RESULT_ARTIFACT_CONTRACTS}
logger = get_logger(__name__)

#: Stay inside ``budget_tool_result_v2``'s pass-through band. Otherwise a
#: continuation response can itself spill into another artifact pointer.
_RESPONSE_TOKEN_BUDGET = 7_000
_MAX_BATCH_WORKERS = 4
_BATCH_KEYS = {
    "read_tool_artifact": "reads",
    "search_tool_artifact": "searches",
}


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


def _fit_read_result(result: dict, model: str, token_budget: int) -> dict:
    """Fit one range while keeping its cursor aligned to the visible prefix."""
    if _count_tokens(_serialize(result), model) <= token_budget:
        return result
    visible = result.get('content')
    if not isinstance(visible, str) or not visible:
        return result
    offset = int(result.get('offset') or 0)
    low, high, best = 0, len(visible) - 1, None
    while low <= high:
        mid = (low + high) // 2
        candidate = dict(result)
        prefix = visible[:mid]
        candidate['content'] = prefix
        candidate['truncated'] = True
        candidate['nextCursor'] = str(offset + len(prefix.encode('utf-8')))
        candidate['outputTruncated'] = True
        if _count_tokens(_serialize(candidate), model) <= token_budget:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    if best is not None:
        return best
    stub = dict(result)
    stub['content'] = ''
    stub['truncated'] = True
    stub['nextCursor'] = str(offset)
    stub['outputTruncated'] = True
    return stub


def _fit_search_result(result: dict, model: str, token_budget: int) -> dict:
    """Fairly shorten match excerpts without changing the search cursor."""
    if _count_tokens(_serialize(result), model) <= token_budget:
        return result
    matches = result.get('items')
    if not isinstance(matches, list) or not matches:
        return result
    texts = [str(item.get('text') or '') if isinstance(item, dict) else ''
             for item in matches]
    low, high, best = 0, max(map(len, texts), default=0), None
    while low <= high:
        cap = (low + high) // 2
        candidate = dict(result)
        candidate['items'] = [
            {**item, 'text': texts[index][:cap]}
            if isinstance(item, dict) else item
            for index, item in enumerate(matches)
        ]
        candidate['outputTruncated'] = True
        if _count_tokens(_serialize(candidate), model) <= token_budget:
            best = candidate
            low = cap + 1
        else:
            high = cap - 1
    if best is not None:
        return best
    stub = dict(result)
    stub['items'] = [
        {key: value for key, value in item.items() if key != 'text'}
        if isinstance(item, dict) else item
        for item in matches
    ]
    stub['outputTruncated'] = True
    return stub


def _fit_one_result(fn_name: str, result: dict, model: str,
                    token_budget: int) -> dict:
    if result.get('status') != 'ok':
        return result
    if fn_name == 'read_tool_artifact':
        return _fit_read_result(result, model, token_budget)
    return _fit_search_result(result, model, token_budget)


def _fit_batch_to_budget(fn_name: str, results: list[dict], model: str) -> str:
    """Preserve every item identity/cursor, then split body budget fairly."""
    wrapper = {"status": (
        "ok" if all(item.get('status') == 'ok' for item in results)
        else "partial_failure"), "items": results}
    text = _serialize(wrapper)
    if _count_tokens(text, model) <= _RESPONSE_TOKEN_BUDGET:
        return text

    identity = []
    body_keys = {'content'} if fn_name == 'read_tool_artifact' else {'items'}
    for item in results:
        stub = {key: value for key, value in item.items() if key not in body_keys}
        if fn_name == 'read_tool_artifact' and item.get('status') == 'ok':
            stub.update(
                content='',
                nextCursor=str(int(item.get('offset') or 0)),
                truncated=True,
                outputTruncated=True,
            )
        elif fn_name == 'search_tool_artifact' and item.get('status') == 'ok':
            stub.update(items=[], outputTruncated=True)
        identity.append(stub)
    identity_tokens = _count_tokens(_serialize({**wrapper, 'items': identity}), model)
    share = max(128, (_RESPONSE_TOKEN_BUDGET - identity_tokens) // max(1, len(results)))
    fitted = []
    for index, item in enumerate(results):
        item_identity_tokens = _count_tokens(_serialize(identity[index]), model)
        fitted.append(_fit_one_result(
            fn_name, item, model, item_identity_tokens + share))
    text = _serialize({**wrapper, 'items': fitted})
    if _count_tokens(text, model) <= _RESPONSE_TOKEN_BUDGET:
        return text
    return _serialize({**wrapper, 'items': identity})


def _parse_cursor(arguments: dict) -> int:
    try:
        cursor = int(arguments.get('cursor') or 0)
    except (TypeError, ValueError) as exc:
        raise ToolContractError(
            'invalid_cursor', 'cursor must be a non-negative integer',
            path='$.cursor') from exc
    if cursor < 0:
        raise ToolContractError(
            'invalid_cursor', 'cursor must be non-negative', path='$.cursor')
    return cursor


def _error_result(exc: Exception) -> dict:
    if isinstance(exc, ToolContractError):
        return {"status": "error", "error": exc.to_dict()}
    logger.warning('[ToolArtifact] owner-scoped read failed: %s', exc, exc_info=True)
    return {
        "status": "error",
        "error": {
            "code": "artifact_store_unavailable",
            "retryable": True,
            "nextAction": "Retry once, then re-run the source tool.",
        },
    }


def _execute_one(fn_name: str, arguments: dict, *, user_id: int,
                 repository: ToolResultArtifactRepository) -> dict:
    try:
        cursor = _parse_cursor(arguments)
        if fn_name == 'read_tool_artifact':
            result = repository.read_range(
                user_id=user_id,
                artifact_ref=arguments['artifact_ref'],
                offset=cursor,
                limit=arguments.get('limit', 8192),
            )
        else:
            result = repository.search(
                user_id=user_id,
                artifact_ref=arguments['artifact_ref'],
                query=arguments['query'],
                cursor=cursor,
                limit=arguments.get('limit', 8),
            )
        if result is None:
            return {
                "status": "error",
                "artifactRef": arguments.get('artifact_ref'),
                "error": {
                    "code": "artifact_unavailable",
                    "retryable": False,
                    "nextAction": (
                        "Re-run the source tool if this evidence is required."),
                },
            }
        return {"status": "ok", **result}
    except Exception as exc:
        failed = _error_result(exc)
        failed['artifactRef'] = arguments.get('artifact_ref')
        if fn_name == 'search_tool_artifact':
            failed['query'] = arguments.get('query')
        return failed


def _normalize_calls(fn_name: str, arguments: dict) -> tuple[list[dict], bool]:
    batch_key = _BATCH_KEYS[fn_name]
    batch = arguments.get(batch_key)
    if isinstance(batch, list) and batch:
        return batch, True
    required = ('artifact_ref', 'query') if fn_name == 'search_tool_artifact' \
        else ('artifact_ref',)
    missing = [key for key in required if not arguments.get(key)]
    if missing:
        raise ToolContractError(
            'missing_required_arguments',
            'Missing required arguments: ' + ', '.join(missing), path='$')
    return [arguments], False


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
    model = task.get('model', '') if isinstance(task, dict) else ''
    try:
        arguments = contract.validate_arguments(fn_args)
        calls, is_batch = _normalize_calls(fn_name, arguments)
        owner = task_user_id(task)
        repository = ToolResultArtifactRepository()
        if is_batch:
            results = run_batch_concurrent(
                calls,
                lambda item: _execute_one(
                    fn_name, item, user_id=owner, repository=repository),
                max_workers=_MAX_BATCH_WORKERS,
                tag='ToolArtifact',
            )
            indexed = [{"index": index, **(result or {
                "status": "error",
                "error": {"code": "artifact_store_unavailable",
                          "retryable": True},
            })} for index, result in enumerate(results)]
            content = _fit_batch_to_budget(fn_name, indexed, model)
            succeeded = all(item.get('status') == 'ok' for item in indexed)
        else:
            result = _execute_one(
                fn_name, calls[0], user_id=owner, repository=repository)
            content = _serialize(_fit_one_result(
                fn_name, result, model, _RESPONSE_TOKEN_BUDGET))
            succeeded = result.get('status') == 'ok'
    except Exception as exc:
        logger.debug('tool result artifact execution failed: %s', exc)
        result = _error_result(exc)
        content = _serialize(result)
        succeeded = False
        is_batch = False

    batch_key = _BATCH_KEYS[fn_name]
    batch_size = len(raw_arguments.get(batch_key) or [])
    title = (f'{batch_size} artifact operations' if batch_size
             else str(raw_arguments.get('artifact_ref') or '')[:96])
    meta = _build_simple_meta(
        fn_name, content, source='Tool Result Artifact', title=title,
        snippet=('bounded artifact results' if succeeded
                 else 'artifact read partially or fully failed'),
        badge=('' if succeeded else '❌ failed'),
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, content, False
