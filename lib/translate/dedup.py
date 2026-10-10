"""Repetition-loop detection + truncation for translation outputs.

Cheap models occasionally enter degenerate repetition loops in three ways:

1. **Inline**: a 50-600 char block repeated 3+ times within a single long
   line (no \\n separators).
2. **Single-line consecutive**: the same line repeated ≥ 6 times in a row.
3. **Multi-line block**: a block of 2-8 lines repeated ≥ 4 times
   consecutively (ABCDABCDABCDABCD...).

All three are detected and truncated to ``max_repeats`` occurrences. The
approach avoids false positives from table separators or code lines that
appear multiple times in *different* parts of the document by requiring
*consecutive* repetition.
"""

from collections import Counter

from lib.log import get_logger

logger = get_logger(__name__)


def _dedup_repetition_loop(text, max_repeats=3):
    """Detect and truncate repetition loops in translation output.

    Args:
        text: The translated text to check.
        max_repeats: Maximum allowed consecutive occurrences of the same
            block before truncation (default 3).

    Returns:
        (cleaned_text, was_truncated) tuple.
    """
    truncated = False

    # ── Phase 1: Inline (no-newline) substring repetition ──
    out_lines = []
    for line in text.split('\n'):
        if len(line) > 800:
            cleaned_line, was_cut = _dedup_inline_loop(line, max_repeats=max_repeats)
            if was_cut:
                truncated = True
                line = cleaned_line
        out_lines.append(line)
    text = '\n'.join(out_lines)

    # ── Phase 2: Single-line consecutive repetition ──
    _CONSEC_THRESHOLD = 6
    lines = text.split('\n')
    if len(lines) >= _CONSEC_THRESHOLD:
        kept = []
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            if len(stripped) >= 15:
                run_len = 1
                while i + run_len < len(lines) and lines[i + run_len].strip() == stripped:
                    run_len += 1
                if run_len >= _CONSEC_THRESHOLD:
                    for _ in range(min(max_repeats, run_len)):
                        kept.append(lines[i])
                    logger.warning('[Translate] Single-line repetition: '
                                   '"%s" repeated %dx in a row (keeping %d)',
                                   stripped[:80], run_len, max_repeats)
                    i += run_len
                    truncated = True
                    continue
            kept.append(lines[i])
            i += 1
        if truncated:
            text = '\n'.join(kept).rstrip()
            lines = text.split('\n')

    # ── Phase 3: Multi-line block consecutive repetition ──
    # Detect patterns like ABCDABCDABCD where a block of 2-8 lines repeats.
    _BLOCK_MIN_REPEATS = 4  # at least 4 consecutive block repeats
    for block_size in range(2, 9):  # try block sizes 2..8
        if len(lines) < block_size * _BLOCK_MIN_REPEATS:
            continue
        # Check total chars of a candidate block — skip trivial blocks
        # (e.g. all empty/short lines)
        i = 0
        found_loop = False
        new_lines = []
        while i < len(lines):
            if i + block_size * _BLOCK_MIN_REPEATS <= len(lines):
                block = lines[i:i + block_size]
                block_chars = sum(len(l.strip()) for l in block)
                if block_chars >= 30:  # non-trivial block
                    # Count how many times this block repeats consecutively
                    repeats = 1
                    pos = i + block_size
                    while pos + block_size <= len(lines):
                        if lines[pos:pos + block_size] == block:
                            repeats += 1
                            pos += block_size
                        else:
                            break
                    if repeats >= _BLOCK_MIN_REPEATS:
                        # Keep max_repeats blocks
                        for r in range(min(max_repeats, repeats)):
                            new_lines.extend(lines[i + r * block_size:
                                                    i + (r + 1) * block_size])
                        block_preview = ' | '.join(
                            l.strip()[:40] for l in block[:3])
                        logger.warning('[Translate] Block repetition: '
                                       '%d-line block repeated %dx '
                                       '(keeping %d). Block: %s',
                                       block_size, repeats, max_repeats,
                                       block_preview[:120])
                        i += block_size * repeats
                        found_loop = True
                        truncated = True
                        continue
            new_lines.append(lines[i])
            i += 1
        if found_loop:
            lines = new_lines
            text = '\n'.join(lines).rstrip()
            break  # re-check with smaller block sizes if needed

    return text, truncated


def _dedup_inline_loop(line, max_repeats=3, min_unit=50, max_unit=600,
                       sample_step=10):
    """Detect and truncate a repeating substring block within a single line.

    The model sometimes produces a 100-500 char block repeated 100+ times
    with no newlines.  We detect this by sampling fixed-length windows and
    counting how many times each window appears.

    Args:
        line: A single (long) line of text.
        max_repeats: Keep at most this many occurrences.
        min_unit: Minimum repeating unit length to detect.
        max_unit: Maximum repeating unit length to detect.
        sample_step: Step size for sliding window sampling.

    Returns:
        (cleaned_line, was_truncated) tuple.
    """
    length = len(line)
    if length < min_unit * (max_repeats + 1):
        return line, False

    # Try a few candidate unit lengths (50, 100, 150, 200, 300, 500)
    for unit_len in [50, 100, 150, 200, 250, 300, 400, 500]:
        if unit_len > max_unit or unit_len * (max_repeats + 1) > length:
            continue
        # Sample windows at this unit_len, find the most-repeated one
        window_counts = Counter()
        for i in range(0, length - unit_len, sample_step):
            window_counts[line[i:i + unit_len]] += 1

        # The most common window
        if not window_counts:
            continue
        best_window, best_count = window_counts.most_common(1)[0]
        if best_count < max_repeats + 1:
            continue

        # Found a frequently-repeated window.  Now find the actual repeating
        # unit by locating consecutive occurrences.
        first_pos = line.index(best_window)
        # Find the second occurrence to determine exact unit length
        second_pos = line.index(best_window, first_pos + 1)
        actual_unit_len = second_pos - first_pos
        if actual_unit_len < min_unit or actual_unit_len > max_unit * 2:
            continue

        unit = line[first_pos:first_pos + actual_unit_len]
        # Count consecutive repeats from first_pos
        count = 0
        pos = first_pos
        while pos + actual_unit_len <= length and line[pos:pos + actual_unit_len] == unit:
            count += 1
            pos += actual_unit_len

        if count <= max_repeats:
            continue

        # Truncate: keep content before the loop + max_repeats occurrences
        keep_end = first_pos + actual_unit_len * max_repeats
        # Also keep any trailing content after the loop
        loop_end = first_pos + actual_unit_len * count
        trailing = line[loop_end:]
        cleaned = line[:keep_end] + trailing

        logger.warning('[Translate] Inline repetition: %d-char block repeated %dx '
                       '(keeping %d), line %d→%d chars. Block: %.80s',
                       actual_unit_len, count, max_repeats,
                       length, len(cleaned), unit)
        return cleaned, True

    return line, False
