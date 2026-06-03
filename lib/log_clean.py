"""lib/log_clean.py — Pure-function log noise detection.

This is a server-side port of ``static/js/log-clean.js``. It exists for
two reasons:

1. **Frontend/backend boundary** (CLAUDE.md §16): policy that decides
   what counts as "noise" is server-side; the UI is just a renderer
   over the structured detection result.
2. **Headless API parity**: SDK callers and CI pipelines need access
   to the same heuristic that the UI shows in its banner. They get it
   via ``POST /api/v1/logs/clean`` (same shape, same passes).

The function is pure: ``detect_log_noise(text) → CleaningResult | None``.
No I/O, no state, no logger spam on the hot path. Suitable for batch
calls (translate-mode, paper-mode, evaluation harnesses).

Parity with the JS version
--------------------------
* Same regex patterns, same pass ordering, same threshold constants
  (``min_lines=5``, ``saved_pct >= 8``, ``saved_chars >= 80``).
* Same operation labels (Chinese), same banner-tag shape.
* Same output keys so ``static/js/log-clean.js`` becomes a thin
  fetch + render wrapper without changing its UI contract.

Tests in ``tests/test_log_clean.py`` exercise the same fixtures the
existing JS tests use, so any divergence shows up immediately.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import List, Optional

from lib.log import get_logger

logger = get_logger(__name__)


# ── Pass 1: per-line log prefixes ──────────────────────────────────
# Each entry is ``(compiled_regex, label)``. Order matters — the
# Worker-with-full-format pattern must come before the bare
# ``(WorkerName pid=NNN)`` catch-all.

_LOG_PREFIX_PATTERNS = [
    # Ray/vLLM worker: (Worker_XXX pid=NNN) LEVEL MM-DD HH:MM:SS [path:line]
    (re.compile(
        r'^\([\w_]+ pid=\d+\)\s+(?:ERROR|WARNING|INFO|DEBUG)\s+'
        r'\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+\[[^\]]+\]\s*'),
     'Worker日志前缀'),
    # Bare Ray worker tag — strip ONLY the tag (no trailing \s*) so
    # traceback indentation is preserved.
    (re.compile(r'^\([\w_]+ pid=\d+\) ?'), 'Worker前缀'),
    # Standard Python logging: LEVEL YYYY-MM-DD HH:MM:SS,NNN module
    (re.compile(
        r'^(?:ERROR|WARNING|INFO|DEBUG)\s+\d{4}-\d{2}-\d{2}\s+'
        r'\d{2}:\d{2}:\d{2}[.,]\d+\s+[\w.]+\s*'),
     'Python日志前缀'),
    # Bracketed timestamp: [YYYY-MM-DD HH:MM:SS] LEVEL
    (re.compile(
        r'^\[\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]?\d*\]\s*'
        r'(?:ERROR|WARNING|INFO|DEBUG|CRITICAL)?\s*'),
     '时间戳前缀'),
    # Dash-separated: YYYY-MM-DD HH:MM:SS,NNN - name - LEVEL -
    (re.compile(
        r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[.,]\d+\s+-\s+'
        r'[\w.]+\s+-\s+\w+\s+-\s*'),
     '日志前缀'),
    # Go-style: I0302 01:26:07.123456 file.go:123]
    (re.compile(r'^[IWEF]\d{4}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\S+\]\s*'),
     'Go日志前缀'),
    # Task ID prefix
    (re.compile(r'^\[Task\s+[0-9a-f]+\]\s*'), 'Task ID前缀'),
    # Flask/Werkzeug access log prefix
    (re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}\s+-\s+\S+\s+\[.*?\]\s*'),
     'HTTP日志前缀'),
    # Docker/K8s ISO timestamp prefix
    (re.compile(
        r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\s+'),
     'ISO时间戳前缀'),
    # systemd/journald
    (re.compile(
        r'^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+'
        r'\S+(?:\[\d+\])?:\s*'),
     'syslog前缀'),
]

# Whole-line noise patterns (removed in Pass 0.5).
_NOISE_LINE_PATTERNS = [
    (re.compile(
        r'^\d{1,3}(?:\.\d{1,3}){3}\s+-\s+\S+\s+\[.*?\]\s+'
        r'"[A-Z]+\s+\S+\s+HTTP/[\d.]+"\s+[23]\d{2}\s+[\d-]+\s*$'),
     'HTTP成功请求'),
]

_POINTER_LINE_RE = re.compile(r'^\s*[\^~]+\s*$')

# ── Pass 3: shorten long absolute paths ──
_LONG_PATH_RE = re.compile(
    r'(?:/[\w._-]+){4,}/([\w._-]+/[\w._-]+(?:\.[\w]+)?)')

# ── Pass 3.3: tqdm progress bars ──
_TQDM_BAR_RE = re.compile(
    r'(\d+)%\|[^|]*\|\s*[\d.]+[kKMGT]?\s*/\s*[\d.]+[kKMGT]?')
_TQDM_RATE_TAIL_RE = re.compile(r'/(?:s|it)\]\s*$')

# ── Pass 3.5: similarity fingerprint ──
_HEX_ADDR_RE = re.compile(r'0x[0-9a-fA-F]+')
_UUID_RE = re.compile(
    r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.IGNORECASE)
_IP_RE = re.compile(r'\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?')
_DEVICE_ID_RE = re.compile(
    r'\b(?:cuda|gpu|device|worker|rank)\s*[:_]?\s*\d+', re.IGNORECASE)
_LONG_DIGIT_RE = re.compile(r'\b\d{6,}\b')

_DEVICE_ID_PATTERNS = [
    re.compile(r'\bcuda:(\d+)'),
    re.compile(r'\bWorker\s*(\d+)', re.IGNORECASE),
    re.compile(r'\bGPU\s*[:_]?\s*(\d+)', re.IGNORECASE),
    re.compile(r'\brank\s*[:_]?\s*(\d+)', re.IGNORECASE),
    re.compile(r'\bdevice\s*[:_]?\s*(\d+)', re.IGNORECASE),
]

_WORKER_TAG_RE = re.compile(r'^\([\w_]+ pid=\d+\)')


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


# ── Pass implementations ───────────────────────────────────────────

def _deduplicate_worker_blocks(lines: List[str]) -> tuple[List[str], int, int]:
    worker_ids = set()
    for l in lines:
        m = _WORKER_TAG_RE.match(l)
        if m:
            worker_ids.add(m.group(0))
    if len(worker_ids) < 2:
        return lines, 0, len(worker_ids)

    # Split into contiguous worker blocks.
    blocks: List[dict] = []
    cur: Optional[dict] = None
    for l in lines:
        m = _WORKER_TAG_RE.match(l)
        wid = m.group(0) if m else None
        if wid and (cur is None or wid != cur['worker']):
            if cur is not None:
                blocks.append(cur)
            cur = {'worker': wid, 'lines': [l]}
        else:
            if cur is None:
                cur = {'worker': wid or '__none__', 'lines': []}
            cur['lines'].append(l)
    if cur is not None:
        blocks.append(cur)

    def block_content(b: dict) -> str:
        out = []
        for l in b['lines']:
            stripped = l
            for prefix_re, _ in _LOG_PREFIX_PATTERNS:
                m = prefix_re.match(l)
                if m:
                    stripped = l[m.end():]
                    break
            out.append(stripped)
        return '\n'.join(out).strip()

    content_map: dict[str, dict] = {}
    for b in blocks:
        if b['worker'] == '__none__':
            continue
        c = block_content(b)
        if c not in content_map:
            content_map[c] = {'workers': [b['worker']], 'firstBlock': b}
        else:
            content_map[c]['workers'].append(b['worker'])

    total_deduped = 0
    for v in content_map.values():
        if len(v['workers']) > 1:
            total_deduped += len(v['workers']) - 1
    if total_deduped == 0:
        return lines, 0, len(worker_ids)

    used: set[str] = set()
    result: List[str] = []
    for b in blocks:
        if b['worker'] == '__none__':
            result.extend(b['lines'])
            continue
        c = block_content(b)
        info = content_map.get(c)
        if not info:
            result.extend(b['lines'])
            continue
        if c in used:
            continue
        used.add(c)
        first_worker = info['workers'][0]
        for l in b['lines']:
            m = _WORKER_TAG_RE.match(l)
            if not m or m.group(0) == first_worker:
                result.append(l)
    return result, total_deduped, len(worker_ids)


def _collapse_progress_bars(lines: List[str]) -> tuple[List[str], int]:
    result: List[str] = []
    collapsed = 0
    i = 0
    n = len(lines)
    while i < n:
        if not _is_tqdm_line(lines[i]):
            result.append(lines[i])
            i += 1
            continue
        # Collect consecutive tqdm lines (allow blanks between).
        group: List[dict] = []
        j = i
        while j < n:
            if _is_tqdm_line(lines[j]):
                group.append({'line': lines[j],
                              'pct': _extract_tqdm_pct(lines[j]),
                              'idx': j})
                j += 1
            elif (lines[j].strip() == '' and j + 1 < n
                  and _is_tqdm_line(lines[j + 1])):
                j += 1
            else:
                break
        if len(group) < 4:
            result.extend(lines[i:j])
            i = j
            continue

        pcts = [g['pct'] for g in group]
        min_pct = min(pcts)
        max_pct = max(pcts)
        mid_pct = round((min_pct + max_pct) / 2)

        def closest_to(target: int) -> dict:
            best = group[0]
            best_dist = abs(best['pct'] - target)
            for g in group:
                d = abs(g['pct'] - target)
                # Tie-break: prefer later index, mirroring JS behaviour.
                if d < best_dist or (d == best_dist and g['idx'] > best['idx']):
                    best = g
                    best_dist = d
            return best

        picks = [closest_to(min_pct)]
        mid = closest_to(mid_pct)
        end = closest_to(max_pct)
        if mid['idx'] != picks[0]['idx'] and mid['idx'] != end['idx']:
            picks.append(mid)
        if end['idx'] != picks[0]['idx']:
            picks.append(end)
        picks.sort(key=lambda p: p['idx'])

        dev_ids = _extract_device_ids([g['line'] for g in group])
        dropped = len(group) - len(picks)
        result.append(picks[0]['line'])
        summary = f'  … ({dropped} more progress updates'
        if len(dev_ids) > 1:
            summary += f', ×{len(dev_ids)} devices'
        summary += ') …'
        result.append(summary)
        for p in picks[1:]:
            result.append(p['line'])
        collapsed += dropped
        i = j
    return result, collapsed


def _collapse_similar_lines(lines: List[str]) -> tuple[List[str], int]:
    # Pass A: consecutive runs of identical fingerprint (≥ 5 lines).
    pass_a: List[str] = []
    i = 0
    collapsed = 0
    n = len(lines)
    while i < n:
        fp = _fingerprint(lines[i])
        j = i + 1
        while j < n and _fingerprint(lines[j]) == fp:
            j += 1
        run_len = j - i
        if run_len >= 5 and fp:
            pass_a.append(lines[i])
            run_lines = lines[i:j]
            dev_ids = _extract_device_ids(run_lines)
            dropped_a = run_len - 1
            summary_a = f'  … ({dropped_a} more similar'
            if len(dev_ids) > 1:
                summary_a += (f', ×{len(dev_ids)} devices: '
                               f'{_format_device_range(dev_ids)}')
            summary_a += ') …'
            pass_a.append(summary_a)
            collapsed += dropped_a
        else:
            pass_a.extend(lines[i:j])
        i = j

    # Pass B: scattered duplicates (same fingerprint ≥ 5 times).
    fp_count: dict[str, int] = {}
    fp_all_lines: dict[str, List[str]] = {}
    for l in pass_a:
        fp = _fingerprint(l)
        if fp:
            fp_count[fp] = fp_count.get(fp, 0) + 1
            fp_all_lines.setdefault(fp, []).append(l)

    seen: dict[str, int] = {}
    pass_b: List[str] = []
    pass_b_dropped = 0
    for l in pass_a:
        fp = _fingerprint(l)
        if not fp:
            pass_b.append(l)
            continue
        total = fp_count.get(fp, 0)
        if total < 5:
            pass_b.append(l)
            continue
        kept = seen.get(fp, 0)
        seen[fp] = kept + 1
        if kept == 0:
            pass_b.append(l)
            all_lines = fp_all_lines.get(fp) or []
            dev_ids = _extract_device_ids(all_lines)
            dropped_b = total - 1
            summary_b = f'  … ({dropped_b} more similar'
            if len(dev_ids) > 1:
                summary_b += (f', ×{len(dev_ids)} devices: '
                               f'{_format_device_range(dev_ids)}')
            summary_b += ') …'
            pass_b.append(summary_b)
            pass_b_dropped += dropped_b

    collapsed += pass_b_dropped
    return pass_b, collapsed


def _collapse_blank_lines(lines: List[str]) -> List[str]:
    result: List[str] = []
    prev_blank = False
    for l in lines:
        blank = l.strip() == ''
        if blank and prev_blank:
            continue
        result.append(l)
        prev_blank = blank
    return result


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


__all__ = ['detect_log_noise', 'CleaningResult', 'CleaningOp']
