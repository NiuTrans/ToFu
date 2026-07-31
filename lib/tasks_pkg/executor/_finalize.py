# HOT_PATH
"""Shared tool-handler helpers — DRY finalization & meta.

``_finalize_tool_round`` and ``_build_simple_meta`` are MONKEYPATCH TARGETS:
they are imported+patched by ``lib/tasks_pkg/handlers/misc/_human.py`` (via the
misc facade) and by tests. They are re-exported from the ``executor`` package
facade so ``from lib.tasks_pkg.executor import _finalize_tool_round`` works.
"""

from __future__ import annotations

from typing import Any

from lib.agent_core.events import EventType, build_event, now_ms
from lib.log import get_logger
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


def _finalize_tool_round(
    task: dict[str, Any],
    rn: int,
    round_entry: dict[str, Any],
    results: list,
    *,
    query_override: str = '',
    extra_event_fields: dict[str, Any] | None = None,
) -> None:
    """Finalize a tool round: set results & status, emit the SSE event.

    This replaces the 3-line boilerplate repeated in every tool handler::

        round_entry['results'] = results
        round_entry['status'] = 'done'
        append_event(task, {'type': 'tool_result', ...})

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
    if isinstance(results, list):
        _attach_terminal_qr(results)
    round_entry['results'] = results
    round_entry['status'] = 'done'

    # ★ Timing contract (pt_67ffc2b7). `tEnd` is stamped HERE — the shared
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

    event = build_event(
        EventType.TOOL_RESULT,
        roundNum=rn,
        toolCallId=round_entry.get('toolCallId', ''),
        query=query_override or round_entry['query'],
        results=results,
        tStart=_t_start,
        tEnd=_t_end,
    )
    # ★ Carry the harness self-repair descriptor onto the tool_result event.
    #   For early-announced rounds the original tool_start went out with the
    #   pre-repair (possibly garbled) display, so the frontend relies on this
    #   to swap in the corrected line + "auto-fixed" badge.
    if round_entry.get('_repaired'):
        event['_repaired'] = round_entry['_repaired']
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
