"""Disk persistence for oversized tool results — format-aware splitters.

When a tool result exceeds its per-tool budget (Layer 0), instead of
irreversibly truncating to head+tail, we write the full content to disk
and return a preview + file path the model can ``read_files`` if needed.

Five tools get format-aware splitters that fan out into one file per
result instead of one giant blob:

  * ``web_search``  → one file per search hit
  * ``grep_search`` → one file per matched source file
  * ``find_files``  → one file per batched search section
  * ``fetch_url``   → one file per URL
  * (default)       → single-file persistence

Imports nothing from sibling sub-modules except ``_constants``.
"""

import os
import re
import uuid

from lib.log import get_logger
from lib.tasks_pkg.persist_registry import register as _register_label
from lib.tasks_pkg.compaction._constants import (
    _DEFAULT_TOOL_RESULT_MAX,
    _PERSIST_DIR_BASE,
    _PERSIST_PREVIEW_CHARS,
    TOOL_RESULT_MAX_CHARS,
)

logger = get_logger(__name__)


# Matches a vertical-search block emitted by
# ``handlers.search._vertical_header_for_llm`` and prepended to web_search
# tool content: the ``═══ Vertical Search … ═══`` header, its body, and the
# closing ``═══ Web Search Results ═══`` marker — optionally preceded by the
# ``=== Search: <q> ===`` batch-query header. Relocated into the persist index
# by ``_persist_web_search_split`` because the ``═══`` header is NOT a ``════``
# per-result boundary: left in place, the split would glue the block onto
# result-1's file and it would drop out of the model's immediate context.
# See JOURNAL 2026-07-06 (vertical-search debug-panel gap).
_VERT_BLOCK_RE = re.compile(
    r'(?:^=== Search:[^\n]*\n)?'
    r'^═══ Vertical Search.*?'
    r'^═══ Web Search Results ═══[^\n]*\n?',
    re.MULTILINE | re.DOTALL,
)


def _short_id(tool_use_id: str) -> str:
    """Derive a short, unique filename id fragment from a tool-use id.

    The live caller always passes a provider tool-use id (``toolu_<24 random>``
    for Anthropic, ``call_<24>`` for OpenAI, etc.).  The entropy lives in the
    tail, while the ``toolu_`` / ``call_`` prefix is constant and repeated once
    per split file in the persist index the model reads back — pure waste.
    Strip the constant prefix and keep the entropy-bearing tail, capped so a
    persisted filename never balloons.  Falls back to a fresh uuid fragment
    when no tool-use id is supplied.
    """
    raw = (tool_use_id or uuid.uuid4().hex[:12]).replace('/', '_')
    raw = re.sub(r'^(toolu_|call_|fc_)', '', raw)
    return raw[-16:] or 'id'


def _human_size(byte_count: int) -> str:
    """Format a byte/char count as a human-readable string.

    Local copy so ``_persist`` stays a strict leaf-of-``_constants``.
    """
    if byte_count < 1024:
        return f'{byte_count}B'
    elif byte_count < 1024 * 1024:
        return f'{byte_count / 1024:.1f}KB'
    else:
        return f'{byte_count / (1024 * 1024):.1f}MB'


# Lines that carry no human meaning as a result description: decorative
# rules (═══ / ─── / ═══ boundaries, ==== markers) and blank lines. Used to
# skip past the leading separator a formatted tool result (e.g.
# ``get_conversation``) opens with, so the persisted-result label reads
# ``past conversation — "Referenced Conversation: …"`` instead of a wall of
# box-drawing characters.
_DECORATIVE_LINE_RE = re.compile(r'^[\s═─—\-=_*#·•]+$')


def _first_meaningful_line(content: str) -> str:
    """Return the first non-decorative, non-blank line of ``content``.

    Falls back to the first line when every scanned line is decorative, so a
    description is always produced.
    """
    first = ''
    for line in content.lstrip().split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if not first:
            first = stripped
        if not _DECORATIVE_LINE_RE.match(stripped):
            return stripped
    return first


def _persist_to_disk(content: str, tool_name: str, tool_use_id: str = '',
                     conv_id: str = '') -> str:
    """Persist oversized tool result to disk and return a summary with file paths.

    For tools with structured, multi-item results (web_search, grep_search),
    each item is saved to a **separate** file so the model can selectively
    read only the items it needs via read_files.

    For single-blob tools (fetch_url, run_command, etc.), the full content
    is saved to a single file as before.

    Args:
        content:     Full tool result string.
        tool_name:   Name of the tool that produced the result.
        tool_use_id: Tool call ID (used for filename uniqueness).
        conv_id:     Conversation ID (used for directory grouping).

    Returns:
        A formatted string with file path(s) + preview/index.
    """
    dir_name = conv_id[:12] if conv_id else 'default'
    persist_dir = os.path.join(_PERSIST_DIR_BASE, dir_name)
    os.makedirs(persist_dir, exist_ok=True)

    safe_id = _short_id(tool_use_id)

    # ── Try split-persist for multi-item tools ──
    if tool_name == 'web_search':
        result = _persist_web_search_split(content, persist_dir, safe_id)
        if result is not None:
            return result

    if tool_name == 'grep_search':
        result = _persist_grep_search_split(content, persist_dir, safe_id)
        if result is not None:
            return result

    if tool_name == 'fetch_url':
        result = _persist_fetch_url_split(content, persist_dir, safe_id)
        if result is not None:
            return result

    if tool_name == 'find_files':
        result = _persist_find_files_split(content, persist_dir, safe_id)
        if result is not None:
            return result

    # ── Default: single file persistence ──
    filename = f'{tool_name}_{safe_id}.txt'
    filepath = os.path.join(persist_dir, filename)

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        logger.warning('[Persist] Failed to write %s: %s', filepath, e,
                       exc_info=True)
        return _truncate_head_tail(content, tool_name,
                                   TOOL_RESULT_MAX_CHARS.get(tool_name, _DEFAULT_TOOL_RESULT_MAX))

    logger.info('[Persist] %s result persisted to disk: %s (%s)',
                tool_name, filepath, _human_size(len(content)))

    _register_label(filepath, tool_name, _first_meaningful_line(content))

    # Default preview: first N chars truncated at newline boundary
    preview = content[:_PERSIST_PREVIEW_CHARS]
    last_nl = preview.rfind('\n')
    if last_nl > _PERSIST_PREVIEW_CHARS // 2:
        preview = preview[:last_nl]

    return (
        f'[Persisted to: {filepath}]\n'
        f'Output too large ({_human_size(len(content))}). '
        f'Full output saved to: {filepath}\n'
        f'Use read_files to access the full content if needed.\n\n'
        f'Preview:\n'
        f'{preview}\n'
    )


def _sanitize_filename(s: str, max_len: int = 60) -> str:
    """Convert a string to a safe, short filename fragment."""
    s = re.sub(r'[^\w\-]', '_', s)
    s = re.sub(r'_+', '_', s).strip('_')
    return s[:max_len] if s else 'item'


def _persist_web_search_split(content: str, persist_dir: str,
                              safe_id: str) -> str | None:
    """Split web_search results into per-result files.

    Each search result is saved as a separate file.  Returns an index
    listing each result's title, URL, char count, and file path so
    the model can selectively read only the results it needs.

    Returns None if the content doesn't look like structured web_search
    output (falls through to default single-file persistence).
    """
    _SEP = '════════════════════'
    # ── Relocate vertical-search block(s) out of the split body ──
    # The vertical header (``═══ Vertical Search … ═══``) is prepended to the
    # tool content by the handler, but its 3-char ``═══`` rules are NOT the
    # 20-char ``════`` per-result boundary — so left in place the split glues
    # the block onto result-1's file and it drops out of the model's immediate
    # context (the debug-panel gap). Extract every block (single-search: one
    # leading block; batch: one per ``=== Search: <q> ===`` section) verbatim,
    # remove them from the body before splitting, and prepend them to the index.
    vert_blocks = []  # NEUTERED: relocation disabled
    if vert_blocks:
        # DATA-LOSS GUARD. ``_VERT_BLOCK_RE`` is lazy+DOTALL: if a block's
        # intended close ``═══ Web Search Results ═══`` is missing/truncated,
        # ``.*?`` runs FORWARD to the first such marker downstream — possibly
        # inside a real result body — and sub-ing that match out would DELETE
        # result text. A legitimate block contains NEITHER a per-result
        # ``════`` separator NOR a ``^[N]`` result marker (verified against
        # real blocks), so if any match does, the match over-reached →
        # abandon relocation and leave ``content`` untouched (worst case: the
        # block stays glued to result-1, the pre-fix cosmetic behaviour —
        # never data loss).
        if any(_SEP in b or re.search(r'^\[\d+\]', b, re.MULTILINE)
               for b in vert_blocks):
            vert_blocks = []
        else:
            stripped = _VERT_BLOCK_RE.sub('', content)
            # Only commit to the stripped body if it STILL looks like
            # structured search output. Otherwise keep the original — and
            # since ``content`` is never reassigned in that branch, a
            # subsequent ``return None`` falls through to single-file
            # persistence where the caller re-persists the ORIGINAL, block
            # intact (str is immutable; the local rebind never touched it).
            if re.search(r'^\[1\]', stripped, re.MULTILINE):
                content = stripped
            else:
                vert_blocks = []

    if not re.search(r'^\[1\]', content, re.MULTILINE):
        return None

    parts = content.split(_SEP)

    if len(parts) < 2:
        return None  # Only one result or no separators — use default

    index_lines = []
    for _vb in vert_blocks:
        _vb = _vb.strip()
        if _vb:
            index_lines.append(_vb + '\n')
    index_lines.append(
        f'Search returned {len(parts)} results, saved to separate files '
        f'(total {_human_size(len(content))}). The index below lists every '
        f'result with a preview.\n'
        f'IMPORTANT: do NOT stop after reading just one or two. Scan the whole '
        f'index, pick EVERY result relevant to the task, and read them TOGETHER '
        f'in a single read_files call (pass all the file paths in one `reads` '
        f'array — batched reads are far cheaper than one-at-a-time). A broad '
        f'search is wasted if you only open the top hit.\n')

    files_written = 0
    _SNIPPET_CHARS = 400

    for i, part in enumerate(parts, 1):
        part = part.strip()
        if not part:
            continue

        lines = part.split('\n')

        title = ''
        url = ''
        authority = ''
        has_content = False
        content_chars = 0
        for line in lines:
            m_title = re.match(r'^\[(\d+)\]\s*(.+)', line)
            if m_title and not title:
                title = m_title.group(2).strip()
            if line.strip().startswith('URL:'):
                url = line.strip()[4:].strip()
            if line.strip().startswith('Authority:'):
                authority = line.strip()[len('Authority:'):].strip()
            if '──── Full Page Content' in line:
                has_content = True

        if has_content:
            content_started = False
            for line in lines:
                if content_started:
                    content_chars += len(line) + 1
                elif '──── Full Page Content' in line:
                    content_started = True

        safe_title = _sanitize_filename(title) if title else f'result_{i}'
        filename = f'search_{safe_id}_{i}_{safe_title}.txt'
        filepath = os.path.join(persist_dir, filename)

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(part)
            files_written += 1
            _register_label(filepath, 'web_search', title)
        except Exception as e:
            logger.warning('[Persist] Failed to write split file %s: %s',
                           filepath, e)
            continue

        snippet = ''
        if has_content:
            content_started = False
            snippet_buf = []
            for line in lines:
                if content_started:
                    snippet_buf.append(line)
                    if sum(len(ll) for ll in snippet_buf) > _SNIPPET_CHARS:
                        break
                elif '──── Full Page Content' in line:
                    content_started = True
            snippet = ' '.join(snippet_buf)[:_SNIPPET_CHARS].strip()

        status = f'{_human_size(content_chars)} fetched' if has_content else 'snippet only'
        _auth_line = f'\n    Authority: {authority}' if authority else ''
        index_lines.append(
            f'[{i}] {title}\n'
            f'    URL: {url}{_auth_line}\n'
            f'    Status: {status}\n'
            f'    File: {filepath}'
        )
        if snippet:
            index_lines.append(f'    Preview: {snippet}…')

    if files_written < 2:
        return None

    logger.info('[Persist] web_search split into %d files in %s',
                files_written, persist_dir)

    return '\n'.join(index_lines)


def _persist_grep_search_split(content: str, persist_dir: str,
                               safe_id: str) -> str | None:
    """Split grep_search results by source file into per-file result files.

    Each source file's matches are saved as a separate file.  Returns an
    index listing each source file, its match count, and the saved file
    path so the model can selectively read only specific files' matches.

    Returns None if the content can't be parsed into per-file groups
    (falls through to default single-file persistence).
    """
    lines = content.split('\n')

    header = ''
    body_start = 0
    for idx, line in enumerate(lines):
        if line.strip() == '':
            body_start = idx + 1
            header = '\n'.join(lines[:idx])
            break
    else:
        return None

    file_groups: dict[str, list[str]] = {}
    current_file = None

    for line in lines[body_start:]:
        if not line.strip():
            continue
        if line.strip() == '--':
            if current_file and current_file in file_groups:
                file_groups[current_file].append(line)
            continue

        m = re.match(r'^([^:]+?):(\d+)[:：](.*)$', line)
        if not m:
            m = re.match(r'^([^-]+?)-(\d+)-(.*)$', line)
        if m:
            fpath = m.group(1)
            current_file = fpath
            if fpath not in file_groups:
                file_groups[fpath] = []
            file_groups[fpath].append(line)
        elif current_file:
            file_groups[current_file].append(line)

    if len(file_groups) < 2:
        return None

    index_lines = []
    index_lines.append(
        f'{header}\n'
        f'Results span {len(file_groups)} files '
        f'(total {_human_size(len(content))}). '
        f'Each file\'s matches saved separately — use read_files to read specific ones.\n'
    )

    files_written = 0
    _PREVIEW_LINES = 3

    for fpath, match_lines in file_groups.items():
        safe_fname = _sanitize_filename(
            fpath.replace('/', '_').replace('\\', '_')
        )
        filename = f'grep_{safe_id}_{safe_fname}.txt'
        filepath = os.path.join(persist_dir, filename)

        file_content = '\n'.join(match_lines)
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(file_content)
            files_written += 1
            _register_label(filepath, 'grep_search', fpath)
        except Exception as e:
            logger.warning('[Persist] Failed to write split file %s: %s',
                           filepath, e)
            continue

        match_count = sum(
            1 for ll in match_lines
            if re.match(r'^[^:]+?:\d+:', ll)
        )

        preview_lines = [
            ll for ll in match_lines[:_PREVIEW_LINES * 2]
            if ll.strip() and ll.strip() != '--'
        ][:_PREVIEW_LINES]
        preview = '\n    '.join(preview_lines)

        index_lines.append(
            f'  {fpath}  ({match_count} matches)\n'
            f'    File: {filepath}\n'
            f'    {preview}'
        )

    if files_written < 2:
        return None

    logger.info('[Persist] grep_search split into %d files in %s',
                files_written, persist_dir)

    return '\n'.join(index_lines)


def _persist_find_files_split(content: str, persist_dir: str,
                              safe_id: str) -> str | None:
    """Split batch ``find_files`` results into per-search files."""
    _HEADER_RE = re.compile(
        r'^Files matching "([^"]+)"(?: in ([^()]+?))? \((\d+) found\):',
        re.MULTILINE,
    )

    matches = list(_HEADER_RE.finditer(content))
    if len(matches) < 2:
        return None

    index_lines = [
        f'Batch find_files: {len(matches)} searches saved to separate files '
        f'(total {_human_size(len(content))}). '
        f'Use read_files to read individual search results.\n'
    ]

    files_written = 0
    _SNIPPET_LINES = 5

    for i, m in enumerate(matches, 1):
        start = m.start()
        end = matches[i].start() if i < len(matches) else len(content)
        section = content[start:end].rstrip()

        pattern = m.group(1)
        in_path = m.group(2) or ''
        found_count = m.group(3)

        safe_frag = _sanitize_filename(
            f'{pattern}_{in_path}'.replace('/', '_').replace('\\', '_')
        )
        filename = f'find_{safe_id}_{i}_{safe_frag}.txt'
        filepath = os.path.join(persist_dir, filename)

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(section)
            files_written += 1
            _register_label(filepath, 'find_files',
                            f'{pattern} in {in_path.strip()}' if in_path else pattern)
        except Exception as e:
            logger.warning('[Persist] Failed to write split file %s: %s',
                           filepath, e)
            continue

        lines = section.split('\n')
        body_lines = []
        seen_blank = False
        for ln in lines[1:]:
            if not seen_blank:
                if not ln.strip():
                    seen_blank = True
                continue
            if ln.strip():
                body_lines.append(ln.strip())
            if len(body_lines) >= _SNIPPET_LINES:
                break

        header_display = f'"{pattern}"' + (f' in {in_path.strip()}' if in_path else '')
        index_lines.append(
            f'[{i}] {header_display}  ({found_count} found)\n'
            f'    File: {filepath}'
        )
        if body_lines:
            preview = '\n    '.join(body_lines)
            index_lines.append(f'    Preview:\n    {preview}')

    if files_written < 2:
        return None

    logger.info('[Persist] find_files split into %d files in %s',
                files_written, persist_dir)

    return '\n'.join(index_lines)


def _persist_fetch_url_split(content: str, persist_dir: str,
                             safe_id: str) -> str | None:
    """Split batch ``fetch_url`` results into per-URL files."""
    _HEADER_RE = re.compile(
        r'^(?:Content from (\S+) \(([\d,]+) chars\):|Failed to fetch (\S+?)\.)',
        re.MULTILINE,
    )

    matches = list(_HEADER_RE.finditer(content))
    if len(matches) < 2:
        return None

    sections = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        section = content[start:end].rstrip()
        ok_url = m.group(1)
        reported_chars = m.group(2)
        fail_url = m.group(3)
        url = ok_url or fail_url or ''
        sections.append({
            'url': url,
            'ok': bool(ok_url),
            'reported_chars': reported_chars,
            'body': section,
        })

    index_lines = [
        f'Fetched {len(sections)} URLs saved to separate files '
        f'(total {_human_size(len(content))}). '
        f'Use read_files to read individual pages.\n'
    ]

    files_written = 0
    _SNIPPET_CHARS = 300

    for i, sec in enumerate(sections, 1):
        url = sec['url']
        try:
            from urllib.parse import urlparse
            u = urlparse(url)
            host = u.netloc or 'url'
            path_tail = u.path.rstrip('/').rsplit('/', 1)[-1] if u.path else ''
            safe_frag = _sanitize_filename(f'{host}_{path_tail}' if path_tail else host)
        except Exception as e:
            logger.debug('[Persist] URL parse failed for %s: %s', url[:80], e)
            safe_frag = _sanitize_filename(url)

        filename = f'fetch_{safe_id}_{i}_{safe_frag}.txt'
        filepath = os.path.join(persist_dir, filename)

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(sec['body'])
            files_written += 1
            _register_label(filepath, 'fetch_url', url)
        except Exception as e:
            logger.warning('[Persist] Failed to write split file %s: %s',
                           filepath, e)
            continue

        body = sec['body']
        nl = body.find('\n\n')
        snippet_src = body[nl + 2:] if nl >= 0 else body
        snippet = snippet_src[:_SNIPPET_CHARS].strip()
        if len(snippet_src) > _SNIPPET_CHARS:
            snippet += '…'

        status = ('OK, ' + sec['reported_chars'] + ' chars') if sec['ok'] else 'FAILED'
        index_lines.append(
            f'[{i}] {url}\n'
            f'    Status: {status}\n'
            f'    File: {filepath}'
        )
        if snippet:
            indented = snippet.replace('\n', '\n    ')
            index_lines.append(f'    Preview: {indented}')

    if files_written < 2:
        return None

    logger.info('[Persist] fetch_url split into %d files in %s',
                files_written, persist_dir)

    return '\n'.join(index_lines)


def _generate_web_search_preview(content: str) -> str:
    """Generate a structured preview for web_search results.

    Instead of dumbly taking the first N chars (which only shows the
    first result's content), parse the structured web_search output and
    generate a preview that includes title + URL + content snippet for
    ALL results.  This lets the model retain awareness of all search
    results and selectively read individual ones.
    """
    if not re.search(r'^\[1\]', content, re.MULTILINE):
        preview = content[:_PERSIST_PREVIEW_CHARS]
        last_nl = preview.rfind('\n')
        if last_nl > _PERSIST_PREVIEW_CHARS // 2:
            preview = preview[:last_nl]
        return preview

    _SEP = '════════════════════'
    parts = content.split(_SEP)

    preview_parts = []
    _CONTENT_SNIPPET_CHARS = 500
    _HEADER_MAX_CHARS = 300

    for part in parts:
        part = part.strip()
        if not part:
            continue

        lines = part.split('\n')
        header_lines = []
        content_start = -1
        for j, line in enumerate(lines):
            if '──── Full Page Content' in line or '────' in line:
                content_start = j + 1
                break
            header_lines.append(line)

        header = '\n'.join(header_lines).strip()
        if len(header) > _HEADER_MAX_CHARS:
            header = header[:_HEADER_MAX_CHARS] + '…'

        if content_start > 0 and content_start < len(lines):
            full_text = '\n'.join(lines[content_start:])
            snippet = full_text[:_CONTENT_SNIPPET_CHARS].rstrip()
            last_nl = snippet.rfind('\n', _CONTENT_SNIPPET_CHARS // 2)
            if last_nl > 0:
                snippet = snippet[:last_nl]
            preview_parts.append(
                f'{header}\n'
                f'    Content snippet: {snippet}\n'
                f'    ...'
            )
        else:
            preview_parts.append(header)

    if preview_parts:
        return '\n\n'.join(preview_parts)

    preview = content[:_PERSIST_PREVIEW_CHARS]
    last_nl = preview.rfind('\n')
    if last_nl > _PERSIST_PREVIEW_CHARS // 2:
        preview = preview[:last_nl]
    return preview


def _truncate_head_tail(content: str, tool_name: str, max_chars: int) -> str:
    """Legacy head+tail truncation fallback.

    Used only when disk persistence fails (e.g. permission errors).
    """
    original_len = len(content)
    head_budget = int(max_chars * 0.70)
    tail_budget = int(max_chars * 0.25)

    head = content[:head_budget]
    tail = content[-tail_budget:]

    truncation_note = (
        f'\n\n... [{original_len - head_budget - tail_budget:,} chars truncated — '
        f'result was {original_len:,} chars, budget is {max_chars:,}] ...\n\n'
    )

    logger.info('[Budget] %s result truncated (fallback): %s → %s (budget %s)',
                tool_name, _human_size(original_len),
                _human_size(head_budget + tail_budget),
                _human_size(max_chars))

    return head + truncation_note + tail
