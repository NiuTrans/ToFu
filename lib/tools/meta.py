"""lib/tools/meta.py — Tool result metadata builder for frontend UI display."""

import re
from collections import OrderedDict


def build_project_tool_meta(fn_name, fn_args, tool_content):
    """Build structured metadata dict for project tool results (shown in frontend).

    Returns a dict with keys: title, source, fetched, fetchedChars, url,
    snippet, badge, and tool-specific extras (writeOk, command, output, etc.).
    """
    chars = len(tool_content)
    meta = {'title': fn_name, 'source': 'Project', 'fetched': True,
            'fetchedChars': chars, 'url': ''}
    path = fn_args.get('path', '')

    # Check for MCP tools (dynamic names not in static dispatch table)
    from lib.mcp.types import MCP_TOOL_PREFIX
    if fn_name.startswith(MCP_TOOL_PREFIX):
        _build_mcp_tool(meta, fn_name, fn_args, tool_content, path)
    else:
        _META_BUILDERS.get(fn_name, _build_default)(meta, fn_name, fn_args, tool_content, path)
    return meta


# ──────────────────────────────────────────────────────────
#  Strategy Pattern — per-tool metadata builders
# ──────────────────────────────────────────────────────────

def _build_read_files(meta, fn_name, fn_args, tool_content, path):
    reads = fn_args.get('reads')
    if reads is None and 'path' in fn_args:
        reads = [fn_args]  # flat-args compat (DeepSeek etc.)
    if not reads:
        reads = []
    n_files = len(reads) if reads else 1
    if n_files <= 1:
        first = reads[0] if reads else None
        p = (first.get('path', path) if isinstance(first, dict) else path) if first else path
        m = re.search(r'\(lines (\d+)-(\d+) of (\d+)\)', tool_content[:200])
        if m:
            meta['snippet'] = f'{p}  L{m.group(1)}-{m.group(2)} of {m.group(3)}'
            meta['badge'] = f'L{m.group(1)}-{m.group(2)}'
        else:
            m2 = re.search(r'\((\d+) lines', tool_content[:200])
            total = m2.group(1) if m2 else '?'
            meta['snippet'] = f'{p}  {total} lines'
            meta['badge'] = f'{total}L'
    else:
        groups = OrderedDict()
        for r in reads:
            if not isinstance(r, dict):
                continue   # skip malformed entries (e.g. LLM passes strings)
            p = r.get('path', '?')
            sl, el = r.get('start_line'), r.get('end_line')
            groups.setdefault(p, [])
            if sl and el:
                groups[p].append(f'L{sl}-{el}')
            elif sl:
                groups[p].append(f'L{sl}+')
        n_unique = len(groups)
        basenames = [p.rsplit('/', 1)[-1] for p in groups]
        seen = {}
        for b in basenames:
            seen[b] = seen.get(b, 0) + 1
        dupes = {b for b, c in seen.items() if c > 1}
        parts_list = []
        for (p, ranges), b in zip(list(groups.items())[:4], basenames):
            name = p if b in dupes else b
            if ranges:
                parts_list.append(f'{name} {", ".join(ranges)}')
            else:
                parts_list.append(name)
        suffix = f' +{n_unique - 4} more' if n_unique > 4 else ''
        meta['snippet'] = f'{n_unique} file{"s" if n_unique > 1 else ""}: {"; ".join(parts_list)}{suffix}'
        meta['badge'] = f'{n_unique} file{"s" if n_unique > 1 else ""}'


def _build_grep_search(meta, fn_name, fn_args, tool_content, path):
    searches = fn_args.get('searches')
    if searches and isinstance(searches, list):
        # Batch mode: count total matches across all searches
        total_matches = 0
        for m_obj in re.finditer(r'(\d+) match', tool_content):
            total_matches += int(m_obj.group(1))
        n_searches = len(searches)
        pats = [s.get('pattern', '?')[:20] for s in searches[:3] if isinstance(s, dict)]
        suffix = f' +{n_searches - 3} more' if n_searches > 3 else ''
        meta['snippet'] = f'{n_searches} searches: /{"; /".join(pats)}/{suffix} → {total_matches} total matches'
        meta['badge'] = f'{n_searches} searches'
        return
    pattern = fn_args.get('pattern', '')
    include = fn_args.get('include', '')
    search_path = fn_args.get('path', '')
    m = re.search(r'(\d+) match', tool_content[:200])
    n = m.group(1) if m else '0'
    meta['snippet'] = (f'/{pattern}/'
                       + (f' in {include}' if include else '')
                       + (f' path={search_path}' if search_path else '')
                       + f'  → {n} matches')
    meta['badge'] = f'{n} matches'


_DIR_LINE_RE = re.compile(r'^  (?P<name>[^/\n]+)/ \(\d', re.MULTILINE)
_FILE_LINE_RE = re.compile(r'^  (?P<name>[^/\n]+[^/]) \((?:\d+L, )?\d', re.MULTILINE)


def _build_list_dir(meta, fn_name, fn_args, tool_content, path):
    # Count listed entries by parsing the plain-text output: directories
    # have a trailing slash before the `(N items)` suffix; files don't.
    nd = len(_DIR_LINE_RE.findall(tool_content))
    nf = len(_FILE_LINE_RE.findall(tool_content))
    meta['snippet'] = f'{path or "."}  {nd} dirs, {nf} files'
    meta['badge'] = f'{nd + nf} items'


def _build_find_files(meta, fn_name, fn_args, tool_content, path):
    searches = fn_args.get('searches')
    if searches and isinstance(searches, list):
        # Batch mode: count total found across all searches
        total_found = 0
        for m_obj in re.finditer(r'\((\d+) found\)', tool_content):
            total_found += int(m_obj.group(1))
        n_searches = len(searches)
        pats = [s.get('pattern', '?') for s in searches[:3] if isinstance(s, dict)]
        suffix = f' +{n_searches - 3} more' if n_searches > 3 else ''
        meta['snippet'] = f'{n_searches} finds: {", ".join(pats)}{suffix} → {total_found} total found'
        meta['badge'] = f'{n_searches} finds'
        return
    pattern = fn_args.get('pattern', '')
    m = re.search(r'\((\d+) found\)', tool_content[:200])
    n = m.group(1) if m else '0'
    meta['snippet'] = f'{pattern}  → {n} found'
    meta['badge'] = f'{n} found'


def _build_write_file(meta, fn_name, fn_args, tool_content, path):
    desc = fn_args.get('description', '')
    # Success lines start with "File created:" / "File updated:";
    # failures start with "Write failed:" or "write_file:" (bad path).
    low = tool_content.lower()
    ok = low.startswith('file created') or low.startswith('file updated')
    meta['snippet'] = f'{path}' + (f'  {desc}' if desc else '')
    meta['badge'] = ('created' if ok and 'created' in low
                     else ('updated' if ok else 'failed'))
    meta['writeOk'] = ok


def _build_apply_diff(meta, fn_name, fn_args, tool_content, path):
    edits = fn_args.get('edits')
    if edits and isinstance(edits, list):
        paths = list(dict.fromkeys(e.get('path', '?') for e in edits if isinstance(e, dict)))
        m = re.search(r'Applied (\d+)/(\d+)', tool_content[:200])
        ok_n = m.group(1) if m else '?'
        total_n = m.group(2) if m else str(len(edits))
        desc = fn_args.get('description', '')
        meta['snippet'] = (f'{len(paths)} file{"s" if len(paths) > 1 else ""}, '
                           f'{ok_n}/{total_n} edits' + (f'  {desc}' if desc else ''))
        meta['badge'] = f'{ok_n}/{total_n} edits'
        meta['writeOk'] = ok_n == total_n
        # Per-edit summaries for collapsible frontend display.
        # When the output is NOT a real batch result (no "Applied X/Y"
        # header — e.g. a read-gate refusal or a hard error), there are no
        # per-edit result lines; every edit must default to 'fail' rather
        # than 'ok' so the frontend doesn't render green checks for edits
        # that never ran.
        edit_lines = [l for l in tool_content.split('\n') if l.startswith('[')]
        default_status = 'ok' if m else 'fail'
        summaries = []
        for i, edit in enumerate(edits):
            if not isinstance(edit, dict):
                continue
            # An edit with no corresponding result line did not succeed.
            status = default_status if i < len(edit_lines) else 'fail'
            detail = ''
            if i < len(edit_lines):
                line = edit_lines[i]
                if '] FAIL' in line:
                    status = 'fail'
                    detail = line.split('FAIL', 1)[-1].strip().lstrip(': ')
                elif '] OK' in line:
                    status = 'ok'
                    detail = line.split('OK', 1)[-1].strip().lstrip(': ')
            summaries.append({
                'path': edit.get('path', '?'),
                'description': edit.get('description', ''),
                'status': status,
                'detail': detail[:120],
            })
        meta['editSummaries'] = summaries
    else:
        desc = fn_args.get('description', '')
        ok = tool_content.lower().startswith('applied diff')
        meta['snippet'] = f'{path}' + (f'  {desc}' if desc else '')
        meta['badge'] = 'patched' if ok else 'failed'
        meta['writeOk'] = ok


def _build_run_command(meta, fn_name, fn_args, tool_content, path):
    cmd = fn_args.get('command', '')
    # ★ Must anchor to END — command output may itself contain [exit code: N]
    m = re.search(r'\[exit code: (-?\d+)\]\s*$', tool_content)
    exit_code = m.group(1) if m else '?'
    timed_out = '[Command timed out]' in tool_content
    prefix = f'$ {cmd}\n'
    if tool_content.startswith(prefix):
        output_text = tool_content[len(prefix):]
    else:
        output_lines = tool_content.split('\n', 1)
        output_text = output_lines[1] if len(output_lines) > 1 else ''
    output_text = re.sub(r'\n?\[exit code: -?\d+\]\s*$', '', output_text).strip()
    output_text = re.sub(r'\n?\[Command timed out\].*$', '', output_text).strip()
    meta['command'] = cmd
    meta['description'] = fn_args.get('description', '')
    meta['output'] = output_text
    meta['exitCode'] = 'timeout' if timed_out else exit_code
    meta['timedOut'] = timed_out
    if timed_out:
        meta['snippet'] = f'$ {cmd[:120]}'
        meta['badge'] = 'timeout'
    elif exit_code == '0':
        meta['snippet'] = f'$ {cmd[:120]}'
        meta['badge'] = 'done'
    else:
        meta['snippet'] = f'$ {cmd[:120]}'
        meta['badge'] = f'exit {exit_code}'



def _build_insert_content(meta, fn_name, fn_args, tool_content, path):
    edits = fn_args.get('edits')
    if edits and isinstance(edits, list):
        paths = list(dict.fromkeys(e.get('path', '?') for e in edits if isinstance(e, dict)))
        m = re.search(r'Inserted (\d+)/(\d+)', tool_content[:200])
        ok_n = m.group(1) if m else '?'
        total_n = m.group(2) if m else str(len(edits))
        desc = fn_args.get('description', '')
        if len(paths) == 1:
            label = f'{paths[0]} ({ok_n}/{total_n} insertions)'
        elif len(paths) <= 3:
            label = f'{", ".join(paths)} ({ok_n}/{total_n} insertions)'
        else:
            label = f'{len(paths)} files ({ok_n}/{total_n} insertions)'
        meta['snippet'] = label + (f'  {desc}' if desc else '')
        meta['badge'] = f'{ok_n}/{total_n} inserted'
        meta['writeOk'] = ok_n == total_n
        # Per-edit summaries for collapsible frontend display.
        # See _build_apply_diff: default to 'fail' when the output is not a
        # real batch result (no "Inserted X/Y" header) or an edit has no
        # corresponding per-edit result line, so the frontend never renders
        # green checks for insertions that never ran.
        edit_lines = [l for l in tool_content.split('\n') if l.startswith('[')]
        default_status = 'ok' if m else 'fail'
        summaries = []
        for i, edit in enumerate(edits):
            if not isinstance(edit, dict):
                continue
            status = default_status if i < len(edit_lines) else 'fail'
            detail = ''
            if i < len(edit_lines):
                line = edit_lines[i]
                if '] FAIL' in line:
                    status = 'fail'
                    detail = line.split('FAIL', 1)[-1].strip().lstrip(': ')
                elif '] OK' in line:
                    status = 'ok'
                    detail = line.split('OK', 1)[-1].strip().lstrip(': ')
            summaries.append({
                'path': edit.get('path', '?'),
                'description': edit.get('description', ''),
                'status': status,
                'detail': detail[:120],
            })
        meta['editSummaries'] = summaries
    else:
        desc = fn_args.get('description', '')
        ok = tool_content.lower().startswith('inserted ')
        position = fn_args.get('position', 'after')
        meta['snippet'] = f'{path} ({position})' + (f'  {desc}' if desc else '')
        meta['badge'] = 'inserted' if ok else 'failed'
        meta['writeOk'] = ok


def _build_create_project(meta, fn_name, fn_args, tool_content, path):
    ok = not tool_content.lower().startswith('create_project failed')
    p = fn_args.get('path', '') or path
    name = fn_args.get('name', '')
    if ok:
        meta['snippet'] = f'{p}' + (f'  name={name}' if name else '')
        meta['badge'] = 'created'
    else:
        meta['snippet'] = f'{p} — {tool_content[:120]}'
        meta['badge'] = 'failed'
    meta['writeOk'] = ok


def _build_default(meta, fn_name, fn_args, tool_content, path):
    meta['snippet'] = tool_content[:120].replace('\n', ' ')
    meta['badge'] = ''


def _build_mcp_tool(meta, fn_name, fn_args, tool_content, path):
    from lib.mcp.types import parse_namespaced_name
    parsed = parse_namespaced_name(fn_name)
    is_error = isinstance(tool_content, str) and tool_content.startswith(
        ('❌', 'MCP Error', 'MCP tool error', 'MCP server not connected'))
    if parsed:
        server_name, tool_name = parsed
        meta['title'] = f'🔌 {server_name}/{tool_name}'
        meta['source'] = f'MCP:{server_name}'
        meta['badge'] = f'❌ {server_name}' if is_error else f'🔌 {server_name}'
    else:
        meta['title'] = f'🔌 {fn_name}'
        meta['badge'] = '❌ MCP' if is_error else '🔌 MCP'
    meta['snippet'] = tool_content[:120].replace('\n', ' ')


# Module-level dispatch table — O(1) lookup replaces if/elif chain
# NOTE: 'read_file' (singular) is an obsolete tool name kept only for
# backward compatibility with old persisted tool-round metadata in saved
# conversations. New code always emits 'read_files' (plural).
_META_BUILDERS = {
    'read_file':    _build_read_files,   # legacy — persisted tool rounds only
    'read_files':   _build_read_files,
    'grep_search':  _build_grep_search,
    'list_dir':     _build_list_dir,
    'find_files':   _build_find_files,
    'write_file':   _build_write_file,
    'apply_diff':   _build_apply_diff,
    'apply_diffs':  _build_apply_diff,
    'run_command':  _build_run_command,
    'insert_content': _build_insert_content,
    'insert_contents': _build_insert_content,
    'create_project': _build_create_project,
}


__all__ = ['build_project_tool_meta']
