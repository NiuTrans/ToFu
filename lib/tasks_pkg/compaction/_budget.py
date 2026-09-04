"""Layer 0 — per-tool budgeting for tool results.

Three public entry points:

  * ``budget_tool_result`` — single-result wrapper called from
    ``lib/tasks_pkg/tool_dispatch/_pipeline.py`` when a tool result enters the
    context.  Persists oversized results to disk via ``_persist_to_disk``
    instead of irreversibly truncating.
  * ``enforce_round_aggregate_budget`` — round-level guard against
    parallel tool-call explosion (10 × 30 KB grep results would still
    swallow the context window even though each one is under its
    per-tool cap).
  * ``mark_empty_result`` — empty-string protector that prevents the
    model from misreading a blank tool result as conversation end.

Imports nothing from sibling sub-modules except ``_constants`` and
``_persist``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import (
    _BUDGET_EXEMPT_TOOLS,
    _DEFAULT_TOOL_RESULT_MAX,
    _SINGLE_RESULT_HARD_CEILING_CHARS,
    MAX_ROUND_TOOL_RESULTS_CHARS,
    TOOL_RESULT_MAX_CHARS,
)
from lib.tasks_pkg.compaction._persist import _human_size, _persist_to_disk

logger = get_logger(__name__)

TOOL_RESULT_V2_MAX_TOKENS = 8_000
ROUND_TOOL_RESULT_V2_MAX_TOKENS = 24_000
# Reconstructible source reads may use the otherwise-idle remainder of the
# round budget. Parallel siblings are still reduced by the aggregate guard.
SOURCE_TOOL_RESULT_V2_MAX_TOKENS = ROUND_TOOL_RESULT_V2_MAX_TOKENS

_SUMMARY_TRUNCATION_MARKER = "\n… [summary truncated]"
_ARTIFACT_CONTINUATION_MARKER = (
    "\n… [truncated; continue with read_tool_artifact using the envelope's "
    "artifactRef as artifact_ref and its cursor]"
)
_NARROWER_QUERY_MARKER = (
    "\n… [truncated; rerun the source tool with a narrower query or range]"
)


#: Longest run of uninterrupted non-whitespace that still looks like prose or
#: source. Real text wraps: even minified JS and long log lines carry spaces or
#: newlines every few hundred chars. A base64 payload or a mis-decoded binary
#: has no such structure, so a single enormous unbroken run is the signal that
#: separates "opaque blob leaked into the text stream" from "the model just
#: read a lot of files".
_OPAQUE_RUN_CHARS = 4_000

#: Sampled window (head) used for the shape test — the decision must not cost
#: a full scan of a multi-megabyte string on the clamp hot path.
_OPAQUE_SAMPLE_CHARS = 200_000


def _looks_like_opaque_blob(content: str) -> bool:
    """True when an oversized result looks like binary/base64, not text.

    Decides WHICH hard-ceiling message the model receives. Deliberately
    shape-based rather than tool-based: a blob can leak through any tool, and
    any tool can legitimately return a lot of text, so the tool NAME cannot
    tell the two apart.

    Judged on the head sample only (bounded work), by the longest run of
    non-whitespace: text of any kind — source, logs, markdown, CJK prose —
    breaks up long before :data:`_OPAQUE_RUN_CHARS`, while base64 and decoded
    binary do not.
    """
    sample = content[:_OPAQUE_SAMPLE_CHARS]
    longest = 0
    run = 0
    for ch in sample:
        if ch.isspace():
            run = 0
            continue
        run += 1
        if run > longest:
            longest = run
            if longest >= _OPAQUE_RUN_CHARS:
                return True
    return False


def clamp_tool_result_text(tool_name: str, content: str,
                           tc_id: str = '', conv_id: str = '') -> str:
    """Tool-agnostic hard ceiling on a single tool-result text (Layer 2).

    The LAST line of defence before a tool result is committed to the
    message stream.  Unlike :func:`budget_tool_result` (Layer 0), this has
    NO per-tool exemptions and NO disk-persist escape hatch — it simply
    refuses to let any single result exceed
    ``_SINGLE_RESULT_HARD_CEILING_CHARS`` chars of text, full stop.

    Its job is to make the "opaque blob floods the context" bug CLASS
    unrepresentable: even if a future ingress point (a new tool, a
    mis-routed binary read, a str()'d image dict) sneaks an oversized blob
    past every earlier layer, it gets head+tail-clamped here into a
    degraded-but-survivable result instead of a fatal context overflow.

    ``__screenshot__`` dicts and other non-str content are passed through
    untouched — images ride the native ``image_url`` protocol and never
    enter the text stream this guards.

    Args:
        tool_name:  Tool that produced the result (for the log + marker).
        content:    Candidate tool-result text.
        tc_id:      Tool-call id (for the log line).
        conv_id:    Conversation id (for the log line).

    Returns:
        ``content`` unchanged if within the ceiling, else a head+tail
        clamp with an explanatory middle marker.
    """
    if not isinstance(content, str):
        return content
    if len(content) <= _SINGLE_RESULT_HARD_CEILING_CHARS:
        return content

    original_len = len(content)
    ceiling = _SINGLE_RESULT_HARD_CEILING_CHARS
    head_budget = int(ceiling * 0.70)
    tail_budget = int(ceiling * 0.25)
    head = content[:head_budget]
    tail = content[-tail_budget:]
    elided = original_len - head_budget - tail_budget

    # WHY two messages: the ceiling fires for two UNRELATED causes, and telling
    # the model the wrong one is worse than saying nothing. A binary/base64
    # leak is a defect the model should report; a 20-file batch read is a
    # perfectly legal request that merely asked for too much at once. The old
    # single message accused every oversized result of leaking binary data, so
    # a legitimate batch read got a false diagnosis of its own behaviour.
    if _looks_like_opaque_blob(content):
        marker = (
            f'\n\n... [⚠ {elided:,} chars elided by hard ceiling — this single '
            f'"{tool_name}" result was {original_len:,} chars (> {ceiling:,} cap). '
            f'This usually means binary/base64 data leaked into a text result. '
            f'Re-read a specific line range or file instead.] ...\n\n'
        )
        logger.error('[HardCeiling] %s result %s exceeded single-result ceiling '
                     '%s — clamped (tc=%s conv=%s). Investigate: opaque blob in '
                     'text stream.',
                     tool_name, _human_size(original_len), _human_size(ceiling),
                     tc_id[:8] if tc_id else '?', conv_id[:8] if conv_id else '?')
    else:
        marker = (
            f'\n\n... [{elided:,} chars elided — this "{tool_name}" call '
            f'returned {original_len:,} chars, over the {ceiling:,}-char '
            f'single-result limit. The content above and below is intact; the '
            f'middle was dropped. Request less at once: fewer paths per call, '
            f'or a specific range via start_line/end_line.] ...\n\n'
        )
        logger.info('[HardCeiling] %s returned %s (> %s ceiling) — clamped '
                    '(tc=%s conv=%s). Text-shaped, so this is an oversized '
                    'legitimate read, not a blob leak.',
                    tool_name, _human_size(original_len), _human_size(ceiling),
                    tc_id[:8] if tc_id else '?', conv_id[:8] if conv_id else '?')
    return head + marker + tail


def budget_tool_result(tool_name: str, content: str,
                       tool_use_id: str = '', conv_id: str = '') -> str:
    """Budget a tool result — persist to disk or pass through.

    For exempt tools (read_files): always pass through unchanged.
    These tools have their own internal limits and truncating them is
    counterproductive (the model would just re-call).

    For other tools: if the content exceeds the per-tool budget, persist
    the full content to disk and return a preview + file path.  The model
    can later use read_files to access the full content.

    Args:
        tool_name:   Name of the tool that produced the result.
        content:     Raw result string.
        tool_use_id: Tool call ID (for persistence filename).
        conv_id:     Conversation ID (for persistence directory).

    Returns:
        Original content if within budget or exempt, or persisted
        preview+path string.
    """
    if not isinstance(content, str):
        return content

    if tool_name in _BUDGET_EXEMPT_TOOLS:
        return content

    max_chars = TOOL_RESULT_MAX_CHARS.get(tool_name, _DEFAULT_TOOL_RESULT_MAX)
    if len(content) <= max_chars:
        return content

    return _persist_to_disk(content, tool_name, tool_use_id, conv_id)


def _result_tokens(content: str, model: str) -> int:
    try:
        from lib.token_counter import count_text
        return max(0, int(count_text(content, model=model or "")))
    except Exception as exc:
        logger.debug("[ToolResultV2] token counter fallback: %s", exc)
        return max(1, (len(content) + 3) // 4) if content else 0


def _model_result_tokens(content: str, model: str) -> int:
    """Count only bytes that will actually enter the provider message."""
    from lib.tools.result_envelope import model_text_from_tool_result

    return _result_tokens(model_text_from_tool_result(content), model)


def _fit_summary(text: str, *, token_budget: int, model: str,
                 marker: str = _SUMMARY_TRUNCATION_MARKER) -> str:
    if _result_tokens(text, model) <= token_budget:
        return text
    low, high, best = 0, len(text), marker
    while low <= high:
        middle = (low + high) // 2
        candidate = text[:middle].rstrip() + marker
        if _result_tokens(candidate, model) <= token_budget:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _candidate_items(content: str) -> tuple[str, list[Any], bool]:
    """Return bounded structured preview plus whether raw items were omitted."""
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return content, [], False
    if isinstance(value, dict):
        raw_items = value.get("items")
        items = list(raw_items[:64]) if isinstance(raw_items, list) else []
        omitted = isinstance(raw_items, list) and len(raw_items) > len(items)
        summary = next((str(value.get(key) or "") for key in (
            "summary", "message", "notice") if value.get(key)), "")
        if not summary and isinstance(raw_items, list):
            summary = f"Tool returned {len(raw_items)} items."
        if isinstance(raw_items, list):
            return summary or content, items, omitted
        # A generic structured result (notably read_tool_artifact) often has a
        # terse ``status=ok`` plus the useful payload in sibling fields.  The
        # old projection promoted only ``status`` to the summary and silently
        # discarded those fields.  Keep the complete bounded object as one
        # item; _materialize_envelope will drop it only when an artifactRef has
        # already made the oversized source recoverable.
        return summary or "Tool returned a structured result.", [value], False
    if isinstance(value, list):
        items = list(value[:64])
        return f"Tool returned {len(value)} items.", items, len(value) > len(items)
    return content, [], False


def _normalized_recovery_policy(value: Any) -> str:
    policy = str(value or "artifact").strip().lower()
    return policy if policy in {"artifact", "source", "none"} else "artifact"


def _bounded_source_arguments(
    tool_name: str,
    tool_arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a bounded, immediately executable source-recovery query.

    ``read_files`` handlers annotate specs with ``_base``/``_display_path`` in
    place. Those values are execution internals and can be very long; source
    recovery retains only the public path/range contract and at most 20 reads.
    A broad original read is narrowed to a shared 600-line slice so replaying
    ``recovery.arguments`` cannot reproduce the overflow loop that made the
    recovery necessary. Other future source-recoverable tools get a
    JSON-bounded public copy.
    """
    arguments = tool_arguments if isinstance(tool_arguments, Mapping) else {}
    if tool_name == "read_files":
        raw_reads = arguments.get("reads")
        single = raw_reads is None and arguments.get("path") is not None
        if single:
            raw_reads = [arguments]
        if not isinstance(raw_reads, list):
            return {}
        public_reads = [raw for raw in raw_reads[:20]
                        if isinstance(raw, Mapping)
                        and raw.get("path") is not None]
        if not public_reads:
            return {}
        lines_per_read = max(40, 600 // len(public_reads))
        reads: list[dict[str, Any]] = []
        for raw in public_reads:
            item: dict[str, Any] = {"path": str(raw.get("path") or "")[:1024]}
            raw_start = raw.get("start_line")
            start_line = (raw_start if isinstance(raw_start, int)
                          and not isinstance(raw_start, bool)
                          and raw_start > 0 else 1)
            raw_end = raw.get("end_line")
            requested_end = (raw_end if isinstance(raw_end, int)
                             and not isinstance(raw_end, bool)
                             and raw_end > 0 else None)
            # The read_files handler repairs a reversed range by swapping it
            # and then reads that swapped window. Mirror the swap here so the
            # recovery slice stays inside the window that was actually read;
            # otherwise the narrowed end drifts to start+lines_per_read past
            # the real evidence (start=2900,end=1010 read 1010-2900 but
            # recovered 2900-3499).
            if (requested_end is not None and start_line > 1
                    and requested_end < start_line):
                start_line, requested_end = requested_end, start_line
            item["start_line"] = start_line
            item["end_line"] = min(
                requested_end if requested_end is not None
                else start_line + lines_per_read - 1,
                start_line + lines_per_read - 1,
            )
            reads.append(item)
        if single and len(reads) == 1:
            return reads[0]
        return {"reads": reads} if reads else {}

    try:
        encoded = json.dumps(
            dict(arguments), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str)
    except Exception as e:
        logger.debug('tool argument budgeting serialization failed: %s', e)
        return {}
    if len(encoded.encode("utf-8")) > 8_192:
        return {}
    try:
        decoded = json.loads(encoded)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _source_recovery(
    tool_name: str,
    tool_arguments: Mapping[str, Any] | None,
):
    from lib.tools.result_envelope import ToolResultRecoveryV2

    return ToolResultRecoveryV2(
        kind="source", tool=str(tool_name or "source_tool"),
        arguments=_bounded_source_arguments(tool_name, tool_arguments),
    )


def _materialize_envelope(*, status: str, summary: str, items: list[Any],
                          artifact_ref: str, truncated: bool, raw_bytes: int,
                          observed_at_ms: int, world_version: str,
                          evidence_id: str, model: str,
                          recovery=None,
                          token_budget: int = TOOL_RESULT_V2_MAX_TOKENS) -> str:
    from lib.tools.result_envelope import ToolResultEnvelopeV2

    recovery_marker = (_ARTIFACT_CONTINUATION_MARKER if artifact_ref
                       else _NARROWER_QUERY_MARKER)

    def _render(candidate_summary: str, candidate_items: list[Any],
                candidate_truncated: bool) -> str:
        visible_status = (
            "partial" if candidate_truncated and status == "ok" else status)
        return ToolResultEnvelopeV2(
            status=visible_status, summary=candidate_summary,
            items=tuple(candidate_items), artifact_ref=artifact_ref,
            cursor="0" if artifact_ref else "",
            truncated=candidate_truncated,
            raw_bytes=raw_bytes, visible_bytes=0,
            observed_at_ms=observed_at_ms, world_version=world_version,
            evidence_id=evidence_id, recovery=recovery,
        ).with_visible_bytes().to_envelope_text()

    # Preserve a complete result whenever its final model projection fits.
    # A fixed summary/item split is only a fallback allocation, not permission
    # to discard data that already fits inside the public 8k contract.
    if not truncated:
        complete = _render(summary, items, False)
        if _model_result_tokens(complete, model) <= token_budget:
            return complete

    accepted: list[Any] = []
    summary_budget = (token_budget // 2 if items else token_budget - 500)
    fitted_summary = _fit_summary(
        summary, token_budget=max(256, summary_budget), model=model,
        marker=recovery_marker)
    truncated = truncated or fitted_summary != summary
    for item in items:
        candidate = _render(
            fitted_summary, [*accepted, item], truncated)
        if _model_result_tokens(candidate, model) > token_budget:
            truncated = True
            break
        accepted.append(item)
    final_truncated = truncated or len(accepted) < len(items)
    visible = _render(fitted_summary, accepted, final_truncated)
    if _model_result_tokens(visible, model) <= token_budget:
        return visible

    # The summary budget above is only a first-pass allocation. JSON string
    # escaping is content-dependent for structured projections. Fit against
    # the final model-visible value, preserving the largest deterministic
    # prefix and every item that can coexist with it.
    remaining_items = list(accepted)
    while True:
        low, high = 0, len(summary)
        best_visible = ""
        while low <= high:
            middle = (low + high) // 2
            candidate_summary = (
                summary if middle == len(summary)
                else summary[:middle].rstrip() + recovery_marker)
            candidate = _render(candidate_summary, remaining_items, True)
            if _model_result_tokens(candidate, model) <= token_budget:
                best_visible = candidate
                low = middle + 1
            else:
                high = middle - 1
        if best_visible:
            return best_visible
        if remaining_items:
            remaining_items.pop()
            continue
        # Envelope metadata plus the bounded marker is deliberately tiny. This
        # is defensive for a pathological token counter, and still guarantees
        # a deterministic bounded result instead of unbounded recursion.
        return _render("Result truncated.", [], True)


def _materialize_file_read_envelope(
    *,
    status: str,
    summary: str,
    items: list[Any],
    artifact_ref: str,
    raw_bytes: int,
    observed_at_ms: int,
    world_version: str,
    evidence_id: str,
    model: str,
    max_preview_chars: int | None = None,
    recovery=None,
    token_budget: int = TOOL_RESULT_V2_MAX_TOKENS,
) -> str:
    """Fit every file identity first, then share preview space max-min fairly.

    A batch read is one tool result but several independent observations.  The
    generic item fitter admits items in order and may therefore let one large
    first file evict every later file.  This specialized projection keeps all
    identities/statuses and binary-searches one equal per-item preview cap;
    short previews return their unused share naturally.
    """
    from lib.tools.result_envelope import ToolResultEnvelopeV2

    source_items = [dict(item) for item in items if isinstance(item, dict)][:64]
    fitted_summary = _fit_summary(summary, token_budget=768, model=model)

    def _clip_path(value: Any, limit: int) -> str:
        text = str(value or "?")
        if len(text) <= limit:
            return text
        head = max(1, (limit - 1) // 2)
        return text[:head] + "…" + text[-(limit - head - 1):]

    def _render(preview_cap: int, *, path_limit: int,
                include_details: bool) -> ToolResultEnvelopeV2:
        visible_items: list[dict[str, Any]] = []
        for position, source in enumerate(source_items, 1):
            preview = str(source.get("preview") or "")
            prefix = preview[:max(0, preview_cap)]
            item: dict[str, Any] = {
                "type": "file_read/v1",
                "index": position,
                "path": _clip_path(source.get("path"), path_limit),
                "status": str(source.get("status") or "unknown")[:16],
            }
            if include_details:
                requested_range = source.get("requestedRange")
                if isinstance(requested_range, dict) and requested_range:
                    item["requestedRange"] = requested_range
                try:
                    item["rawBytes"] = max(
                        0, int(source.get("rawBytes") or 0))
                except (TypeError, ValueError):
                    pass
                if source.get("media"):
                    item["media"] = str(source.get("media"))[:32]
            if prefix:
                item["preview"] = prefix
            item["previewTruncated"] = bool(
                source.get("previewTruncated") or len(prefix) < len(preview))
            visible_items.append(item)

        return ToolResultEnvelopeV2(
            status=status,
            summary=fitted_summary,
            items=tuple(visible_items),
            artifact_ref=artifact_ref,
            cursor="0" if artifact_ref else "",
            truncated=True,
            raw_bytes=raw_bytes,
            visible_bytes=0,
            observed_at_ms=observed_at_ms,
            world_version=world_version,
            evidence_id=evidence_id,
            recovery=recovery,
        ).with_visible_bytes()

    # Identity/status is non-negotiable. Progressive fallbacks only remove
    # optional detail and shorten pathological paths; at the read_files schema
    # maximum of 20 items the last form remains far below either token ceiling.
    minimal = _render(0, path_limit=384, include_details=True)
    if _result_tokens(minimal.to_model_text(), model) > token_budget:
        minimal = _render(0, path_limit=160, include_details=False)
    if _result_tokens(minimal.to_model_text(), model) > token_budget:
        fitted_summary = _fit_summary(summary, token_budget=128, model=model)
        minimal = _render(0, path_limit=48, include_details=False)
    if _result_tokens(minimal.to_model_text(), model) > token_budget:
        # Defensive last form for malformed non-schema callers. It still keeps
        # one identity/status row per input instead of reverting to prefix bias.
        fitted_summary = "Batch read result; re-read named files as needed."
        minimal = _render(0, path_limit=24, include_details=False)

    maximum_source_preview = max(
        (len(str(item.get("preview") or "")) for item in source_items),
        default=0)
    high = maximum_source_preview
    if max_preview_chars is not None:
        high = min(high, max(0, int(max_preview_chars)))
    if high <= 0:
        return minimal.to_envelope_text()

    best = minimal
    low = 1
    while low <= high:
        middle = (low + high) // 2
        candidate = _render(middle, path_limit=384, include_details=True)
        if _result_tokens(candidate.to_model_text(), model) \
                <= token_budget:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best.to_envelope_text()


def _store_tool_result_artifact(content: str, *, user_id: int,
                                observed_at_ms: int) -> str:
    if int(user_id or 0) <= 0:
        return ""
    try:
        from lib.tool_result_artifacts import ToolResultArtifactRepository
        effective_now = int(observed_at_ms or 0)
        result = ToolResultArtifactRepository().put(
            user_id=int(user_id), content=content,
            now_ms=effective_now if effective_now > 0 else None)
        return str((result or {}).get("artifactRef") or "")
    except Exception as exc:
        logger.warning("[ToolResultV2] artifact persistence failed: %s", exc)
        return ""


def budget_tool_result_v2(tool_name: str, content: str, *, user_id: int,
                          model: str = "", observed_at_ms: int = 0,
                          world_version: str = "",
                          tool_arguments: dict[str, Any] | None = None,
                          projection_items: list[Any] | None = None,
                          producer_metadata: Mapping[str, Any] | None = None,
                          recovery_policy: str = "artifact") -> str:
    """Return one bounded envelope under the tool's declared recovery policy.

    Volatile results retain the conservative 8k+artifact contract. Cheaply
    reconstructible source reads may consume the full 24k round allowance so a
    single useful read does not spill while 16k tokens sit idle; the aggregate
    guard still enforces the same round ceiling when siblings run in parallel.
    """
    recovery_policy = _normalized_recovery_policy(recovery_policy)
    token_budget = (
        SOURCE_TOOL_RESULT_V2_MAX_TOKENS
        if recovery_policy == "source" else TOOL_RESULT_V2_MAX_TOKENS)
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, default=str)
    # Tool gateways and nested runtimes may already have applied this exact
    # contract.  Re-wrapping would hide artifactRef/cursor/freshness inside a
    # new envelope and make continuation impossible.  Valid V2 output is
    # already guaranteed to fit the single-result budget, so preserve it
    # byte-for-byte (cache stability is part of the contract).
    try:
        existing = json.loads(content)
    except (TypeError, ValueError):
        existing = None
    if (isinstance(existing, dict)
            and existing.get("contractVersion") == "tofu.tool-result/v2"
            and _model_result_tokens(content, model) <= token_budget):
        return content
    raw = content.encode("utf-8", errors="replace")
    evidence_id = "ev_" + hashlib.sha256(raw).hexdigest()[:24]
    raw_result_tokens = _result_tokens(content, model)
    exceeds_inline_budget = raw_result_tokens > token_budget
    preserve_file_identities = False
    summary, items, structurally_truncated = _candidate_items(content)

    # Structured projections can differ in size from their source JSON. Detect
    # that before deciding whether a durable recovery artifact is necessary.
    # The probe is limited to already-small raw values, so it cannot duplicate
    # a multi-megabyte result on the hot path.
    if not exceeds_inline_budget:
        from lib.tools.result_envelope import ToolResultEnvelopeV2
        inline_probe = ToolResultEnvelopeV2(
            status="ok", summary=summary, items=tuple(items),
            raw_bytes=len(raw), observed_at_ms=max(
                0, int(observed_at_ms or 0)),
            world_version=str(world_version or ""),
            evidence_id=evidence_id,
        ).with_visible_bytes().to_envelope_text()
        exceeds_inline_budget = (
            _model_result_tokens(inline_probe, model) > token_budget)

    if tool_name == "read_files" and exceeds_inline_budget:
        from lib.tools.result_projection import (
            normalize_file_read_projection_items,
        )
        file_items = normalize_file_read_projection_items(
            projection_items, tool_arguments)
        if len(file_items) > (0 if recovery_policy == "source" else 1):
            items = file_items
            summary = (
                f"read_files returned {len(file_items)} file results. Every "
                "file is represented in items with a bounded preview; "
                + (
                    "rerun read_files using recovery.arguments with narrower "
                    "per-file ranges for omitted source evidence."
                    if recovery_policy == "source" else
                    "use read_tool_artifact with artifactRef as artifact_ref "
                    "and cursor for the complete batch."
                )
            )
            structurally_truncated = True
            preserve_file_identities = True
    # Structured results also need an honest recovery declaration whenever the
    # finite item preview omits raw entries. Internal envelope metadata itself
    # consumes no model budget.
    producer_truncated = bool(
        isinstance(producer_metadata, Mapping)
        and (producer_metadata.get("truncated")
             or producer_metadata.get("status") == "partial"))
    envelope_overflow = exceeds_inline_budget or structurally_truncated
    needs_recovery = envelope_overflow or producer_truncated
    artifact_ref = (_store_tool_result_artifact(
        content, user_id=user_id, observed_at_ms=observed_at_ms)
        if envelope_overflow and recovery_policy == "artifact" else "")
    # A producer-side cap means the missing bytes never reached this boundary,
    # so persisting the already-truncated string cannot recover them. Point the
    # model back to the source tool even when its normal overflow policy is an
    # artifact.
    recovery = (
        _source_recovery(tool_name, tool_arguments)
        if producer_truncated
        or (envelope_overflow and recovery_policy == "source")
        else None)
    if needs_recovery and not artifact_ref and recovery is None:
        if preserve_file_identities:
            summary = (
                f"read_files returned {len(items)} file results; every file is "
                "listed with a bounded preview. Full result unavailable; "
                "rerun with narrower per-file ranges."
            )
        else:
            summary = (
                _fit_summary(
                    summary, token_budget=5_000, model=model,
                    marker=_NARROWER_QUERY_MARKER)
                + "\n[Full result unavailable; rerun with a narrower query.]"
            )
    if preserve_file_identities:
        return _materialize_file_read_envelope(
            status="partial",
            summary=summary,
            items=items,
            artifact_ref=artifact_ref,
            raw_bytes=len(raw),
            observed_at_ms=max(0, int(observed_at_ms or 0)),
            world_version=str(world_version or ""),
            evidence_id=evidence_id,
            model=model,
            recovery=recovery,
            token_budget=token_budget,
        )
    return _materialize_envelope(
        status="partial" if needs_recovery else "ok",
        summary=summary,
        items=items,
        artifact_ref=artifact_ref,
        truncated=needs_recovery,
        raw_bytes=len(raw),
        observed_at_ms=max(0, int(observed_at_ms or 0)),
        world_version=str(world_version or ""),
        evidence_id=evidence_id,
        model=model,
        recovery=recovery,
        token_budget=token_budget,
    )


def enforce_round_aggregate_budget(
    tool_results: dict[str, tuple[str, str, str]],
    conv_id: str = '',
) -> dict[str, tuple[str, str, str]]:
    """Enforce per-round aggregate budget on tool results.

    If the total chars of all tool results in one round exceed
    MAX_ROUND_TOOL_RESULTS_CHARS, persist the largest non-exempt results
    to disk until under budget.

    Args:
        tool_results: dict of tc_id → (content, tool_name, tool_use_id)
        conv_id:      Conversation ID for persistence directory.

    Returns:
        Updated tool_results dict (modified in place and returned).
    """
    total_chars = sum(
        len(content) for content, _, _ in tool_results.values()
        if isinstance(content, str)
    )

    if total_chars <= MAX_ROUND_TOOL_RESULTS_CHARS:
        return tool_results

    logger.info('[AggregateBudget] Round total %s exceeds budget %s, '
                'persisting largest results',
                _human_size(total_chars),
                _human_size(MAX_ROUND_TOOL_RESULTS_CHARS))

    candidates = [
        (tc_id, content, tool_name, tool_use_id)
        for tc_id, (content, tool_name, tool_use_id) in tool_results.items()
        if isinstance(content, str)
        and tool_name not in _BUDGET_EXEMPT_TOOLS
        and not content.startswith('[Persisted to:')
    ]
    candidates.sort(key=lambda x: len(x[1]), reverse=True)

    for tc_id, content, tool_name, tool_use_id in candidates:
        if total_chars <= MAX_ROUND_TOOL_RESULTS_CHARS:
            break
        persisted = _persist_to_disk(content, tool_name, tool_use_id, conv_id)
        saved = len(content) - len(persisted)
        total_chars -= saved
        tool_results[tc_id] = (persisted, tool_name, tool_use_id)
        logger.info('[AggregateBudget] Persisted %s result (%s saved), '
                    'new total %s',
                    tool_name, _human_size(saved), _human_size(total_chars))

    return tool_results


def enforce_round_aggregate_budget_v2(
    tool_results: dict[str, tuple[str, str, str]], *, user_id: int,
    model: str = "", observed_at_ms: int = 0,
    recovery_by_call_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, tuple[str, str, str]]:
    """Bound all model-visible results in a round to 24k aggregate tokens."""
    total = sum(_model_result_tokens(content, model)
                for content, _, _ in tool_results.values()
                if isinstance(content, str))
    if total <= ROUND_TOOL_RESULT_V2_MAX_TOKENS:
        return tool_results
    candidates = sorted(
        ((tc_id, value) for tc_id, value in tool_results.items()
         if isinstance(value[0], str)),
        key=lambda row: (-_model_result_tokens(row[1][0], model), row[0]),
    )
    for tc_id, (content, tool_name, tool_use_id) in candidates:
        if total <= ROUND_TOOL_RESULT_V2_MAX_TOKENS:
            break
        before = _model_result_tokens(content, model)
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            value = {}
        declared = ((recovery_by_call_id or {}).get(tc_id) or {})
        existing_recovery = (value.get("recovery")
                             if isinstance(value, dict) else None)
        if not isinstance(existing_recovery, Mapping):
            existing_recovery = {}
        recovery_policy = _normalized_recovery_policy(
            declared.get("policy") or (
                "source" if existing_recovery.get("kind") == "source"
                else "artifact"))
        source_arguments = declared.get("arguments")
        if not isinstance(source_arguments, Mapping):
            source_arguments = existing_recovery.get("arguments")
        if not isinstance(source_arguments, Mapping):
            source_arguments = {}

        artifact_ref = (
            str(value.get("artifactRef") or "")
            if isinstance(value, dict) and recovery_policy == "artifact" else "")
        if not artifact_ref and recovery_policy == "artifact":
            artifact_ref = _store_tool_result_artifact(
                content, user_id=user_id, observed_at_ms=observed_at_ms)
        recovery = (_source_recovery(tool_name, source_arguments)
                    if recovery_policy == "source" else None)
        evidence_id = (str(value.get("evidenceId") or "")
                       if isinstance(value, dict) else "")
        if not evidence_id:
            evidence_id = "ev_" + hashlib.sha256(
                content.encode("utf-8", errors="replace")).hexdigest()[:24]
        raw_bytes = (int(value.get("rawBytes") or 0)
                     if isinstance(value, dict) else 0)
        source_freshness = (value.get("freshness")
                            if isinstance(value, dict) else {})
        if not isinstance(source_freshness, dict):
            source_freshness = {}
        source_observed_at_ms = int(
            source_freshness.get("observedAtMs") or 0)
        source_world_version = str(
            source_freshness.get("worldVersion") or "")
        if recovery is not None:
            reduced_summary = (
                f"{tool_name} result exceeded the shared round budget; "
                f"rerun {tool_name} using recovery.arguments with a narrower "
                "query or range for omitted source evidence."
            )
        elif artifact_ref:
            reduced_summary = (
                f"{tool_name} result moved behind the round aggregate "
                "budget; continue with read_tool_artifact using artifactRef "
                "as artifact_ref and cursor, or search_tool_artifact."
            )
        else:
            source_summary = (str(value.get("summary") or "")
                              if isinstance(value, dict) else "")
            preview = _fit_summary(
                source_summary or content, token_budget=512, model=model,
                marker=_NARROWER_QUERY_MARKER)
            if recovery_policy == "none":
                reduced_summary = (
                    "Result reduced by the shared round budget; this tool "
                    "declares no overflow recovery.\n" + preview)
            else:
                reduced_summary = (
                    "Full aggregate result unavailable because artifact "
                    "persistence failed; rerun with a narrower query.\n"
                    + preview
                )
        source_items = (value.get("items")
                        if isinstance(value, dict) else None)
        from lib.tools.result_projection import (
            is_file_read_projection,
            normalize_file_read_projection_items,
        )
        if (recovery_policy == "source" and tool_name == "read_files"
                and not is_file_read_projection(source_items)):
            source_items = normalize_file_read_projection_items(
                None, source_arguments)
        if is_file_read_projection(source_items):
            reduced = _materialize_file_read_envelope(
                status="partial",
                summary=reduced_summary,
                items=list(source_items),
                artifact_ref=artifact_ref,
                raw_bytes=raw_bytes or len(content.encode("utf-8")),
                observed_at_ms=max(
                    0, source_observed_at_ms or int(observed_at_ms or 0)),
                world_version=source_world_version,
                evidence_id=evidence_id,
                model=model,
                # Aggregate pressure may remove previews, never identities.
                max_preview_chars=0,
                recovery=recovery,
            )
        else:
            reduced = _materialize_envelope(
                status="partial",
                summary=reduced_summary,
                items=[], artifact_ref=artifact_ref, truncated=True,
                raw_bytes=raw_bytes or len(content.encode("utf-8")),
                observed_at_ms=max(
                    0, source_observed_at_ms or int(observed_at_ms or 0)),
                world_version=source_world_version,
                evidence_id=evidence_id, model=model,
                recovery=recovery,
            )
        tool_results[tc_id] = (reduced, tool_name, tool_use_id)
        total -= max(0, before - _model_result_tokens(reduced, model))
    return tool_results


def mark_empty_result(tool_name: str, content: str) -> str:
    """Replace empty/whitespace-only tool results with a descriptive marker.

    Inspired by Claude Code's empty result handling which prevents models
    from misinterpreting empty results as conversation end.
    """
    if isinstance(content, str) and not content.strip():
        return f'({tool_name} completed with no output)'
    return content
