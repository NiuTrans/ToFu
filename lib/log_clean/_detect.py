"""lib/log_clean/_detect.py — Public entrypoint.

``detect_log_noise(text) → CleaningResult | None`` orchestrates every
cleaning pass in the JS-parity order and applies the savings threshold
(``min_lines=5``, ``saved_pct >= 8``, ``saved_chars >= 80``).
"""

from __future__ import annotations

from typing import List, Optional

from lib.log import get_logger

from lib.log_clean._patterns import (
    _LOG_PREFIX_PATTERNS,
    _NOISE_LINE_PATTERNS,
    _POINTER_LINE_RE,
)
from lib.log_clean._types import CleaningOp, CleaningResult
from lib.log_clean._helpers import _shorten_paths
from lib.log_clean._collapse import (
    _collapse_blank_lines,
    _collapse_progress_bars,
    _collapse_similar_lines,
    _deduplicate_worker_blocks,
)

logger = get_logger(__name__)


# ── Main entry point ───────────────────────────────────────────────

def detect_log_noise(text: str) -> Optional[CleaningResult]:
    """Detect cleanable log noise. Returns ``None`` if savings are
    trivial (< 8% or < 80 chars) — same threshold as the JS version."""
    if not isinstance(text, str) or not text:
        return None
    lines = text.split('\n')
    if len(lines) < 5:
        return None

    ops: List[CleaningOp] = []
    working = list(lines)

    prefix_lines_stripped = 0
    pointer_lines_removed = 0
    paths_shortened_count = 0
    workers_deduplicated = 0
    worker_count = 0
    prefix_label = ''
    similar_lines_collapsed = 0
    progress_bars_collapsed = 0

    # Pre-pass: dedup worker tracebacks BEFORE prefix strip.
    working, workers_deduplicated, worker_count = _deduplicate_worker_blocks(
        working)
    if workers_deduplicated > 0:
        ops.append(CleaningOp(
            name='dedup',
            desc=f'合并{workers_deduplicated}个Worker的重复堆栈'
                  f'（{worker_count}个Worker）'))

    # Pass 0.5: drop entire noise lines (HTTP 2xx access logs etc.).
    noise_lines_removed = 0
    before = len(working)
    filtered: List[str] = []
    for l in working:
        if not l.strip():
            filtered.append(l)
            continue
        skip = False
        for pat, _label in _NOISE_LINE_PATTERNS:
            if pat.match(l):
                skip = True
                break
        if not skip:
            filtered.append(l)
    working = filtered
    noise_lines_removed = before - len(working)
    if noise_lines_removed > 0:
        ops.append(CleaningOp(
            name='noise',
            desc=f'移除{noise_lines_removed}行HTTP成功请求日志'))

    # Pass 1: detect & strip per-line prefixes (multi-pattern).
    total_non_empty = sum(1 for l in working if l.strip())
    applied_labels: List[str] = []
    for prefix_re, label in _LOG_PREFIX_PATTERNS:
        cnt = 0
        for l in working:
            if l.strip() and prefix_re.match(l):
                cnt += 1
        if cnt >= 3 and cnt / max(total_non_empty, 1) >= 0.15:
            prefix_lines_stripped += cnt
            new_working = []
            for l in working:
                m = prefix_re.match(l)
                new_working.append(l[m.end():] if m else l)
            working = new_working
            applied_labels.append(f'{cnt}行{label}')
    if applied_labels:
        prefix_label = '、'.join(applied_labels)
        ops.append(CleaningOp(name='prefix',
                               desc=f'去除{"、".join(applied_labels)}'))

    # Pass 2: pointer (^^^) lines.
    before = len(working)
    working = [l for l in working if not _POINTER_LINE_RE.match(l)]
    pointer_lines_removed = before - len(working)
    if pointer_lines_removed > 0:
        ops.append(CleaningOp(
            name='pointer',
            desc=f'移除{pointer_lines_removed}行指向箭头(^^^)'))

    # Pass 3: shorten paths.
    total_path_chars_shaved = 0
    new_working = []
    for idx, l in enumerate(working):
        shortened = _shorten_paths(l)
        total_path_chars_shaved += len(l) - len(shortened)
        new_working.append(shortened)
    if total_path_chars_shaved > 50:
        # Compare against ORIGINAL lines, mirroring JS (l !== lines[i]).
        # Note: indexes drift after earlier passes — match by reverse
        # mapping isn't perfect, but the JS impl has the same drift.
        paths_shortened_count = sum(
            1 for i, l in enumerate(new_working)
            if i < len(lines) and l != lines[i])
        working = new_working
        ops.append(CleaningOp(
            name='paths',
            desc=f'缩短长路径，节省{total_path_chars_shaved}字符'))
    else:
        # Discard the shortened version — savings too small.
        pass

    # Pass 3.3: collapse tqdm bars.
    working, progress_bars_collapsed = _collapse_progress_bars(working)
    if progress_bars_collapsed > 0:
        ops.append(CleaningOp(
            name='progress',
            desc=f'压缩{progress_bars_collapsed}行进度条（保留首/中/末）'))

    # Pass 3.5: similar lines.
    working, similar_lines_collapsed = _collapse_similar_lines(working)
    if similar_lines_collapsed > 0:
        ops.append(CleaningOp(
            name='similar',
            desc=f'合并{similar_lines_collapsed}行重复/近似日志'))

    # Pass 4: blank lines.
    before = len(working)
    working = _collapse_blank_lines(working)
    blank_lines_removed = before - len(working)
    if blank_lines_removed > 2:
        ops.append(CleaningOp(
            name='blanks',
            desc=f'合并{blank_lines_removed}个连续空行'))

    if not ops:
        return None

    cleaned_text = '\n'.join(working)
    saved_chars = len(text) - len(cleaned_text)
    if saved_chars <= 0:
        return None
    saved_pct = round(saved_chars / len(text) * 100)
    if saved_pct < 8 or saved_chars < 80:
        return None

    # Build a sample prefix from the ORIGINAL lines.
    prefix_example = ''
    if prefix_lines_stripped > 0:
        for l in lines:
            for prefix_re, _label in _LOG_PREFIX_PATTERNS:
                m = prefix_re.match(l)
                if m:
                    prefix_example = m.group(0).strip()
                    break
            if prefix_example:
                break

    return CleaningResult(
        originalText=text,
        cleanedText=cleaned_text,
        ops=ops,
        prefixExample=prefix_example,
        prefixLabel=prefix_label,
        prefixLinesStripped=prefix_lines_stripped,
        noiseLinesRemoved=noise_lines_removed,
        pointerLinesRemoved=pointer_lines_removed,
        pathsShortenedCount=paths_shortened_count,
        similarLinesCollapsed=similar_lines_collapsed,
        progressBarsCollapsed=progress_bars_collapsed,
        workersDeduplicated=workers_deduplicated,
        workerCount=worker_count,
        totalLines=total_non_empty,
        savedChars=saved_chars,
        savedPct=saved_pct,
    )
