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
will pick up identifiers it recognises. The cache is a plain dict guarded
by a lock (writes are rare — a few per session — and reads are cheap).

This module intentionally has zero dependencies on the MCP bridge so it
can be imported from :mod:`lib.tasks_pkg.tool_display` without circular-
import risk.
"""

from __future__ import annotations

import json
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

# ── Cache storage ────────────────────────────────────────────────────
_cache: dict[str, str] = {}                # overleaf project_id → name
_doc_cache: dict[str, str] = {}            # xuecheng contentId → title
_proj_url_cache: dict[str, str] = {}       # overleaf project_id → full URL
_doc_url_cache: dict[str, str] = {}        # xuecheng contentId → full URL
_lock = threading.Lock()

# Overleaf base URL used to synthesize a project link when only the
# project_id is known. Defaults to the public Overleaf SaaS host (the same
# default the overleaf-mcp server itself uses); learned/overridden at runtime
# from any tool result that embeds a ``<scheme>://<host>/project/<id>`` URL,
# so self-hosted Overleaf instances link correctly too.
_DEFAULT_OVERLEAF_BASE = 'https://www.overleaf.com'
_overleaf_base: str = ''

# Xuecheng base URL used to synthesize a doc link when only the contentId is
# known. Defaults to the internal KM host the xuecheng-mcp registry entry
# targets; learned/overridden at runtime from any tool result that embeds a
# ``<scheme>://<host>/collabpage|page/<id>`` URL, so a different deployment
# links correctly too.
_DEFAULT_XUECHENG_BASE = 'https://km.internal.example.com'
_xuecheng_base: str = ''

# Matches a full Overleaf project URL so we can learn the deployment's base.
_OVERLEAF_URL_RE = re.compile(r'(https?://[^\s/]+)/project/([0-9a-f]{24})')
# Matches a full Xuecheng doc URL: group(0) is the whole URL, group(1) the
# deployment base, group(2) the contentId.
_KM_FULL_URL_RE = re.compile(r'(https?://[^\s/]+)/(?:collabpage|page)/(\d{6,15})')

# 24-char lowercase hex MongoDB ObjectID
_PID_RE = re.compile(r'\b([0-9a-f]{24})\b')

# Max entries to keep in each cache — accounts rarely accumulate more
# than a few hundred unique resources, but cap defensively.
_MAX_ENTRIES = 2000

# Numeric ``contentId`` — Xuecheng IDs are 8-12 digits today; the regex
# accepts 6-15 to be future-proof without matching arbitrary numbers.
_DOC_ID_RE = re.compile(r'^\d{6,15}$')


def get_project_name(project_id: str) -> str:
    """Return the cached human-readable name for ``project_id``, or ''."""
    if not project_id:
        return ''
    with _lock:
        return _cache.get(project_id, '')


def set_project_name(project_id: str, name: str) -> None:
    """Record a project_id → name mapping in the cache."""
    pid = (project_id or '').strip().lower()
    nm = (name or '').strip()
    if not pid or not nm or not _PID_RE.fullmatch(pid):
        return
    with _lock:
        if len(_cache) >= _MAX_ENTRIES and pid not in _cache:
            # Evict a random existing key (cheap & good-enough for a cap
            # we don't expect to hit in practice).
            _cache.pop(next(iter(_cache)), None)
        prev = _cache.get(pid)
        _cache[pid] = nm
    if prev != nm:
        logger.debug('[OverleafNames] cached %s → %s', pid[:8], nm[:40])


def get_doc_title(content_id) -> str:
    """Return the cached Xuecheng doc title for ``content_id``, or ''.

    Accepts either a string or int; normalised to its decimal string form
    before lookup.
    """
    if content_id is None:
        return ''
    cid = str(content_id).strip()
    if not cid:
        return ''
    with _lock:
        return _doc_cache.get(cid, '')


def set_doc_title(content_id, title: str) -> None:
    """Record a Xuecheng contentId → title mapping in the cache."""
    cid = str(content_id or '').strip()
    nm = (title or '').strip()
    if not cid or not nm or not _DOC_ID_RE.fullmatch(cid):
        return
    with _lock:
        if len(_doc_cache) >= _MAX_ENTRIES and cid not in _doc_cache:
            _doc_cache.pop(next(iter(_doc_cache)), None)
        prev = _doc_cache.get(cid)
        _doc_cache[cid] = nm
    if prev != nm:
        logger.debug('[XuechengTitles] cached %s → %.40s', cid, nm)


def get_project_url(project_id: str) -> str:
    """Return a clickable URL for an Overleaf ``project_id``.

    Prefers a URL harvested verbatim from a tool result (so self-hosted
    deployments and exact paths are honored); falls back to synthesizing
    ``<base>/project/<id>`` from the learned-or-default Overleaf base URL.
    Returns '' only when ``project_id`` is not a 24-hex ObjectID.
    """
    pid = (project_id or '').strip().lower()
    if not _PID_RE.fullmatch(pid):
        return ''
    with _lock:
        cached = _proj_url_cache.get(pid)
        base = _overleaf_base or _DEFAULT_OVERLEAF_BASE
    if cached:
        return cached
    return f"{base.rstrip('/')}/project/{pid}"


def get_doc_url(content_id) -> str:
    """Return a clickable Xuecheng URL for ``content_id``.

    Prefers a URL harvested verbatim from a tool result (so the exact path a
    deployment used is honored); falls back to synthesizing
    ``<base>/collabpage/<id>`` from the learned-or-default Xuecheng base URL.
    Returns '' only when ``content_id`` is not a numeric contentId.
    """
    if content_id is None:
        return ''
    cid = str(content_id).strip()
    if not _DOC_ID_RE.fullmatch(cid):
        return ''
    with _lock:
        cached = _doc_url_cache.get(cid)
        base = _xuecheng_base or _DEFAULT_XUECHENG_BASE
    if cached:
        return cached
    return f"{base.rstrip('/')}/collabpage/{cid}"


def clear_cache() -> None:
    """Empty all caches — intended for tests."""
    global _overleaf_base, _xuecheng_base
    with _lock:
        _cache.clear()
        _doc_cache.clear()
        _proj_url_cache.clear()
        _doc_url_cache.clear()
        _overleaf_base = ''
        _xuecheng_base = ''


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
    except (json.JSONDecodeError, TypeError, ValueError) as _e_audit:
        logger.debug('[project_names] _try_json caught %s: %s', type(_e_audit).__name__, _e_audit)
        return None


def _ingest_obj(obj) -> int:
    """Recursively extract id→name pairs from any structured tool result.

    Handles both shapes used by the MCP servers we know about:

    * Overleaf — ``{id|project_id, name|project_name|title}`` pairs.
    * Xuecheng — ``{contentId, contentTitle|title}`` pairs.

    Other shapes are ignored — recursion still descends into nested dicts
    and lists so e.g. an embedded ``items: [...]`` list is harvested.
    """
    added = 0
    if isinstance(obj, dict):
        # Overleaf — 24-hex project_id → name
        pid = obj.get('id') or obj.get('project_id')
        name = obj.get('name') or obj.get('project_name') or obj.get('title')
        if pid and name and isinstance(pid, str) and isinstance(name, str):
            before = get_project_name(pid)
            set_project_name(pid, name)
            if before != name and get_project_name(pid) == name:
                added += 1

        # Xuecheng — numeric contentId → title
        cid = obj.get('contentId') or obj.get('content_id')
        ctitle = obj.get('contentTitle') or obj.get('title')
        if cid is not None and ctitle and isinstance(ctitle, str):
            before = get_doc_title(cid)
            set_doc_title(cid, ctitle)
            if before != ctitle and get_doc_title(cid) == ctitle:
                added += 1

        for v in obj.values():
            if isinstance(v, (dict, list)):
                added += _ingest_obj(v)
    elif isinstance(obj, list):
        for item in obj:
            added += _ingest_obj(item)
    return added


def ingest_tool_result(fn_name: str, fn_args: dict | None, tool_content) -> int:
    """Harvest id → name mappings from an MCP tool result.

    Called after every MCP call completes. Safe to invoke for any tool:
    it early-exits for unknown servers and silently ignores content it
    can't parse. Returns the number of new/updated mappings learned.

    Also records the ``create_project`` text shape (overleaf), plus the
    request-side ``doc`` arg paired with title fields harvested from a
    ``read_doc`` result (xuecheng).
    """
    if not isinstance(fn_name, str):
        return 0
    is_overleaf = '__overleaf__' in fn_name
    is_xuecheng = '__xuecheng__' in fn_name
    if not (is_overleaf or is_xuecheng):
        return 0

    learned = 0

    # 1) Structured JSON results (list_projects, get_sections, …)
    parsed = _try_json(tool_content) if isinstance(tool_content, str) else tool_content
    if parsed is not None:
        try:
            learned += _ingest_obj(parsed)
        except Exception as e:  # noqa: BLE001 - defensive
            logger.debug('[OverleafNames] ingest parse failed: %s', e)

    # 2) Our friendly create_project text response embeds both pieces:
    #      "✅ Created Overleaf project [My Paper]\n"
    #      "   project_id: 69f2114b31a22a8b1f4fcca7  (short: 69f21…cca7)\n"
    if isinstance(tool_content, str) and 'project_id:' in tool_content:
        m_pid = re.search(r'project_id:\s*([0-9a-f]{24})', tool_content)
        m_name = re.search(r'\[([^\]\n]{1,80})\]', tool_content)
        if m_pid and m_name:
            set_project_name(m_pid.group(1), m_name.group(1))
            learned += 1

    # 3) status_summary renders as plain text with a header like
    #       📄 Project: <title>  [<24-hex-id>]
    #    Anchor on the 24-hex ID (enclosed in [] or ()) and pull the
    #    project title from the ``(?:project|name):`` label that
    #    immediately precedes it on the same line. Title may itself
    #    contain ``[...]`` substrings like ``[EMNLP Demo] Tofu`` so we
    #    can't exclude ``[`` from the value class — we tether on the ID.
    if isinstance(tool_content, str):
        m = re.search(
            r'(?im)(?:project|name)\s*:\s*(.{1,120}?)\s*[\[(]([0-9a-f]{24})[\])]',
            tool_content,
        )
        if m:
            title = m.group(1).strip()
            # Guard against empty titles after stripping
            if title:
                set_project_name(m.group(2), title)
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
                if isinstance(t, str) and t.strip():
                    title = t
                else:
                    meta = parsed.get('meta')
                    if isinstance(meta, dict):
                        t2 = meta.get('contentTitle') or meta.get('title')
                        if isinstance(t2, str) and t2.strip():
                            title = t2
            if title:
                before = get_doc_title(cid)
                set_doc_title(cid, title)
                if before != title and get_doc_title(cid) == title:
                    learned += 1

    # 5) Harvest full URLs embedded in the result text so the tool-call line
    #    can become a clickable link. Overleaf write/create tools and the
    #    create_document/read_doc responses embed a full ``…/project/<id>`` or
    #    ``…/collabpage/<id>`` URL — record it per id AND learn the deployment
    #    base (lets self-hosted Overleaf instances link correctly).
    if isinstance(tool_content, str) and tool_content:
        global _overleaf_base, _xuecheng_base
        for m in _OVERLEAF_URL_RE.finditer(tool_content):
            base, pid = m.group(1), m.group(2).lower()
            with _lock:
                if not _overleaf_base:
                    _overleaf_base = base
                if len(_proj_url_cache) >= _MAX_ENTRIES and pid not in _proj_url_cache:
                    _proj_url_cache.pop(next(iter(_proj_url_cache)), None)
                _proj_url_cache[pid] = m.group(0)
        for m in _KM_FULL_URL_RE.finditer(tool_content):
            base, cid = m.group(1), m.group(2)
            with _lock:
                if not _xuecheng_base:
                    _xuecheng_base = base
                if len(_doc_url_cache) >= _MAX_ENTRIES and cid not in _doc_url_cache:
                    _doc_url_cache.pop(next(iter(_doc_url_cache)), None)
                _doc_url_cache[cid] = m.group(0)

    if learned:
        logger.debug('[McpNames] %s learned %d names', fn_name, learned)
    return learned


_KM_DOC_URL_RE = re.compile(r'/(?:collabpage|page)/(\d{6,15})')


def _extract_doc_id(val) -> str:
    """Normalise a Xuecheng ``doc`` arg to a bare numeric contentId."""
    if val is None:
        return ''
    s = str(val).strip()
    if not s:
        return ''
    m = _KM_DOC_URL_RE.search(s)
    if m:
        return m.group(1)
    if _DOC_ID_RE.fullmatch(s):
        return s
    return ''
