"""lib/log_clean/_helpers.py — Pure line-level helpers.

Small stateless helpers used by the collapse passes: path shortening,
tqdm detection/parsing, device-id extraction/formatting, and the
similarity fingerprint. All operate on strings/lists — no I/O.
"""

from __future__ import annotations

import re
from typing import List

from lib.log import get_logger

from lib.log_clean._patterns import (
    _DEVICE_ID_PATTERNS,
    _DEVICE_ID_RE,
    _HEX_ADDR_RE,
    _IP_RE,
    _LONG_DIGIT_RE,
    _LONG_PATH_RE,
    _TQDM_BAR_RE,
    _TQDM_RATE_TAIL_RE,
    _UUID_RE,
)

logger = get_logger(__name__)


# ── Helpers ────────────────────────────────────────────────────────

def _shorten_paths(line: str) -> str:
    def _sub(m: re.Match) -> str:
        full = m.group(0)
        tail = m.group(1)
        if len(full) < 50:
            return full
        return '.../' + tail
    return _LONG_PATH_RE.sub(_sub, line)


def _is_tqdm_line(line: str) -> bool:
    return bool(_TQDM_BAR_RE.search(line)) and bool(
        _TQDM_RATE_TAIL_RE.search(line))


def _extract_tqdm_pct(line: str) -> int:
    m = _TQDM_BAR_RE.search(line)
    return int(m.group(1)) if m else -1


def _extract_device_ids(lines: List[str]) -> List[int]:
    ids: set[int] = set()
    for line in lines:
        for pat in _DEVICE_ID_PATTERNS:
            for m in pat.finditer(line):
                try:
                    ids.add(int(m.group(1)))
                except (ValueError, IndexError) as e:
                    logger.debug('[log_clean] device-id parse failed for %r: %s', m.group(0), e)
    return sorted(ids)


def _format_device_range(ids: List[int]) -> str:
    if not ids:
        return ''
    if len(ids) == 1:
        return str(ids[0])
    ranges: List[str] = []
    start = end = ids[0]
    for x in ids[1:]:
        if x == end + 1:
            end = x
        else:
            ranges.append(str(start) if start == end
                           else f'{start}-{end}')
            start = end = x
    ranges.append(str(start) if start == end else f'{start}-{end}')
    return ', '.join(ranges)


def _fingerprint(line: str) -> str:
    s = line
    s = _HEX_ADDR_RE.sub('⊕', s)
    s = _UUID_RE.sub('⊕', s)
    s = _IP_RE.sub('⊛', s)
    s = _DEVICE_ID_RE.sub('⊗', s)
    s = _LONG_DIGIT_RE.sub('⊘', s)
    return s.strip()
