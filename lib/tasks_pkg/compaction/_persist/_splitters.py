"""Format-aware per-tool splitters for oversized tool results.

Each splitter fans an oversized structured result out into one file per
item and returns an INDEX (the new LLM-facing content) so the model can
selectively ``read_files`` only the items it needs. Returns ``None`` when
the content doesn't look like the tool's structured output, so the caller
falls through to default single-file persistence.

  * ``_persist_web_search_split``  → one file per search hit
  * ``_persist_grep_search_split`` → one file per matched source file
  * ``_persist_find_files_split``  → one file per batched search section
  * ``_persist_fetch_url_split``   → one file per URL
  * ``_generate_web_search_preview`` → structured preview (no disk writes)

Depends only on ``_helpers`` (leaf) + the ``persist_registry`` label seam.
"""

import os
import re

from lib.log import get_logger
from lib.tasks_pkg.persist_registry import register as _register_label
from lib.tasks_pkg.compaction._constants import _PERSIST_PREVIEW_CHARS
from lib.tasks_pkg.compaction._persist._helpers import (
    _VERT_BLOCK_RE,
    _human_size,
    _sanitize_filename,
)

logger = get_logger(__name__)


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
    vert_blocks = _VERT_BLOCK_RE.findall(content)
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
