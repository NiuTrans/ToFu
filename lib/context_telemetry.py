"""Best-effort context/tool telemetry used by cost experiments and benchmarks.

The helpers in this module never affect request construction.  They use local
token counters where possible, fall back to a conservative character estimate,
and keep only bounded numeric/fingerprint metadata on the live task.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

_MAX_ROUND_SNAPSHOTS = 512
PROMPT_PROFILE_EVIDENCE_VERSION = 'tofu.prompt-profile/v1'
TOOL_SCHEMA_EVIDENCE_KEY = '_tool_schema_evidence'


@dataclass(slots=True)
class _ToolSchemaEvidence:
    """Opaque call-local proof; JSON callers cannot forge this sidecar."""

    source_tools: list[Any]
    model: str
    token_count: int
    source_fingerprint: str = ''


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        logger.debug('[ContextTelemetry] JSON serialization fallback: %s', exc)
        return str(value)


def _count_text(value: Any, *, model: str = '') -> int:
    text = value if isinstance(value, str) else _json_text(value)
    if not text:
        return 0
    try:
        from lib.token_counter import count_text
        return max(0, int(count_text(text, model=model, reusable=True)))
    except Exception as exc:
        logger.debug('[ContextTelemetry] token count fallback: %s', exc)
        cjk = sum(1 for ch in text if '\u2e80' <= ch <= '\u9fff')
        return max(1, cjk + (len(text) - cjk + 3) // 4)


def _content_tokens(message: dict, *, model: str = '') -> int:
    content = message.get('content')
    return _count_text(content, model=model)


def tool_schema_tokens(tools: Any, *, model: str = '') -> int:
    return _count_text(tools or [], model=model) if tools else 0


def _precomputed_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def validated_tool_schema_token_count(tools: Any, value: Any) -> int | None:
    """Validate same-call/turn schema evidence; a nonempty surface cannot be 0."""
    schema_tokens = _precomputed_nonnegative_int(value)
    if tools and schema_tokens == 0:
        return None
    return schema_tokens


def build_tool_schema_evidence(
    tools: Any,
    value: Any,
    *,
    model: str,
    source_fingerprint: Any = None,
) -> Any:
    """Seal a nonempty count against an existing request-local schema copy."""
    schema_tokens = validated_tool_schema_token_count(tools, value)
    if not isinstance(tools, list) or not tools or schema_tokens is None:
        return None
    return _ToolSchemaEvidence(
        source_tools=tools,
        model=str(model or ''),
        token_count=schema_tokens,
        source_fingerprint=_validated_schema_fingerprint(source_fingerprint),
    )


def _validated_schema_fingerprint(value: Any) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in '0123456789abcdef'
                   for character in value)):
        return ''
    return value


def _schema_evidence_matches_final_tools(
    evidence: Any,
    final_tools: Any,
) -> bool:
    if not isinstance(evidence, _ToolSchemaEvidence):
        return False
    source_tools = evidence.source_tools
    if (not isinstance(final_tools, list)
            or len(source_tools) != len(final_tools)):
        return False
    return all(final is source
               for source, final in zip(source_tools, final_tools))


def reusable_tool_schema_metrics(
    evidence: Any,
    final_tools: Any,
    *,
    model: str,
) -> tuple[int | None, str | None]:
    """Return trusted count/fingerprint after one ordered identity check."""
    if not _schema_evidence_matches_final_tools(evidence, final_tools):
        return None, None
    token_count = (
        validated_tool_schema_token_count(final_tools, evidence.token_count)
        if evidence.model == str(model or '') else None
    )
    fingerprint = (
        _validated_schema_fingerprint(evidence.source_fingerprint) or None)
    return token_count, fingerprint


def reusable_tool_schema_token_count(
    evidence: Any,
    final_tools: Any,
    *,
    model: str,
) -> int | None:
    """Compatibility projection of the shared schema-evidence validator."""
    return reusable_tool_schema_metrics(
        evidence, final_tools, model=model)[0]


def record_tool_schema_fingerprint(
    evidence: Any,
    final_tools: Any,
    value: Any,
) -> bool:
    """Seal one exact source fingerprint after the final identity check."""
    fingerprint = _validated_schema_fingerprint(value)
    if (not fingerprint
            or not _schema_evidence_matches_final_tools(
                evidence, final_tools)):
        return False
    evidence.source_fingerprint = fingerprint
    return True


def tool_schema_fingerprint_from_evidence(evidence: Any) -> str | None:
    """Read a sealed request-local fingerprint without exposing schema refs."""
    if not isinstance(evidence, _ToolSchemaEvidence):
        return None
    return _validated_schema_fingerprint(
        evidence.source_fingerprint) or None


def tool_result_tokens(
    messages: Any,
    *,
    model: str = '',
    reusable_text_token_counts_by_identity: Any = None,
) -> int:
    """Count final-body tool results, reusing only identical string objects."""
    if not isinstance(messages, list):
        return 0
    reusable_counts = (
        reusable_text_token_counts_by_identity
        if isinstance(reusable_text_token_counts_by_identity, dict)
        else {}
    )
    total = 0
    for message in messages:
        if not isinstance(message, dict) or message.get('role') != 'tool':
            continue
        content = message.get('content')
        reused = (
            _precomputed_nonnegative_int(reusable_counts.get(id(content)))
            if isinstance(content, str) else None
        )
        total += (
            reused if reused is not None
            else _content_tokens(message, model=model)
        )
    return total


def raw_tool_result_tokens(task: dict, *, model: str = '') -> int:
    """Count the pre-compaction tool content retained on task/round metadata."""
    total = 0
    seen: set[str] = set()
    for round_entry in task.get('toolRounds') or ():
        if not isinstance(round_entry, dict):
            continue
        identity = str(round_entry.get('toolCallId') or id(round_entry))
        if identity in seen:
            continue
        seen.add(identity)
        reported = round_entry.get('rawToolTokens')
        if reported is not None:
            try:
                total += max(0, int(reported or 0))
                continue
            except (TypeError, ValueError) as exc:
                logger.debug(
                    '[ContextTelemetry] invalid rawToolTokens ignored: %s', exc)
        content = (round_entry.get('rawToolContent')
                   if round_entry.get('rawToolContent') is not None
                   else round_entry.get('toolContent'))
        total += _count_text(content or '', model=model)
    return total


def stable_prefix_tokens(messages: Any, *, model: str = '') -> int:
    """Count the leading system/developer instruction floor."""
    if not isinstance(messages, list):
        return 0
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get('role') not in ('system', 'developer'):
            break
        total += _content_tokens(message, model=model)
    return total


def prefix_fingerprint(messages: Any) -> str:
    try:
        from lib.tasks_pkg.wire_fingerprint import static_prefix_hash
        return str(static_prefix_hash(messages or []))
    except Exception as exc:
        logger.debug('[ContextTelemetry] canonical prefix hash fallback: %s',
                     exc)
        return hashlib.sha256(
            _json_text(messages or []).encode('utf-8')).hexdigest()[:24]


def build_prompt_profile_evidence(
    *, requested_profile: str, resolved_profile: str, content: str,
    model: str, status: str, reason: str = '',
) -> dict:
    """Build bounded proof of the static prompt contract used by a request."""
    prompt = str(content or '') if status == 'applied' else ''
    return {
        'contractVersion': PROMPT_PROFILE_EVIDENCE_VERSION,
        'requestedProfile': str(requested_profile or ''),
        'resolvedProfile': str(resolved_profile or ''),
        'effectiveProfile': (
            str(resolved_profile or '') if status == 'applied' else ''),
        'status': str(status or ''),
        'reason': str(reason or ''),
        'model': str(model or ''),
        'charCount': len(prompt),
        'tokenCount': _count_text(prompt, model=model) if prompt else 0,
        'sha256': (
            hashlib.sha256(prompt.encode('utf-8')).hexdigest()
            if prompt else ''),
    }


def prompt_profile_evidence_matches(
    evidence: Any, *, expected_profile: str, model: str = '',
) -> bool:
    """Return whether bounded evidence proves one applied prompt profile."""
    if not isinstance(evidence, dict):
        return False

    def _positive_int(value: Any) -> bool:
        if isinstance(value, bool):
            return False
        try:
            return int(value or 0) > 0
        except (TypeError, ValueError, OverflowError):
            return False

    expected = str(expected_profile or '')
    digest = str(evidence.get('sha256') or '')
    return bool(
        expected
        and evidence.get('contractVersion') == PROMPT_PROFILE_EVIDENCE_VERSION
        and evidence.get('status') == 'applied'
        and evidence.get('requestedProfile') == expected
        and evidence.get('resolvedProfile') == expected
        and evidence.get('effectiveProfile') == expected
        and (not model or evidence.get('model') == str(model))
        and _positive_int(evidence.get('charCount'))
        and _positive_int(evidence.get('tokenCount'))
        and len(digest) == 64
        and set(digest) <= set('0123456789abcdef')
    )


def capture_round_context(
    task: dict,
    messages: list,
    tools: Any,
    *,
    round_num: int,
    model: str,
    precomputed_tool_schema_tokens: Any = None,
    reusable_text_token_counts_by_identity: Any = None,
) -> dict:
    """Capture request-side token shape; precomputed hints remain fail-soft."""
    schema_tokens = validated_tool_schema_token_count(
        tools, precomputed_tool_schema_tokens)
    snapshot = {
        'round': int(round_num) + 1,
        'stablePrefixTokens': stable_prefix_tokens(messages, model=model),
        'toolSchemaTokens': (
            schema_tokens if schema_tokens is not None
            else tool_schema_tokens(tools, model=model)),
        'rawToolResultTokens': raw_tool_result_tokens(task, model=model),
        'modelToolResultTokens': tool_result_tokens(
            messages,
            model=model,
            reusable_text_token_counts_by_identity=(
                reusable_text_token_counts_by_identity),
        ),
        'prefixFingerprint': prefix_fingerprint(messages),
    }
    prompt_profile = task.get('_promptProfileV1')
    if isinstance(prompt_profile, dict):
        snapshot['promptProfile'] = dict(prompt_profile)
    rows = task.setdefault('_contextTelemetryRounds', [])
    if isinstance(rows, list):
        rows.append(snapshot)
        if len(rows) > _MAX_ROUND_SNAPSHOTS:
            del rows[:-_MAX_ROUND_SNAPSHOTS]
    return snapshot


def stamp_tool_exposure(task: dict, *, mode: str, available: int,
                        exposed: int, routed_keys: list[str] | None = None,
                        omitted_keys: list[str] | None = None) -> None:
    task['_toolExposureTelemetry'] = {
        'mode': mode,
        'availableTools': max(0, int(available or 0)),
        'exposedTools': max(0, int(exposed or 0)),
        'routedKeys': list(routed_keys or []),
        'omittedKeys': list(omitted_keys or []),
    }


def record_compaction_event(task: dict | None, *, trigger: str,
                            reason: str = '', tokens_before: int = 0,
                            tokens_after: int = 0, archive_id: Any = None,
                            evidence_retained: list[str] | None = None,
                            evidence_lost: list[str] | None = None) -> None:
    if not isinstance(task, dict):
        return
    event = {
        'trigger': str(trigger or ''),
        'reason': str(reason or '')[:500],
        'tokensBefore': max(0, int(tokens_before or 0)),
        'tokensAfter': max(0, int(tokens_after or 0)),
        'tokenCountKind': 'estimated',
        'evidenceRetained': list(evidence_retained or []),
        'evidenceLost': list(evidence_lost or []),
    }
    if archive_id is not None:
        event['archiveId'] = archive_id
    task.setdefault('_contextCompactionEvents', []).append(event)


def record_mcp_search(task: dict, *, misses: int = 0) -> None:
    task['_mcpSearchCount'] = int(task.get('_mcpSearchCount') or 0) + 1
    task['_mcpSearchMissCount'] = (
        int(task.get('_mcpSearchMissCount') or 0) + max(0, int(misses or 0)))


__all__ = [
    'PROMPT_PROFILE_EVIDENCE_VERSION', 'TOOL_SCHEMA_EVIDENCE_KEY',
    'build_prompt_profile_evidence', 'build_tool_schema_evidence',
    'capture_round_context', 'prompt_profile_evidence_matches',
    'prefix_fingerprint',
    'raw_tool_result_tokens', 'record_compaction_event', 'record_mcp_search',
    'record_tool_schema_fingerprint', 'reusable_tool_schema_metrics',
    'reusable_tool_schema_token_count',
    'stable_prefix_tokens', 'stamp_tool_exposure', 'tool_result_tokens',
    'tool_schema_fingerprint_from_evidence', 'tool_schema_tokens',
    'validated_tool_schema_token_count',
]
