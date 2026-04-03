"""lib/tools/meta.py — Tool result metadata builder for frontend UI display."""

import os
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

    _META_BUILDERS.get(fn_name, _build_default)(meta, fn_name, fn_args, tool_content, path)
    return meta


# ──────────────────────────────────────────────────────────
#  Strategy Pattern — per-tool metadata builders
# ──────────────────────────────────────────────────────────

def _build_read_files(meta, fn_name, fn_args, tool_content, path):
    reads = fn_args.get('reads', [])
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


def _build_list_dir(meta, fn_name, fn_args, tool_content, path):
    nd = tool_content.count('📁')
    nf = tool_content.count('📄')
    meta['snippet'] = f'{path or "."}  {nd} dirs, {nf} files'
    meta['badge'] = f'{nd + nf} items'


def _build_find_files(meta, fn_name, fn_args, tool_content, path):
    pattern = fn_args.get('pattern', '')
    m = re.search(r'\((\d+) found\)', tool_content[:200])
    n = m.group(1) if m else '0'
    meta['snippet'] = f'{pattern}  → {n} found'
    meta['badge'] = f'{n} found'


def _build_write_file(meta, fn_name, fn_args, tool_content, path):
    desc = fn_args.get('description', '')
    ok = '✅' in tool_content
    meta['snippet'] = f'{path}' + (f'  {desc}' if desc else '')
    meta['badge'] = ('created' if ok and 'created' in tool_content.lower()
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
    else:
        desc = fn_args.get('description', '')
        ok = '✅' in tool_content
        meta['snippet'] = f'{path}' + (f'  {desc}' if desc else '')
        meta['badge'] = 'patched' if ok else 'failed'
        meta['writeOk'] = ok


def _build_run_command(meta, fn_name, fn_args, tool_content, path):
    cmd = fn_args.get('command', '')
    # ★ Must anchor to END — command output may itself contain [exit code: N]
    m = re.search(r'\[exit code: (-?\d+)\]\s*$', tool_content)
    exit_code = m.group(1) if m else '?'
    timed_out = '⏰' in tool_content
    prefix = f'$ {cmd}\n'
    if tool_content.startswith(prefix):
        output_text = tool_content[len(prefix):]
    else:
        output_lines = tool_content.split('\n', 1)
        output_text = output_lines[1] if len(output_lines) > 1 else ''
    output_text = re.sub(r'\n?\[exit code: -?\d+\]\s*$', '', output_text).strip()
    output_text = re.sub(r'\n?⏰ Command timed out.*$', '', output_text).strip()
    meta['command'] = cmd
    meta['output'] = output_text
    meta['exitCode'] = 'timeout' if timed_out else exit_code
    meta['timedOut'] = timed_out
    if timed_out:
        meta['snippet'] = f'$ {cmd[:120]}'
        meta['badge'] = '⏰ timeout'
    elif exit_code == '0':
        meta['snippet'] = f'$ {cmd[:120]}'
        meta['badge'] = '✓ done'
    else:
        meta['snippet'] = f'$ {cmd[:120]}'
        meta['badge'] = f'✗ exit {exit_code}'


def _build_read_local_file(meta, fn_name, fn_args, tool_content, path):
    file_path = fn_args.get('path', '?')
    filename = os.path.basename(file_path) if file_path else '?'
    ext = os.path.splitext(filename)[1].lower() if filename else ''
    chars = len(tool_content)
    if tool_content.startswith('❌'):
        meta['snippet'] = tool_content[:120].replace('\n', ' ')
        meta['badge'] = '❌ failed'
    elif ext == '.pdf':
        meta['snippet'] = f'{filename} — {chars:,} chars extracted'
        meta['badge'] = '📄 PDF'
    elif ext in ('.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls'):
        meta['snippet'] = f'{filename} — {chars:,} chars extracted'
        meta['badge'] = f'📄 {ext}'
    else:
        meta['snippet'] = f'{filename} — {chars:,} chars'
        meta['badge'] = f'{chars:,} chars'


def _build_default(meta, fn_name, fn_args, tool_content, path):
    meta['snippet'] = tool_content[:120].replace('\n', ' ')
    meta['badge'] = ''


# Module-level dispatch table — O(1) lookup replaces if/elif chain
_META_BUILDERS = {
    'read_file':    _build_read_files,
    'read_files':   _build_read_files,
    'grep_search':  _build_grep_search,
    'list_dir':     _build_list_dir,
    'find_files':   _build_find_files,
    'write_file':   _build_write_file,
    'apply_diff':   _build_apply_diff,
    'run_command':  _build_run_command,
    'read_local_file': _build_read_local_file,
}


__all__ = ['build_project_tool_meta']
