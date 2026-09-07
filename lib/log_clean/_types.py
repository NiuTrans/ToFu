"""lib/log_clean/_types.py — Output dataclasses (mirrors the JS shape).

``CleaningOp`` and ``CleaningResult`` mirror the JS object shape so
``static/js/log-clean.js`` can render the detection result directly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List

from lib.log import get_logger

logger = get_logger(__name__)


# ── Output dataclasses (mirrors the JS shape) ──────────────────────

@dataclass
class CleaningOp:
    name: str
    desc: str


@dataclass
class CleaningResult:
    """Structured cleaning report. Mirrors the JS object shape so
    ``static/js/log-clean.js`` can render it directly."""
    originalText: str
    cleanedText: str
    ops: List[CleaningOp]
    prefixExample: str = ''
    prefixLabel: str = ''
    prefixLinesStripped: int = 0
    noiseLinesRemoved: int = 0
    pointerLinesRemoved: int = 0
    pathsShortenedCount: int = 0
    similarLinesCollapsed: int = 0
    progressBarsCollapsed: int = 0
    workersDeduplicated: int = 0
    workerCount: int = 0
    totalLines: int = 0
    savedChars: int = 0
    savedPct: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d['ops'] = [asdict(op) for op in self.ops]
        return d
