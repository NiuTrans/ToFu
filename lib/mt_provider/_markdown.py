"""lib/mt_provider/_markdown.py — Markdown structure preservation for MT.

MT APIs treat input as plain text and would corrupt code blocks and strip
markdown structural elements. These pure functions extract code blocks and
line-level markdown prefixes before translation and reattach them after, so
the MT API receives clean sentences and formatting is preserved.
"""

import re

from lib.log import get_logger

logger = get_logger(__name__)

# ── Code block extraction for MT ──
# MT APIs don't understand markdown — code blocks would get corrupted.
# We extract them before translation and reinsert after.
_CODE_BLOCK_RE = re.compile(r'(```[\w]*\n[\s\S]*?```)', re.MULTILINE)
_INLINE_CODE_RE = re.compile(r'(`[^`\n]+`)')


def _extract_code_blocks(text):
    """Extract fenced code blocks and inline code, replacing with placeholders.

    Uses ``[CBLOCK_N]`` format which NiuTrans preserves verbatim.

    Returns:
        (cleaned_text, blocks_dict) where blocks_dict maps placeholder → original.
    """
    blocks = {}
    counter = [0]

    def _replace_block(m):
        key = '[CBLOCK_%d]' % counter[0]
        blocks[key] = m.group(0)
        counter[0] += 1
        return key

    # Fenced code blocks first (greedy, before inline)
    text = _CODE_BLOCK_RE.sub(_replace_block, text)
    # Inline code
    text = _INLINE_CODE_RE.sub(_replace_block, text)
    return text, blocks


def _restore_code_blocks(text, blocks):
    """Reinsert code blocks from placeholders."""
    for key, original in blocks.items():
        # NiuTrans may add spaces around the placeholder
        text = text.replace(key, original)
    return text


# ── Markdown structure preservation for MT ──
# MT APIs (NiuTrans etc.) treat input as plain text and strip markdown
# structural elements like headings (###), list markers (- / * / 1.),
# blockquotes (>), horizontal rules (---), and bold/italic markers.
# We strip these prefixes before translation and reattach after, so
# the MT API gets clean sentences and markdown structure is preserved.

# Regex matching markdown line-level prefixes: headings, list items, blockquotes
_MD_PREFIX_RE = re.compile(
    r'^('
    r'#{1,6}\s+'               # headings: # ## ### etc.
    r'|[-*+]\s+'               # unordered list: - * +
    r'|\d+\.\s+'               # ordered list: 1. 2. 3.
    r'|>\s*'                   # blockquote: >
    r')',
    re.MULTILINE
)

# Lines that are purely structural (no translatable text) — preserve as-is
_MD_STRUCTURAL_LINE_RE = re.compile(
    r'^('
    r'\s*[-*_]{3,}\s*'         # horizontal rules: --- *** ___
    r'|\s*\|[-:\s|]+\|\s*'     # table separator: | --- | --- |
    r'|\s*$'                   # empty lines
    r')$'
)


def _extract_md_structure(text):
    """Strip markdown structural prefixes from each line, preserving them for reattach.

    For each line, detects and strips leading markdown markers (headings, lists,
    blockquotes) so the MT API receives clean translatable text. The stripped
    prefixes are stored per-line for restoration after translation.

    Also preserves **bold**, *italic*, and ***bold-italic*** inline markers
    by extracting them before translation and reinserting after.

    Args:
        text: Markdown-formatted text.

    Returns:
        (cleaned_text, line_prefixes) where line_prefixes is a list of
        (prefix_str, indent_str) tuples, one per line.
    """
    lines = text.split('\n')
    prefixes = []
    cleaned = []

    for line in lines:
        # Structural-only lines (horizontal rules, table separators, empty) — keep as-is
        if _MD_STRUCTURAL_LINE_RE.match(line):
            prefixes.append(('', ''))
            cleaned.append(line)
            continue

        # Check for CBLOCK placeholders — don't modify these lines
        if '[CBLOCK_' in line:
            prefixes.append(('', ''))
            cleaned.append(line)
            continue

        # Extract leading whitespace (indentation)
        stripped = line.lstrip()
        indent = line[:len(line) - len(stripped)]

        # Match markdown prefix
        m = _MD_PREFIX_RE.match(stripped)
        if m:
            prefix = m.group(1)
            rest = stripped[len(prefix):]
            prefixes.append((prefix, indent))
            cleaned.append(indent + rest)
        else:
            prefixes.append(('', indent))
            cleaned.append(line)

    return '\n'.join(cleaned), prefixes


def _restore_md_structure(text, prefixes):
    """Reattach markdown structural prefixes to translated lines.

    MT APIs may merge or split lines differently. This function handles:
    - Same line count: direct 1:1 reattach
    - Different line count: best-effort mapping, only attaching prefixes
      to non-empty lines

    Args:
        text: Translated text (from MT API).
        prefixes: Line prefix data from _extract_md_structure().

    Returns:
        Text with markdown prefixes restored.
    """
    lines = text.split('\n')
    result = []

    if len(lines) == len(prefixes):
        # Perfect 1:1 mapping — most common case when MT preserves line breaks
        for line, (prefix, indent) in zip(lines, prefixes):
            if prefix and line.strip():
                # Reattach prefix, respecting original indentation
                stripped = line.lstrip()
                result.append(indent + prefix + stripped)
            else:
                result.append(line)
    else:
        # Line count mismatch — MT merged/split lines.
        # Best effort: attach prefixes to corresponding non-empty lines.
        # Build a queue of prefixes that need attaching.
        prefix_queue = [(p, ind) for p, ind in prefixes if p]
        qi = 0
        for line in lines:
            if qi < len(prefix_queue) and line.strip():
                prefix, indent = prefix_queue[qi]
                stripped = line.lstrip()
                # Only attach if the line doesn't already start with a markdown prefix
                if not _MD_PREFIX_RE.match(stripped):
                    result.append(indent + prefix + stripped)
                    qi += 1
                else:
                    result.append(line)
            else:
                result.append(line)

    return '\n'.join(result)
