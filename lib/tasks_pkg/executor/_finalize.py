# HOT_PATH
"""Shared tool-handler helpers — DRY finalization & meta.

``_finalize_tool_round`` and ``_build_simple_meta`` are the shared result
projection primitives. Handlers bind them from the executor API directly.
"""

from __future__ import annotations

from typing import Any

from lib.agent_core.events import EventType, build_event, now_ms
from lib.log import get_logger
from lib.tool_rejection import (
    stamp_tool_rejection,
    tool_rejection_descriptor,
)
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


#: Tools whose output is a terminal transcript that may contain a QR code a
#: human is expected to SCAN (``gh auth login``, ``docker login``, wrangler,
#: any ``qrcode.print_ascii`` caller). Scanned for QR art on finalize.
_QR_SCAN_TOOLS = frozenset({'run_command', 'code_exec'})


def _attach_terminal_qr(results: list) -> None:
    """Promote QR codes drawn as terminal art into real inline images.

    Terminal QR art cannot be scanned from the chat transcript: the output
    pane (``.ptool-cmd-output``) is ``white-space: pre-wrap`` with
    ``word-break: break-all``, so the module rows re-wrap at arbitrary
    columns and the 2-D grid is destroyed. Restyling cannot fix it either,
    because the user has to point a phone at it — it must become a bitmap.

    Runs on the shared finalize path so ALL run_command surfaces (local
    sandbox, remote worktree, project handler) are covered by one
    implementation instead of three that drift apart.

    Best-effort by construction: a failure here must never fail the tool
    round, so the command's real result is always preserved.
    """
    for meta in results:
        if not isinstance(meta, dict):
            continue
        if meta.get('toolName') not in _QR_SCAN_TOOLS:
            continue
        text = meta.get('output')
        if not isinstance(text, str) or not text:
            continue
        try:
            from lib.qr import terminal_qr_images
            imgs = terminal_qr_images(text)
        except Exception as e:
            logger.warning('[QR] terminal QR scan failed (non-fatal): %s', e)
            continue
        if imgs:
            meta['qrImages'] = imgs
            logger.info('[QR] attached %d scannable QR image(s) to a %s round',
                        len(imgs), meta.get('toolName'))


#: Terminal verdicts a finalize may stamp. Anything else is normalized to
#: 'done' — a finalize SETTLES a round, and the only honest settle states are
#: success or one of these backend-assigned failure verdicts (the same set the
#: client reducer treats as terminal, _TERMINAL_ROUND_STATUS).
_FINALIZE_VERDICTS = frozenset(
    {'done', 'error', 'rejected', 'aborted', 'unanswerable'})


def _finalize_tool_round(
    task: dict[str, Any],
    rn: int,
    round_entry: dict[str, Any],
    results: list,
    *,
    query_override: str = '',
    extra_event_fields: dict[str, Any] | None = None,
    status: str = 'done',
) -> None:
    """Finalize a tool round: set results & status, emit the SSE event.

    This replaces the 3-line boilerplate repeated in every tool handler::

        round_entry['results'] = results
        round_entry['status'] = 'done'
        append_event(task, {'type': 'tool_result', ...})

    ``status`` (keyword-only, default ``'done'``) is the backend's terminal
    VERDICT on the tool — the single source of truth the client renders. It
    is stamped on the round BEFORE the event is built and ALWAYS rides the
    ``tool_result`` wire frame, so neither the live client nor a later
    replay/rehydration ever has to GUESS the outcome (guessing is how a
    crashed tool rendered as a ✓ success card). A non-done verdict already
    on the round is never demoted by a late 'done' finalize (a pool-timeout
    lane whose cancelled thread finishes late must not overwrite the
    'error' the pipeline already recorded).

    Parameters
    ----------
    task : dict
        Live task dict — event is appended.
    rn : int
        Round number for the event.
    round_entry : dict
        The search-round entry dict to finalize.
    results : list
        List of result meta dicts (usually ``[meta]``).
    query_override : str, optional
        If provided, overrides ``round_entry['query']`` in the event.
    extra_event_fields : dict, optional
        Additional fields to merge into the SSE event payload
        (e.g. ``{'engineBreakdown': ...}``).
    """
    # Must run BEFORE results are frozen onto the round + copied into the SSE
    # event, so the live stream and any later replay/rehydration carry the
    # same descriptors (a post-hoc mutation would reach only one of them).
    rejection = tool_rejection_descriptor(round_entry)
    if rejection is not None and isinstance(results, list):
        for result_meta in results:
            if isinstance(result_meta, dict):
                stamp_tool_rejection(
                    result_meta, rejection, legacy_result_alias=True)
    if isinstance(results, list):
        _attach_terminal_qr(results)
    round_entry['results'] = results
    _status = status if status in _FINALIZE_VERDICTS else 'done'
    _prior = round_entry.get('status')
    if (_status == 'done' and isinstance(_prior, str)
            and _prior in _FINALIZE_VERDICTS and _prior != 'done'):
        # Verdict protection: a failure verdict already recorded (e.g. the
        # pool-timeout lane stamped 'error') outranks a late success settle.
        _status = _prior
    round_entry['status'] = _status

    # Timing contract (). `tEnd` is stamped HERE — the shared
    #   finalize seam every one of the ~44 handler call sites already funnels
    #   through — so per-tool duration is measured in ONE place instead of 44
    #   that would drift. `tStart` is carried forward from the round so the
    #   terminal frame is SELF-DESCRIBING: a client that reconnected mid-turn
    #   (or replays from a cursor) never saw the tool_start, and would
    #   otherwise render a blank duration on exactly the path a user takes when
    #   investigating a slow turn.
    #   `tStart` may be absent when a round dict was hand-built by a secondary
    #   surface (paper / swarm / timer); we then fall back to `tEnd` rather than
    #   inventing a start, so a duration is either honest or zero — never
    #   fabricated.
    _t_end = now_ms()
    round_entry['tEnd'] = _t_end
    _t_start = round_entry.get('tStart')
    if _t_start is None:
        _t_start = _t_end
        round_entry['tStart'] = _t_start

    _tool_name = round_entry.get('toolName')
    if not _tool_name and isinstance(results, list) and results \
            and isinstance(results[0], dict):
        _tool_name = results[0].get('toolName')
    event = build_event(
        EventType.TOOL_RESULT,
        roundNum=rn,
        toolCallId=round_entry.get('toolCallId', ''),
        toolName=_tool_name or '',
        query=query_override or round_entry['query'],
        results=results,
        # Self-describing terminal frame: the verdict ALWAYS rides the event,
        # so a client never settles a round by inference. The client treats
        # this field as the ONLY truth for the success/failure badge.
        status=_status,
        tStart=_t_start,
        tEnd=_t_end,
    )
    # Carry the harness self-repair descriptor onto the tool_result event.
    #   For early-announced rounds the original tool_start went out with the
    #   pre-repair (possibly garbled) display, so the frontend relies on this
    #   to swap in the corrected line + "auto-fixed" badge.
    if round_entry.get('_repaired'):
        event['_repaired'] = round_entry['_repaired']
    if rejection is not None:
        stamp_tool_rejection(event, rejection)
    if extra_event_fields:
        event.update(extra_event_fields)
    append_event(task, event)


def _build_simple_meta(
    fn_name: str,
    tool_content,
    *,
    source: str,
    icon: str = '',
    badge: str = '',
    title: str = '',
    snippet: str = '',
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standard tool result meta dict.

    Handles the common pattern where handlers build near-identical dicts
    with ``toolName``, ``title``, ``snippet``, ``source``, ``fetched``,
    ``fetchedChars``, and ``badge``.  Any extra keys can be merged via
    *extra*.

    Parameters
    ----------
    fn_name : str
        Tool function name.
    tool_content : str | Any
        Raw tool output — used for ``fetchedChars`` and default snippet.
    source : str
        Source label (e.g. ``'Scheduler'``, ``'Swarm'``).
    icon : str
        Emoji prefix for the default title and badge.
    badge : str
        Badge text (defaults to *icon* if not provided).
    title : str
        Override title (defaults to ``'{icon} {fn_name}'``).
    snippet : str
        Override snippet (defaults to first 120 chars of *tool_content*).
    extra : dict, optional
        Additional keys merged into the meta dict.
    """
    content_str = tool_content if isinstance(tool_content, str) else str(tool_content)
    meta = {
        'toolName': fn_name,
        'title': title or (f'{icon} {fn_name}' if icon else fn_name),
        'snippet': snippet or content_str[:120].replace('\n', ' '),
        'source': source,
        'fetched': True,
        'fetchedChars': len(content_str),
        'badge': badge or icon,
    }
    if extra:
        meta.update(extra)
    return meta
