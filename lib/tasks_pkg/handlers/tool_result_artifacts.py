"""Handlers for owner-scoped range/search access to large tool results.

Single-call arguments remain wire-compatible. Native ``reads`` / ``searches``
batches execute through the shared bounded runner, isolate per-item failures,
and return input-ordered results under one aggregate model-view token budget.
Canonical query JSON and decoded program values retain the complete page.
"""

from __future__ import annotations

import json

from lib.log import get_logger
from lib.agent_core.storage_errors import StorageError
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.handlers._adapter import run_batch_concurrent
from lib.tasks_pkg.manager import task_user_id
from lib.tool_results.tool_result_artifacts import (
    ToolResultArtifactRepository, resolve_artifact_ref)
from lib.tools.contracts import ToolContractError
from lib.tools.tool_result_artifacts import (
    TOOL_RESULT_ARTIFACT_CONTRACTS,
    TOOL_RESULT_ARTIFACT_NAMES,
)


_CONTRACTS = {contract.name: contract
              for contract in TOOL_RESULT_ARTIFACT_CONTRACTS}
logger = get_logger(__name__)

#: Bound on media descriptors replayed from one read (model blocks + tiles).
_MAX_MEDIA_REPLAY = 8

_MEDIA_REF_PREFIX = 'tofu-media:'

_MIME_EXTENSIONS = {
    'image/jpeg': 'jpeg', 'image/png': 'png', 'image/gif': 'gif',
    'image/webp': 'webp', 'image/svg+xml': 'svg',
}


def _media_descriptors_of(read_result):
    """``(tofu-media ref, mime)`` pairs carried by one successful read."""
    out = []
    if not isinstance(read_result, dict):
        return out
    for item in read_result.get('media') or ():
        if not isinstance(item, dict):
            continue
        ref = str(item.get('reference') or '')
        if ref.startswith(_MEDIA_REF_PREFIX):
            out.append((ref, str(item.get('mimeType') or 'image/*')))
    return out


def _attachment_url(reference):
    """Same-origin resolvable URL for a retained media attachment."""
    return '/api/v1/media/attachments/' + reference[len(_MEDIA_REF_PREFIX):]


def _mime_ext(mime):
    # Manifest media parts may only know the generic family ('image/*');
    # the extension is a caption hint, the serving route owns Content-Type.
    return _MIME_EXTENSIONS.get(mime, 'img')

#: Stay inside ``budget_tool_result_v2``'s pass-through band. Otherwise a
#: continuation response can itself spill into another artifact pointer.
from lib.tool_results.tool_result_query_contract import CONTRACT, SIGNED_CURSOR_PREFIXES
_RESPONSE_TOKEN_BUDGET = CONTRACT['response']['tokens']
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
        return len(text.encode('utf-8')) if text else 0


def _serialize(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _model_tokens(fn_name: str, text: str, model: str) -> int:
    """Use the common delivery projection; returned/captured JSON stays exact."""
    from lib.tools.result_surfaces import project_model_source
    return _count_tokens(project_model_source(fn_name, text), model)


def _metadata_refusal(result=None):
    identity = {key: result[key] for key in ('index', 'artifactRef', 'query')
                if result is not None and key in result}
    return {**identity, 'status': 'error', 'error': {'code': 'response_metadata_too_large',
            'retryable': False, 'nextAction': 'Use fewer references or a narrower source query.'}}


def _fit_read_result(result, model, token_budget):
    # Opaque cursors cannot be reconstructed after trimming text locally.
    if _model_tokens('read_tool_artifact', _serialize(result), model) <= token_budget:
        return result
    if result.get('resumeCursor') or str(result.get('nextCursor', '')).startswith(SIGNED_CURSOR_PREFIXES):
        return _metadata_refusal(result)
    visible = result.get('content') or ''
    low, high, best = 1, len(visible), None
    while low <= high:
        mid = (low + high) // 2
        prefix = visible[:mid]
        candidate = {**result, 'content': prefix, 'nextCursor': str(int(result.get('offset', 0)) + len(prefix.encode())), 'truncated': True, 'outputTruncated': True}
        if _model_tokens('read_tool_artifact', _serialize(candidate), model) <= token_budget:
            best, low = candidate, mid + 1
        else:
            high = mid - 1
    return best if best is not None else _metadata_refusal(result)


def _fit_search_result(result, model, token_budget):
    if _model_tokens('search_tool_artifact', _serialize(result), model) <= token_budget:
        return result
    # The cursor has consumed every match in this page. Shortening a quotation
    # (or dropping a match) would strand evidence behind that cursor, including
    # the search term itself. Let the caller re-query fewer complete matches.
    return _metadata_refusal(result)


def _fit_one_result(fn_name, result, model, token_budget):
    if fn_name == 'read_tool_artifact':
        return _fit_read_result(result, model, token_budget)
    return _fit_search_result(result, model, token_budget)


def _fit_batch_to_budget(fn_name, results, model):
    wrapper = {'status': 'ok' if all(x.get('status') == 'ok' for x in results) else 'partial_failure', 'items': results}
    text = _serialize(wrapper)
    if _model_tokens(fn_name, text, model) > _RESPONSE_TOKEN_BUDGET:
        share = max(1, (_RESPONSE_TOKEN_BUDGET - 64) // max(1, len(results)))
        fitted = [_fit_one_result(fn_name, item, model, share) for item in results]
        text = _serialize({**wrapper, 'items': fitted, 'status': 'ok' if all(x.get('status') == 'ok' for x in fitted) else 'partial_failure'})
        if _model_tokens(fn_name, text, model) > _RESPONSE_TOKEN_BUDGET:
            return _serialize(_metadata_refusal())
    return text


def _parse_cursor(arguments: dict) -> int | str:

    raw = arguments.get('cursor') or '0'
    if isinstance(raw, str) and raw.startswith(SIGNED_CURSOR_PREFIXES):
        return raw
    try:
        cursor = int(arguments.get('cursor') or 0)
    except (TypeError, ValueError) as exc:
        raise ToolContractError(
            'invalid_cursor', 'cursor must be a non-negative integer or a returned continuation cursor',
            path='$.cursor', retryable=False,
            next_action=CONTRACT['failures']['invalid_cursor']['nextAction']) from exc
    if cursor < 0:
        raise ToolContractError(
            'invalid_cursor', 'cursor must be non-negative', path='$.cursor',
            retryable=False, next_action=CONTRACT['failures']['invalid_cursor']['nextAction'])
    return cursor


def _error_result(exc: Exception) -> dict:
    if isinstance(exc, ToolContractError):
        return {"status": "error", "error": exc.to_dict()}
    if isinstance(exc, StorageError):
        for code, failure in CONTRACT['failures'].items():
            if exc.code == failure['storageCode']:
                return {'status': 'error', 'error': {
                    'code': code, 'retryable': failure['retryable'],
                    'nextAction': failure['nextAction']}}
    logger.warning('[ToolArtifact] owner-scoped read failed: %s', exc, exc_info=True)
    retryable = isinstance(exc, StorageError) and exc.retryable
    return {
        "status": "error",
        "error": {
            "code": "artifact_store_unavailable",
            "retryable": retryable,
            "nextAction": (
                "Retry this read once; do not re-execute the source tool to recover historical evidence."
                if retryable else
                "The artifact store could not complete this read. Continue independent work and report the storage failure; do not repeat the source tool to recover historical evidence."),
        },
    }


def _execute_one(fn_name: str, arguments: dict, *, user_id: int,
                 repository: ToolResultArtifactRepository, model: str = '',
                 token_budget: int = _RESPONSE_TOKEN_BUDGET,
                 task=None) -> dict:
    try:
        cursor = _parse_cursor(arguments)
        # The model quotes the semantic alias from its envelope projection;
        # storage only accepts the canonical digest. A well-formed alias that
        # does not resolve fails loud — never guess at neighbouring evidence.
        raw_ref = str(arguments.get('artifact_ref') or '')
        resolved_ref = resolve_artifact_ref(task, raw_ref, user_id=user_id)
        if resolved_ref is None:
            return {
                "status": "error",
                "artifactRef": raw_ref,
                "error": {
                    "code": "artifact_unavailable",
                    "coverage": "unavailable",
                    "retryable": False,
                    "nextAction": (
                        "Unknown or expired artifact reference. Quote the artifactRef exactly as returned; historical evidence is not recovered by repeating the source tool."),
                },
            }
        if fn_name == 'read_tool_artifact':
            result = repository.read_range(
                user_id=user_id,
                artifact_ref=resolved_ref,
                offset=cursor,
                limit=arguments.get('limit', 8192),

                **({'selector': arguments['selector']} if 'selector' in arguments else {}),
            )
        else:
            result = repository.search(
                user_id=user_id,
                artifact_ref=resolved_ref,
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

                    "coverage": "unavailable",
                    "retryable": False,
                    "nextAction": (
                        "Historical evidence is unavailable. A separately authorized source call is a new observation, not recovery."),
                },
            }
        result = {"status": "ok", **result}
        if resolved_ref != raw_ref:
            # Echo the model's own handle so follow-up ranges keep quoting
            # the alias; nested child refs stay canonical digests.
            result['artifactRef'] = raw_ref
        # Re-query the same immutable cursor with a smaller range/match count.
        # The source owns both exact evidence and its signed continuation.
        source_limit = arguments.get('limit', 8192 if fn_name == 'read_tool_artifact' else 8)
        fitted = _fit_one_result(fn_name, result, model, token_budget)
        if fitted.get('status') != 'ok' and source_limit > 1:
            source_limit = max(1, source_limit // 2)
            return _execute_one(fn_name, {**arguments, 'limit': source_limit},
                                user_id=user_id, repository=repository,
                                model=model, token_budget=token_budget,
                                task=task)
        return fitted
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
                    fn_name, item, user_id=owner, repository=repository, model=model,
                    token_budget=max(128, (_RESPONSE_TOKEN_BUDGET - 256) // len(calls)),
                    task=task),
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
                fn_name, calls[0], user_id=owner, repository=repository,
                model=model, task=task)
            content = _serialize(_fit_one_result(
                fn_name, result, model, _RESPONSE_TOKEN_BUDGET))
            succeeded = result.get('status') == 'ok'
    except Exception as exc:
        logger.debug('tool result artifact execution failed: %s', exc)
        result = _error_result(exc)
        content = _serialize(result)
        succeeded = False
        is_batch = False

    # A successful read can surface retained media descriptors. They ride
    # TWO projections from one collection: canonical media parts (the settle
    # stamp + post-phase projection deliver real image blocks to a vision
    # model — previously the read-back was text-only and the model stayed
    # blind) and ``imageDataUris`` on the row meta (the frontend renders the
    # same images as tiles). Search results are text-only by contract scope.
    media_refs = []
    if fn_name == 'read_tool_artifact' and succeeded:
        if is_batch:
            for item in indexed:
                if item.get('status') == 'ok':
                    media_refs.extend(_media_descriptors_of(item))
        else:
            media_refs = _media_descriptors_of(result)
    media_capped = len(media_refs) > _MAX_MEDIA_REPLAY
    media_refs = media_refs[:_MAX_MEDIA_REPLAY]
    if media_refs:
        from lib.tools.result_value import (
            CapturedToolResult, ResultPart, TrustedResultText)
        # Everything this read returned — the JSON page plus every media
        # descriptor — is retained in the canonical, so retention is
        # 'complete' unless the replay cap dropped descriptors.
        coverage = 'partial' if media_capped else 'complete'
        parts = [ResultPart('json', content)]
        parts.extend(ResultPart('media', reference=ref, mime_type=mime)
                     for ref, mime in media_refs)
        content = TrustedResultText(
            content,
            canonical=CapturedToolResult(
                tuple(parts), retention_coverage=coverage))

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
    if media_refs:
        meta['imageDataUris'] = [
            {'uri': _attachment_url(ref),
             'format': _mime_ext(mime),
             'filename': f'artifact-media-{index + 1}.{_mime_ext(mime)}'}
            for index, (ref, mime) in enumerate(media_refs)]
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, content, False
