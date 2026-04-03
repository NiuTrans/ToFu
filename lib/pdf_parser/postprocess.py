"""lib/pdf_parser/postprocess.py — Text post-processing for PDF extraction.

Handles:
  - Manuscript line number stripping (standalone + leading)
  - Markdown cleanup (pymupdf4llm quirks)
"""

import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['strip_manuscript_line_numbers', 'cleanup_markdown']


def strip_manuscript_line_numbers(text: str) -> str:
    """Remove line numbers commonly found in review/manuscript PDFs.

    Detects two patterns:
      1) Standalone number lines (e.g. "  123  " on its own line).
      2) Leading line numbers: "123  Some text here" with sequential numbering.
    """
    lines = text.split('\n')
    non_blank = [l for l in lines if l.strip()]
    if len(non_blank) < 10:
        return text

    # Pattern 1: standalone number lines
    standalone_num = re.compile(r'^\s*\d{1,5}\s*$')
    num_count = sum(1 for l in non_blank if standalone_num.match(l))
    ratio = num_count / len(non_blank)

    if ratio > 0.15:
        cleaned = [l for l in lines if not standalone_num.match(l)]
        removed = len(lines) - len(cleaned)
        if removed > 0:
            logger.debug('Stripped %d manuscript line-number lines '
                         '(%.0f%% of non-blank lines were standalone numbers)',
                         removed, ratio * 100)
        return '\n'.join(cleaned)

    # Pattern 2: leading line numbers on text lines
    leading_num = re.compile(r'^(\d{1,5})([ \t]{2,})(.*)')
    matches = [leading_num.match(l) for l in non_blank]
    leading_count = sum(1 for m in matches if m and len(m.group(3).strip()) > 0)
    leading_ratio = leading_count / len(non_blank)

    if leading_ratio > 0.25:
        nums = []
        for m in matches:
            if m and len(m.group(3).strip()) > 0:
                nums.append(int(m.group(1)))
        if len(nums) >= 5:
            increments = sum(1 for a, b in zip(nums, nums[1:]) if 0 < b - a <= 3)
            seq_ratio = increments / max(len(nums) - 1, 1)
            if seq_ratio > 0.4:
                def _strip_leading(line):
                    m = leading_num.match(line)
                    if m and len(m.group(3).strip()) > 0:
                        return m.group(3)
                    return line
                cleaned = [_strip_leading(l) for l in lines]
                logger.debug('Stripped leading line numbers from %d '
                             'lines (seq_ratio=%.0f%%)',
                             leading_count, seq_ratio * 100)
                return '\n'.join(cleaned)

    return text


def cleanup_markdown(md_text: str) -> str:
    """Light cleanup of pymupdf4llm output quirks."""
    # Collapse 3+ consecutive blank lines → 2
    md_text = re.sub(r'\n{3,}', '\n\n', md_text)
    # Fix double-encoded bold: ****text**** → **text**
    md_text = re.sub(r'\*{4,}(.+?)\*{4,}', r'**\1**', md_text)
    return md_text
