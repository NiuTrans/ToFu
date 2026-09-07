"""lib/motion_video/_srt.py — SubRip (SRT) parsing utilities.

The storyboard step of the motion-video pipeline consumes an SRT transcript
and splits it into semantically coherent scenes. This module is the zero-LLM
parsing half: timestamp arithmetic with millisecond precision, tolerant block
parsing (malformed blocks are skipped and counted, never fatal), and the
round-trip formatter used by tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['SrtEntry', 'parse_srt', 'parse_timestamp', 'format_timestamp',
           'total_span']

_TS_RE = re.compile(
    r'(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*'
    r'(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})')


@dataclass
class SrtEntry:
    """One subtitle cue. Times are float seconds (millisecond precision)."""
    index: int
    start: float
    end: float
    text: str


def _parts_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    millis = int(ms.ljust(3, '0')[:3])  # '5' → 500, '050' → 50
    return int(h) * 3600 + int(m) * 60 + int(s) + millis / 1000.0


def parse_timestamp(ts: str) -> float:
    """Parse ``HH:MM:SS,mmm`` (or ``.mmm``) to float seconds."""
    m = re.match(r'^\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*$', ts)
    if not m:
        raise ValueError(f'invalid SRT timestamp: {ts!r}')
    return _parts_to_seconds(*m.groups())


def format_timestamp(seconds: float) -> str:
    """Format float seconds as ``HH:MM:SS,mmm`` (round-half-up on ms)."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def parse_srt(text: str) -> list[SrtEntry]:
    """Parse SRT content into a list of :class:`SrtEntry`.

    Tolerant: blocks without a valid ``-->`` timing line are skipped (and
    logged), cue numbers are re-sequenced in output order, multi-line cue
    text is joined with a single space, and ``\\r\\n`` / a leading BOM are
    normalised. Returns cues sorted by start time.
    """
    if not text:
        return []
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    entries: list[SrtEntry] = []
    skipped = 0
    for block in re.split(r'\n\s*\n', text):
        block = block.strip('\n')
        if not block.strip():
            continue
        lines = [ln for ln in block.split('\n')]
        ts_line_idx = None
        match = None
        for i, ln in enumerate(lines[:3]):  # timing line is line 1 or 2 of a cue
            match = _TS_RE.search(ln)
            if match:
                ts_line_idx = i
                break
        if match is None or ts_line_idx is None:
            skipped += 1
            continue
        start = _parts_to_seconds(*match.groups()[0:4])
        end = _parts_to_seconds(*match.groups()[4:8])
        if end <= start:
            skipped += 1
            continue
        body = ' '.join(ln.strip() for ln in lines[ts_line_idx + 1:] if ln.strip())
        entries.append(SrtEntry(index=len(entries) + 1, start=start,
                                end=end, text=body))
    if skipped:
        logger.warning('[MotionVideo] parse_srt skipped %d malformed block(s)', skipped)
    entries.sort(key=lambda e: e.start)
    for i, e in enumerate(entries, 1):
        e.index = i
    return entries


def total_span(entries: list[SrtEntry]) -> tuple[float, float]:
    """``(first_start, last_end)`` in seconds; ``(0.0, 0.0)`` when empty."""
    if not entries:
        return 0.0, 0.0
    return entries[0].start, max(e.end for e in entries)
