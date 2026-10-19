"""lib/project_mod/write_tools/_text.py — pure text / fuzzy-match / fs-nudge
leaf helpers used by the write operations.

Extracted verbatim from the former flat ``write_tools.py``. These have NO
back-references into the write-path core (``_paths``); they are consumed by the
edit operations in ``_ops`` (apply_diff / insert_content matching) and by
``tool_write_file`` (the VS Code mtime nudge).
"""

import os
import re
from difflib import SequenceMatcher

from lib.log import get_logger

logger = get_logger(__name__)


# Match \uXXXX, \UXXXXXXXX and \xXX escape sequences (literal backslash form).
_UNICODE_ESCAPE_RE = re.compile(r'\\U[0-9a-fA-F]{8}|\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}')


def _decode_unicode_escapes(s):
    """Decode literal ``\\uXXXX`` / ``\\UXXXXXXXX`` / ``\\xXX`` escapes to the
    characters they denote, leaving all other text untouched.

    Models frequently emit a real glyph (e.g. ``⏰``, em-dash ``—``) in an
    ``apply_diff`` search where the file on disk holds the literal escape text
    (``\\u23f0``, ``\\u2014``) — or the reverse. Decoding both sides before
    comparison lets the matcher see through this representation drift. Only the
    three numeric-escape forms are decoded; ``\\n`` / ``\\t`` and other C-style
    escapes are deliberately left alone to avoid surprising false matches.
    """
    def _repl(m):
        try:
            return chr(int(m.group(0)[2:], 16))
        except (ValueError, OverflowError) as e:
            logger.debug('[write_tools] undecodable unicode escape %r: %s', m.group(0), e)
            return m.group(0)
    return _UNICODE_ESCAPE_RE.sub(_repl, s)


def _find_closest_match(content, search, threshold=0.6):
    """Find the most similar block in content to the search string."""
    search_lines = search.split('\n')
    n = len(search_lines)
    if n == 0 or not content.strip():
        return None

    content_lines = content.split('\n')
    if len(content_lines) < n:
        return None

    best_ratio = 0.0
    best_start = 0

    search_first_stripped = search_lines[0].strip()[:40]
    search_last_stripped = search_lines[-1].strip()[:40] if n > 1 else search_first_stripped
    candidate_starts = set()
    for i, line in enumerate(content_lines):
        ls = line.strip()
        if (search_first_stripped and search_first_stripped in ls) or \
           (search_last_stripped and search_last_stripped in ls):
            for offset in range(max(0, i - n + 1), min(len(content_lines) - n + 1, i + 1)):
                candidate_starts.add(offset)

    if not candidate_starts:
        candidate_starts = set(range(0, len(content_lines) - n + 1, max(1, (len(content_lines) - n) // 500 + 1)))

    for start in candidate_starts:
        window = '\n'.join(content_lines[start:start + n])
        ratio = SequenceMatcher(None, search, window, autojunk=False).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start

    if best_ratio >= threshold:
        best_text = '\n'.join(content_lines[best_start:best_start + n])
        if len(best_text) > 600:
            best_text = best_text[:600] + '\n… (truncated)'
        return {
            'text': best_text,
            'line': best_start + 1,
            'similarity': best_ratio,
        }
    return None


def _describe_duplicate_matches(content, search, context=1, max_show=5):
    """Build a human/LLM-friendly listing of where *search* matches in *content*.

    For each occurrence, shows the 1-based line number plus a few lines of
    surrounding context so the caller can pick a unique anchor.

    Args:
        content: The file text the search was run against.
        search: The (already line-normalized) search block.
        context: Lines of context to show before/after each match.
        max_show: Cap on how many matches to render in detail.

    Returns:
        A formatted multi-line string, or '' if no line-aligned match exists.
    """
    search_lines = search.split('\n')
    n = len(search_lines)
    content_lines = content.split('\n')
    if n == 0 or len(content_lines) < n:
        return ''

    starts = [i for i in range(len(content_lines) - n + 1)
              if content_lines[i:i + n] == search_lines]
    if not starts:
        return ''

    parts = []
    for idx, start in enumerate(starts[:max_show], 1):
        lo = max(0, start - context)
        hi = min(len(content_lines), start + n + context)
        block = []
        for ln in range(lo, hi):
            marker = '>' if start <= ln < start + n else ' '
            block.append(f'{marker} {ln + 1}: {content_lines[ln]}')
        parts.append(f'Match {idx} (line {start + 1}):\n' + '\n'.join(block))

    out = '\n\n'.join(parts)
    if len(starts) > max_show:
        out += f'\n\n… and {len(starts) - max_show} more match(es).'
    return out


def _common_indent(lines):
    """Leading-whitespace prefix of the first non-blank line ('' when none).

    Used by the relative-indent matching rung: the model's block and the
    matched file block each contribute one anchor indent, and the replacement
    is re-framed between them so relative indentation is preserved.
    """
    for line in lines:
        if line.strip():
            return line[:len(line) - len(line.lstrip())]
    return ''


def _uniform_indent_offset(search_lines, content_lines, *, decode=False):
    """Return one literal (removed, added) indentation prefix, or no match.

    Every nonblank line must have identical text and the same prefix shift.
    Stripping each line independently would erase Python nesting and accept
    anchors describing different control flow. Tabs are literal, never widths.
    Blank-line/trailing whitespace and numeric escape tolerance stay separate.
    """
    offset = None
    for search, content in zip(search_lines, content_lines):
        search_body = search.lstrip(' \t').rstrip()
        content_body = content.lstrip(' \t').rstrip()
        if not search_body and not content_body:
            continue
        if not search_body or not content_body:
            return None
        if decode:
            search_body = _decode_unicode_escapes(search_body)
            content_body = _decode_unicode_escapes(content_body)
        if search_body != content_body:
            return None
        search_prefix = search[:len(search) - len(search.lstrip(' \t'))]
        content_prefix = content[:len(content) - len(content.lstrip(' \t'))]
        if offset is None:
            if content_prefix.endswith(search_prefix):
                offset = ('', content_prefix[:len(content_prefix) - len(search_prefix)])
            elif search_prefix.endswith(content_prefix):
                offset = (search_prefix[:len(search_prefix) - len(content_prefix)], '')
            else:
                return None
        removed, added = offset
        if (not search_prefix.startswith(removed)
                or added + search_prefix[len(removed):] != content_prefix):
            return None
    return offset


def _reindent_block(lines, *, from_prefix, to_prefix):
    """Re-frame replacement *lines* from the search anchor's indentation into
    the matched file block's indentation.

    Uniform out-dent/over-indent is the most common model drift (aider's
    relative-indent finding): ``from_prefix`` is the search block's anchor
    indent, ``to_prefix`` the file block's. Blank lines pass through
    untouched; a line that does not start with ``from_prefix`` keeps no
    absolute claim, so it is rebased onto ``to_prefix`` fully stripped.
    """
    out = []
    for line in lines:
        if not line.strip():
            out.append(line)
        elif not from_prefix:
            out.append(to_prefix + line)
        elif line.startswith(from_prefix):
            out.append(to_prefix + line[len(from_prefix):])
        else:
            out.append(to_prefix + line.lstrip())
    return out


def _touch_for_vscode(filepath):
    """Bump mtime to ensure VS Code's file watcher picks up external writes."""
    try:
        st = os.stat(filepath)
        new_mtime = st.st_mtime + 0.000001
        os.utime(filepath, (st.st_atime, new_mtime))
    except OSError as e:
        logger.debug('[WriteTools] Failed to bump mtime for VS Code watcher on %s: %s', filepath, e)
