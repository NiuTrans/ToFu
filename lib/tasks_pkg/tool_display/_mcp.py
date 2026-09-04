"""MCP-specific tool-display helpers.

Resource/container arg rendering, project/doc/job-id resolution and
shortening, clickable-link resolution, batch-path extraction, and the
single-source-of-truth ``compose_mcp_display`` used by both the live
tool-round line and the persisted results-row title.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# Keys from fn_args that identify the *resource* the call is operating on
# (the inner "what" — a file, a section, a new name, an issue, …).
# Ordered by priority; the first match wins as the resource label.
_MCP_RESOURCE_KEYS = (
    'file_path',      # overleaf/github — which file
    'path',           # github/hope_dfs_ls — which file/dir
    'file_name',      # hope_mlp_log_get — which log file (stdout/stderr/…)
    'name',           # create_project / create_branch — new-resource name
    'title',          # issue/PR title
    'issue_number',   # github — which issue
    'pull_number',    # github — which PR
    'query',          # search tools
    'q',              # github search_* q=
    'keyword',        # xuecheng_search — search keyword
    'url',            # fetch_url-like tools
    'branch',         # github — branch name
    'doc',            # xuecheng_* — doc id or full collabpage URL
    'app_id',         # hope_fetch_source_code / hope_get_status (appid variant)
    'appid',          # hope_get_status / hope_change_priority
    'experiment_id_or_name',  # llm_experiment_get — canvas experiment
    'job_id',         # hope_mlp_job_* — psx… job id
    'job_ids',        # hope_mlp_job_info — comma-separated
    'ids',            # llm_experiment_update_params — comma-separated
    'run_id',         # hope_mlp_run_* — MLP run id
    'runid',          # hope_get_status / hope_stop_job (legacy spelling)
    'session_id',     # hope_stop_session
    'pod_name',       # hope_mlp_log_files / hope_mlp_log_get
    'queue_name',     # hope_mlp_queue_*
    'queue',          # hope_list_resource / hope_mlp_run_jobs
    'key',            # hope_get_lion_config
    'template_id',    # xuecheng_get_template_markdown / create_doc
    'space_id',       # xuecheng_get_space_root_docs
    'memory_id',      # memory tools (defensive — also in _tool_display_memory)
)

# Keys that identify the *container* the call is scoped to (the outer
# "where" — a project, a repo, an owner/repo pair). Rendered after
# the resource label with a ``@`` separator so the title line reads
# e.g. ``acl.sty @ 69f21…cca7`` or ``create_issue #123 @ torvalds/linux``.
_MCP_CONTAINER_KEYS = (
    'project_id',     # overleaf — 24-hex ID (shortened)
    'repo',           # github — paired with owner below
    'cluster',        # hope_mlp_log_get — paired with namespace below
    'namespace',      # hope_mlp_log_files (fallback when cluster missing)
)

# Per-segment length cap for resource + container in the title line.
_MCP_SEG_MAX = 40

# Keys that state the *operation* performed on the resource rather than the
# resource itself (read direction, server-side filter, target field). A
# parallel batch routinely calls the SAME tool on the SAME resource with
# only these args differing (e.g. get_log_file with regex A vs regex B);
# without them every sibling row renders an identical title and looks like a
# duplicate execution. Rendered as compact chips after the resource label.
# Curated — never blanket-render every scalar arg — so tokens, payloads and
# other bulky/sensitive values can never leak into a title line.
_MCP_MODIFIER_KEYS = ('method', 'regex', 'field', 'node_id', 'dry_run')
_MCP_MODIFIER_MAX = 2
_MCP_MODIFIER_SEG_MAX = 28


# ── Overleaf project-name cache ──────────────────────────────────────
# Maps 24-hex project_id → human-readable project name. Populated
# opportunistically by inspecting the results of ``list_projects`` /
# ``status_summary`` / ``create_project`` calls (see lib.mcp.project_names).
# Consulted at title-line render time so users see
# ``… @ [EMNLP Demo] Tofu`` instead of ``… @ 69f21…cca7``.

def _resolve_project_name(pid: str) -> str:
    """Look up a cached human-readable Overleaf project name by its ID.

    Returns '' when no cached name is available — callers should fall back
    to the short ID form. The cache is filled by
    :mod:`lib.mcp.project_names` as MCP tool calls complete.
    """
    try:
        from lib.mcp.project_names import get_project_name
        return get_project_name(pid) or ''
    except ImportError as e:
        logger.debug('[ToolDisplay] mcp.project_names unavailable: %s', e)
        return ''


def _short_project_id(pid: str) -> str:
    """Format a 24-hex Overleaf project_id for display.

    Prefers the cached human-readable name (``[EMNLP Demo] Tofu``) when
    available; falls back to the compact ``prefix…suffix`` form so users
    can still tell two unknown projects apart.
    """
    s = str(pid).strip()
    if len(s) == 24 and all(c in '0123456789abcdef' for c in s.lower()):
        name = _resolve_project_name(s)
        if name:
            # Cap the project name to keep the title line readable.
            if len(name) > _MCP_SEG_MAX:
                name = name[:_MCP_SEG_MAX - 1] + '…'
            return name
        return f'{s[:5]}…{s[-4:]}'
    return s


_KM_DOC_RE = __import__('re').compile(r'/(?:collabpage|page)/(\d+)')


def _resolve_doc_title(content_id: str) -> str:
    """Look up a cached Xuecheng doc title; return '' when absent."""
    try:
        from lib.mcp.project_names import get_doc_title
        return get_doc_title(content_id) or ''
    except ImportError as e:
        logger.debug('[ToolDisplay] mcp.project_names get_doc_title unavailable: %s', e)
        return ''


def _short_doc_id(val) -> str:
    """Format a Xuecheng ``doc`` arg for display.

    Prefers the cached human-readable title (harvested from prior
    ``xuecheng_read_doc`` / ``xuecheng_search`` / ``xuecheng_create_document``
    calls) over the bare content id, falling back to the numeric id when
    no title is known. Accepts either a bare id or a full collabpage URL.
    """
    if val is None:
        return ''
    s = str(val).strip()
    if not s:
        return ''
    cid = ''
    m = _KM_DOC_RE.search(s)
    if m:
        cid = m.group(1)
    elif s.isdigit():
        cid = s
    if cid:
        title = _resolve_doc_title(cid)
        if title:
            if len(title) > _MCP_SEG_MAX:
                title = title[:_MCP_SEG_MAX - 1] + '…'
            return title
        return cid
    return s


def _short_job_id(val: str) -> str:
    """Format a long Hope MLP job id for display.

    Hope's ``psx``-prefixed instance ids are ~32 chars and dominate the
    title line. Keep prefix + suffix so users can still distinguish two
    concurrent jobs.
    """
    s = str(val).strip()
    if len(s) > 24 and (s.startswith('psx') or '-' in s):
        return f'{s[:8]}…{s[-6:]}'
    return s


def _render_mcp_arg(key: str, val) -> str:
    """Render a single MCP fn_args value as a short display string.

    Applies per-key formatting: 24-hex project_ids are shortened, issue/PR
    numbers get a leading ``#``, ``doc`` URLs are reduced to their numeric
    id, long Hope job ids are shortened, strings are stripped + truncated.
    """
    if val is None:
        return ''
    s = str(val).strip()
    if not s:
        return ''
    if key == 'project_id':
        s = _short_project_id(s)
    elif key == 'doc':
        s = _short_doc_id(s)
    elif key in ('job_id', 'app_id', 'appid', 'pod_name'):
        s = _short_job_id(s)
    elif key in ('job_ids', 'ids'):
        # comma-separated list: shorten each, cap to first 2
        parts = [_short_job_id(p) for p in s.split(',') if p.strip()]
        if len(parts) > 2:
            s = f'{parts[0]}, {parts[1]} +{len(parts) - 2} more'
        else:
            s = ', '.join(parts) or s
    if key in ('issue_number', 'pull_number'):
        s = f'#{s}'
    if len(s) > _MCP_SEG_MAX:
        s = s[:_MCP_SEG_MAX - 1] + '…'
    return s


def _render_modifier_chip(key: str, val) -> str:
    """Render one modifier arg as a short chip, or '' when not informative."""
    if val is None or val is False:
        return ''
    if key == 'dry_run':
        return 'dry-run'
    s = str(val).strip()
    if not s:
        return ''
    if len(s) > _MCP_MODIFIER_SEG_MAX:
        s = s[:_MCP_MODIFIER_SEG_MAX - 1] + '…'
    if key == 'regex':
        return f'/{s}/'
    if key in ('field', 'node_id'):
        return f'{key}={s}'
    return s


def _mcp_modifier_chips(fn_args, resource: str) -> list:
    """Collect up to ``_MCP_MODIFIER_MAX`` operation chips for a call.

    Chips that merely restate the resource label are dropped; duplicates
    collapse. The cap bounds title-line growth — the resource + container
    remain the primary identity, chips only disambiguate."""
    if not isinstance(fn_args, dict):
        return []
    chips = []
    for key in _MCP_MODIFIER_KEYS:
        if key not in fn_args:
            continue
        chip = _render_modifier_chip(key, fn_args[key])
        if chip and chip != resource and chip not in chips:
            chips.append(chip)
        if len(chips) >= _MCP_MODIFIER_MAX:
            break
    return chips


def _mcp_arg_suffix(fn_args):
    """Compose a title-line suffix that surfaces BOTH the resource being
    operated on AND the container it lives in, when both apply.

    Examples:
      ``acl.sty @ 69f21…cca7``          (overleaf/create_file)
      ``main.tex @ 69f21…cca7``         (overleaf/edit_file)
      ``My Paper``                      (overleaf/create_project)
      ``@ 69f21…cca7``                  (overleaf/list_files — container only)
      ``torvalds/linux#123``            (github issue-specific call)
      ``issue title @ torvalds/linux``  (github/create_issue)

    Falls back to empty string when no informative arg is present.
    """
    if not isinstance(fn_args, dict):
        return ''

    # ── Container label ────────────────────────────────────────────────
    container = ''
    # github owner+repo pair takes priority over bare ``repo``
    if 'owner' in fn_args and 'repo' in fn_args:
        container = f"{fn_args.get('owner', '?')}/{fn_args.get('repo', '?')}"
        if len(container) > _MCP_SEG_MAX:
            container = container[:_MCP_SEG_MAX - 1] + '…'
    elif fn_args.get('cluster') and fn_args.get('namespace'):
        # Hope log endpoints scope a pod by cluster + namespace
        container = f"{fn_args['cluster']}/{fn_args['namespace']}"
        if len(container) > _MCP_SEG_MAX:
            container = container[:_MCP_SEG_MAX - 1] + '…'
    else:
        for key in _MCP_CONTAINER_KEYS:
            if key in fn_args:
                rendered = _render_mcp_arg(key, fn_args[key])
                if rendered:
                    container = rendered
                    break

    # ── Resource label ─────────────────────────────────────────────────
    resource = ''
    for key in _MCP_RESOURCE_KEYS:
        if key in fn_args:
            rendered = _render_mcp_arg(key, fn_args[key])
            if rendered:
                resource = rendered
                break

    # Special enrichment: overleaf update_section/get_section_content pass
    # both ``file_path`` + ``section_title`` — show both as ``file › section``
    # so users see the exact path being touched.
    sect = fn_args.get('section_title')
    if resource and sect and isinstance(sect, str) and sect.strip():
        sect_short = sect.strip()
        if len(sect_short) > _MCP_SEG_MAX:
            sect_short = sect_short[:_MCP_SEG_MAX - 1] + '…'
        resource = f'{resource} › {sect_short}'

    # ── Operation chips (method/regex/field/…) ─────────────────────────
    chips = _mcp_modifier_chips(fn_args, resource)
    if chips:
        resource = ' · '.join([resource, *chips]) if resource else ' · '.join(chips)

    # ── Compose ────────────────────────────────────────────────────────
    # issue/PR number: if the resource label is a #number, and we have an
    # owner/repo container, render as ``owner/repo#N`` (no "@") — that's
    # the conventional form and more compact.
    if resource.startswith('#') and container and '/' in container:
        return f'{container}{resource}'

    if resource and container:
        return f'{resource} @ {container}'
    if resource:
        return resource
    if container:
        # Bare container (no per-call item) — used by list_files /
        # list_history / compile_project / status_summary. Prefix with
        # a tiny scope glyph so it reads naturally without the ``—``.
        return container
    return ''


def _mcp_links(fn_args):
    """Map the human-readable label of a linkable MCP arg → its clickable URL.

    Returns a dict keyed by the EXACT label string ``_mcp_arg_suffix`` renders
    for that arg (e.g. ``[EMNLP Demo] Tofu`` or the ``6a1e7…a668`` short id, or
    a Xuecheng doc title / numeric id), so the frontend can linkify that exact
    substring on the tool-call line. Only resources we can resolve a URL for
    are included; empty dict when none apply.

    Currently covers:
      * overleaf ``project_id`` → ``…/project/<id>`` (always — synthesized
        from the deployment base when no exact URL was harvested)
      * xuecheng ``doc``        → ``…/collabpage/<id>`` (always — synthesized
        from the learned-or-default km.internal.example.com base when no exact URL was
        harvested)
    """
    if not isinstance(fn_args, dict):
        return {}
    links = {}
    try:
        pid = fn_args.get('project_id')
        if pid:
            label = _render_mcp_arg('project_id', pid)
            from lib.mcp.project_names import get_project_url
            href = get_project_url(str(pid).strip())
            if label and href:
                links[label] = href
    except Exception as e:
        logger.debug('[ToolDisplay] overleaf link resolve failed: %s', e)
    try:
        doc = fn_args.get('doc')
        if doc:
            label = _render_mcp_arg('doc', doc)
            from lib.mcp.project_names import get_doc_url
            href = get_doc_url(_doc_cid(doc))
            if label and href:
                links[label] = href
    except Exception as e:
        logger.debug('[ToolDisplay] xuecheng link resolve failed: %s', e)
    return links


def _mcp_batch_paths(fn_args):
    """Extract the repo-relative paths from a batch-file MCP call.

    Covers the ``files=[{path, …}]`` / ``delete_paths=[…]`` shape used by
    ``github-batch/batch_commit``, the ``paths=[…]`` shape of
    ``github-batch/batch_delete``, and the official ``github/push_files``
    (``files=[{path, content}]``). These carry the paths INSIDE a list, so the
    flat ``_MCP_RESOURCE_KEYS`` scan misses them and the title line degrades to
    just ``branch @ owner/repo``.

    Returns a list of ``(path, is_delete)`` tuples in commit-then-delete order,
    or ``[]`` when the call has no batch-path shape.
    """
    if not isinstance(fn_args, dict):
        return []
    out = []
    files = fn_args.get('files')
    if isinstance(files, list):
        for f in files:
            if isinstance(f, dict) and f.get('path'):
                out.append((str(f['path']), False))
            elif isinstance(f, str) and f.strip():
                out.append((f.strip(), False))
    for key in ('delete_paths', 'paths'):
        val = fn_args.get(key)
        if isinstance(val, list):
            for p in val:
                if isinstance(p, str) and p.strip():
                    out.append((p.strip(), True))
    return out


def _doc_cid(val) -> str:
    """Normalise a Xuecheng ``doc`` arg to its bare numeric contentId, or ''."""
    if val is None:
        return ''
    s = str(val).strip()
    m = _KM_DOC_RE.search(s)
    if m:
        return m.group(1)
    return s if s.isdigit() else ''


def compose_mcp_display(fn_name, fn_args):
    """Compose the MCP tool-call display label — the SINGLE source of truth.

    Both the live tool-round line (``_tool_display_mcp``, at ``tool_start``)
    and the persisted results-row title (``handlers/mcp.py::_post_build``,
    after execution) call this so the two can NEVER diverge. Previously
    ``_post_build`` recomputed the label from ``_mcp_arg_suffix`` directly,
    which has no batch-path awareness — so a ``batch_commit`` line that showed
    every file at ``tool_start`` regressed to ``batch_commit — main @ owner/repo``
    the moment the commit finished.

    Returns ``(display, is_multiline)``. When ``is_multiline`` is True the
    display is the batch-file form (one path per line, ``\\n``-separated) that
    the frontend renders with ``\\n → <br>``; callers should surface it via
    ``_display_query`` so it reaches the SSE event verbatim.
    """
    from lib.mcp.types import parse_namespaced_name
    parsed = parse_namespaced_name(fn_name)
    if parsed:
        server_name, tool_name = parsed
        head = f'{server_name}/{tool_name}'
    else:
        head = fn_name

    # ── Batch-file commits (github-batch/batch_commit, batch_delete,
    #    github/push_files): the paths live inside a ``files``/``paths`` list
    #    that the flat arg scan can't see. Render every path on its own line
    #    so users see exactly what was touched, scoped to ``owner/repo`` —
    #    instead of a uniform ``main @ owner/repo``.
    batch_paths = _mcp_batch_paths(fn_args)
    if batch_paths:
        n = len(batch_paths)
        lines = '\n'.join(
            f'• {"− " if is_del else ""}{p}' for p, is_del in batch_paths
        )
        container = ''
        if isinstance(fn_args, dict) and fn_args.get('owner') and fn_args.get('repo'):
            container = f"{fn_args['owner']}/{fn_args['repo']}"
        scope = f' @ {container}' if container else ''
        return f'{head} — {n} file{"s" if n != 1 else ""}{scope}:\n{lines}', True

    suffix = _mcp_arg_suffix(fn_args)
    return (f'{head} — {suffix}' if suffix else head), False
