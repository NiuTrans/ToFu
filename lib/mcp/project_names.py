"""Id → human-readable name cache for MCP tools.

Humans don't recognize resources by their opaque IDs — Overleaf's 24-hex
project IDs, Xuecheng's 10-digit ``contentId``s, etc. The UI title line
therefore prefers a cached natural name over the bare ID whenever possible.

Two independent namespaces live in this module:

* **overleaf project** — 24-hex MongoDB ObjectID → project name
* **xuecheng doc**     — numeric doc/``contentId`` → document title

The cache is populated opportunistically from MCP tool results:

  Overleaf:
    - ``mcp__overleaf__list_projects``        → JSON array with ``id``/``name``
    - ``mcp__overleaf__status_summary``       → text with ``project_id`` + ``project_name``
    - ``mcp__overleaf__create_project``       → text/JSON with new id + name

  Xuecheng:
    - ``mcp__xuecheng__xuecheng_read_doc``       → ``{contentId, title, …}``
    - ``mcp__xuecheng__xuecheng_get_doc_meta``   → ``{meta: {contentId, contentTitle, …}}``
    - ``mcp__xuecheng__xuecheng_create_document``→ ``{contentId, title, url}``
    - ``mcp__xuecheng__xuecheng_search``         → ``{items: [{contentId, title, …}]}``
    - ``mcp__xuecheng__xuecheng_list_children``  → list of ``{contentId, contentTitle}``

Call :func:`ingest_tool_result` right after any MCP call finishes and it
will pick up identifiers it recognises. Every learned value is owner-scoped;
one bounded LRU includes names, URLs and learned bases across all owners.
Ownerless reads cannot access private cache state. Ingestion bounds text, depth,
visited nodes and value size.

This module intentionally has zero dependencies on the MCP bridge so it
can be imported from :mod:`lib.tasks_pkg.tool_display` without circular-
import risk.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from urllib.parse import urlsplit
import re
import threading

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'get_project_name',
    'set_project_name',
    'get_doc_title',
    'set_doc_title',
    'get_project_url',
    'get_doc_url',
    'ingest_tool_result',
    'clear_cache',
]

# One process-wide capacity includes all owners, namespaces and learned bases.
# Ownership partitions keys; it never multiplies the resident entry budget.
from runtime_guards import resolve_resource_budget

_MAX_ENTRIES = resolve_resource_budget('TOFU_MCP_DISPLAY_CACHE_ENTRIES', maximum=2000)
_MAX_LABEL_CHARS = 512
_MAX_INGEST_CHARS = 65536
_MAX_INGEST_NODES = 256
_MAX_INGEST_DEPTH = 8
_cache = OrderedDict()
_lock = threading.Lock()
_DEFAULT_OVERLEAF_BASE = 'https://www.overleaf.com'
_DEFAULT_XUECHENG_BASE = 'https://km.internal.example.com'
_OVERLEAF_URL_RE = re.compile(r'(https?://[^\s/]+)/project/([0-9a-f]{24})')
_KM_FULL_URL_RE = re.compile(r'(https?://[^\s/]+)/(?:collabpage|page)/(\d{6,15})')
_PID_RE = re.compile(r'[0-9a-f]{24}')
_DOC_ID_RE = re.compile(r'^\d{6,15}$')


def _owner(user_id):
    from lib.identity import require_user_id
    return require_user_id(user_id, context='MCP display cache owner')


def _read(kind, identifier, user_id):
    if user_id is None:
        return ''  # Ownerless historical display must not read private caches.
    key = (_owner(user_id), kind, identifier)
    with _lock:
        value = _cache.get(key, '')
        if value:
            _cache.move_to_end(key)
        return value


def _write(kind, identifier, value, user_id):
    key = (_owner(user_id), kind, identifier)
    with _lock:
        previous = _cache.get(key)
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > _MAX_ENTRIES:
            _cache.popitem(last=False)
    return previous != value


def _project_id(value):
    if not isinstance(value, str) or len(value) > 64:
        return ''
    value = value.strip().lower()
    return value if _PID_RE.fullmatch(value) else ''


def _doc_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ''
    if isinstance(value, int) and not 100000 <= value <= 999999999999999:
        return ''
    if isinstance(value, str) and len(value) > 32:
        return ''
    value = str(value).strip()
    return value if _DOC_ID_RE.fullmatch(value) else ''


def get_project_name(project_id: str, *, user_id=None) -> str:
    return _read('project_name', _project_id(project_id), user_id)


def set_project_name(project_id: str, name: str, *, user_id) -> None:
    owner = _owner(user_id)
    pid = _project_id(project_id)
    label = name[:_MAX_LABEL_CHARS].strip() if isinstance(name, str) else ''
    if pid and label:
        _write('project_name', pid, label, owner)


def get_doc_title(content_id, *, user_id=None) -> str:
    return _read('doc_title', _doc_id(content_id), user_id)


def set_doc_title(content_id, title: str, *, user_id) -> None:
    owner = _owner(user_id)
    cid = _doc_id(content_id)
    label = title[:_MAX_LABEL_CHARS].strip() if isinstance(title, str) else ''
    if cid and label:
        _write('doc_title', cid, label, owner)


def get_project_url(project_id: str, *, user_id=None) -> str:
    pid = _project_id(project_id)
    if not pid:
        return ''
    cached = _read('project_url', pid, user_id)
    base = _read('overleaf_base', '', user_id) or _DEFAULT_OVERLEAF_BASE
    return cached or f"{base.rstrip('/')}/project/{pid}"


def get_doc_url(content_id, *, user_id=None) -> str:
    cid = _doc_id(content_id)
    if not cid:
        return ''
    cached = _read('doc_url', cid, user_id)
    base = _read('xuecheng_base', '', user_id) or _DEFAULT_XUECHENG_BASE
    return cached or f"{base.rstrip('/')}/collabpage/{cid}"


def clear_cache(*, user_id=None) -> None:
    """Drop one owner's reconstructible projection, or all state for tests."""
    owner = _owner(user_id) if user_id is not None else None
    with _lock:
        if owner is None:
            _cache.clear()
        else:
            for key in list(_cache):
                if key[0] == owner:
                    del _cache[key]


def _safe_base(value):
    if not isinstance(value, str) or len(value) > 512 or any(c in value for c in '<>"\\\'`%'):
        return ''
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or parsed.username is not None or parsed.password is not None:
            return ''
        host = parsed.hostname or ''
        if ':' in host:
            import ipaddress
            ipaddress.IPv6Address(host)
        elif not re.fullmatch(r'[a-zA-Z0-9.-]+', host.encode('idna').decode('ascii')):
            return ''
        parsed.port  # noqa: B018 — Reject malformed or out-of-range port syntax.
        return value
    except (ValueError, UnicodeError):
        return ''


# ── Ingestion from tool results ──────────────────────────────────────

def _try_json(text: str):
    """Best-effort JSON parse — returns None on any failure."""
    if not isinstance(text, str) or not text:
        return None
    s = text.strip()
    # Strip common markdown code fences the model sometimes leaves in
    if s.startswith('```'):
        s = s.strip('`')
        nl = s.find('\n')
        if nl != -1:
            s = s[nl + 1:]
    if not (s.startswith('{') or s.startswith('[')):
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as _e_audit:
        logger.debug('[project_names] _try_json caught %s: %s', type(_e_audit).__name__, _e_audit)
        return None


def _ingest_obj(obj, *, user_id, allow_projects, allow_docs) -> int:
    """Bounded iterative extraction; cycles/deep or huge containers cannot fan out."""
    added = visited = 0
    stack = [(iter((obj,)), 0)]
    while stack and visited < _MAX_INGEST_NODES:
        iterator, depth = stack[-1]
        try:
            value = next(iterator)
        except StopIteration:
            stack.pop()
            continue
        visited += 1
        if isinstance(value, dict):
            if allow_projects:
                pid = value.get('id') or value.get('project_id')
                name = value.get('name') or value.get('project_name') or value.get('title')
                before = get_project_name(pid, user_id=user_id)
                set_project_name(pid, name, user_id=user_id)
                added += bool(get_project_name(pid, user_id=user_id) != before)
            if allow_docs:
                cid = value.get('contentId') or value.get('content_id')
                title = value.get('contentTitle') or value.get('title')
                before = get_doc_title(cid, user_id=user_id)
                set_doc_title(cid, title, user_id=user_id)
                added += bool(get_doc_title(cid, user_id=user_id) != before)
            if depth < _MAX_INGEST_DEPTH:
                stack.append((iter(value.values()), depth + 1))
        elif isinstance(value, list) and depth < _MAX_INGEST_DEPTH:
            stack.append((iter(value), depth + 1))
    return added


def ingest_tool_result(fn_name: str, fn_args: dict | None, tool_content, *, user_id) -> int:
    """Harvest id → name mappings from an MCP tool result.

    Called after every MCP call completes. Safe to invoke for any tool:
    it early-exits for unknown servers and silently ignores content it
    can't parse. Returns the number of new/updated mappings learned.

    Also records the ``create_project`` text shape (overleaf), plus the
    request-side ``doc`` arg paired with title fields harvested from a
    ``read_doc`` result (xuecheng).
    """
    user_id = _owner(user_id)
    if not isinstance(fn_name, str) or len(fn_name) > 256:
        return 0
    if isinstance(tool_content, str):
        tool_content = tool_content[:_MAX_INGEST_CHARS]
    is_overleaf = '__overleaf__' in fn_name
    is_xuecheng = '__xuecheng__' in fn_name
    if not (is_overleaf or is_xuecheng):
        return 0

    learned = 0

    # 1) Structured JSON results (list_projects, get_sections, …)
    parsed = _try_json(tool_content) if isinstance(tool_content, str) else tool_content
    if parsed is not None:
        try:
            learned += _ingest_obj(parsed, user_id=user_id, allow_projects=is_overleaf, allow_docs=is_xuecheng)
        except Exception as e:  # noqa: BLE001 - defensive
            logger.debug('[OverleafNames] ingest parse failed: %s', e)

    # 2) Our friendly create_project text response embeds both pieces:
    #      "✅ Created Overleaf project [My Paper]\n"
    #      "   project_id: 69f2114b31a22a8b1f4fcca7  (short: 69f21…cca7)\n"
    if is_overleaf and isinstance(tool_content, str) and 'project_id:' in tool_content:
        m_pid = re.search(r'project_id:\s*([0-9a-f]{24})', tool_content)
        m_name = re.search(r'\[([^\]\n]{1,80})\]', tool_content)
        if m_pid and m_name:
            set_project_name(m_pid.group(1), m_name.group(1), user_id=user_id)
            learned += 1

    # 3) status_summary renders as plain text with a header like
    #       📄 Project: <title>  [<24-hex-id>]
    #    Anchor on the 24-hex ID (enclosed in [] or ()) and pull the
    #    project title from the ``(?:project|name):`` label that
    #    immediately precedes it on the same line. Title may itself
    #    contain ``[...]`` substrings like ``[EMNLP Demo] Tofu`` so we
    #    can't exclude ``[`` from the value class — we tether on the ID.
    if is_overleaf and isinstance(tool_content, str):
        m = re.search(
            r'(?im)(?:project|name)\s*:\s*(.{1,120}?)\s*[\[(]([0-9a-f]{24})[\])]',
            tool_content,
        )
        if m:
            title = m.group(1).strip()
            # Guard against empty titles after stripping
            if title:
                set_project_name(m.group(2), title, user_id=user_id)
                learned += 1

    # 4) Xuecheng-specific: when the request carried ``doc`` (which can be
    #    either a numeric contentId or a full collabpage URL) and the
    #    result has a top-level ``title`` — pair them. ``read_doc`` returns
    #    ``{ok, title, markdown, …}`` but does NOT echo contentId, so the
    #    generic walker above misses this case.
    if is_xuecheng and isinstance(fn_args, dict) and parsed is not None:
        cid = _extract_doc_id(fn_args.get('doc'))
        if cid:
            title = ''
            if isinstance(parsed, dict):
                t = parsed.get('title')
                if isinstance(t, str) and t[:_MAX_LABEL_CHARS].strip():
                    title = t
                else:
                    meta = parsed.get('meta')
                    if isinstance(meta, dict):
                        t2 = meta.get('contentTitle') or meta.get('title')
                        if isinstance(t2, str) and t2[:_MAX_LABEL_CHARS].strip():
                            title = t2
            if title:
                before = get_doc_title(cid, user_id=user_id)
                set_doc_title(cid, title, user_id=user_id)
                if before != title and get_doc_title(cid, user_id=user_id) == title:
                    learned += 1

    # 5) Harvest full URLs embedded in the result text so the tool-call line
    #    can become a clickable link. Overleaf write/create tools and the
    #    create_document/read_doc responses embed a full ``…/project/<id>`` or
    #    ``…/collabpage/<id>`` URL — record it per id AND learn the deployment
    #    base (lets self-hosted Overleaf instances link correctly).
    if isinstance(tool_content, str) and tool_content:
        patterns = ((_OVERLEAF_URL_RE, 'overleaf_base', 'project_url'),) if is_overleaf else (
            (_KM_FULL_URL_RE, 'xuecheng_base', 'doc_url'),)
        for pattern, base_kind, resource_kind in patterns:
            for index, match in enumerate(pattern.finditer(tool_content)):
                if index >= _MAX_INGEST_NODES:
                    break
                base = _safe_base(match.group(1))
                if not base:
                    continue
                if not _read(base_kind, '', user_id):
                    _write(base_kind, '', base, user_id)
                _write(resource_kind, match.group(2), match.group(0), user_id)

    if learned:
        logger.debug('[McpNames] %s learned %d names', fn_name, learned)
    return learned


_KM_DOC_URL_RE = re.compile(r'/(?:collabpage|page)/(\d{6,15})')


def _extract_doc_id(val) -> str:
    """Normalise a Xuecheng ``doc`` arg to a bare numeric contentId."""
    if isinstance(val, int):
        return _doc_id(val)
    if not isinstance(val, str) or len(val) > 2048:
        return ''
    s = val.strip()
    if not s:
        return ''
    m = _KM_DOC_URL_RE.search(s)
    if m:
        return m.group(1)
    if _DOC_ID_RE.fullmatch(s):
        return s
    return ''
