"""lib/log_clean/_collapse.py — Pass implementations (collapse/dedup).

The heavy line-transform passes: worker-block dedup, tqdm progress-bar
collapse, similar-line collapse (consecutive + scattered), and blank-line
collapse. Each is a pure function over a list of lines.
"""

from __future__ import annotations

from typing import List, Optional

from lib.log import get_logger

from lib.log_clean._patterns import (
    _LOG_PREFIX_PATTERNS,
    _WORKER_TAG_RE,
)
from lib.log_clean._helpers import (
    _describe_numbered_variants,
    _extract_device_ids,
    _extract_tqdm_pct,
    _fingerprint,
    _is_tqdm_line,
)

logger = get_logger(__name__)


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
        variants = _describe_numbered_variants(dev_ids)
        if variants:
            summary += f', {variants}'
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
            variants_a = _describe_numbered_variants(dev_ids)
            if variants_a:
                summary_a += f', {variants_a}'
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
            variants_b = _describe_numbered_variants(dev_ids)
            if variants_b:
                summary_b += f', {variants_b}'
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
