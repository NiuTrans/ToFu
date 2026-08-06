"""Per-tool display renderers.

Each ``_tool_display_*`` handler returns ``(display_str, extra_fields_dict)``
for a given tool call.  Shared helpers ``_short_url`` (URL shortening) and
``_persisted_read_labels`` (friendly labels for read_files against spilled
tool results) live here too.
"""

import os
from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.tool_display._mcp import compose_mcp_display, _mcp_links


def _tool_display_web_search(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for web_search tool calls.

    For batch mode (``queries`` array), every candidate search term is
    rendered IN FULL with a newline between terms — the frontend renders
    line breaks so long queries wrap naturally instead of being elided.
    The structured list is also exposed via ``_batchQueries`` for any UI
    that wants to render it differently.
    """
    queries = fn_args.get('queries')
    if queries and isinstance(queries, list):
        full_list = []
        for s in queries:
            if isinstance(s, dict):
                q = s.get('query', '?') or '?'
            elif isinstance(s, str):
                q = s.strip() or '?'
            else:
                q = '?'
            full_list.append(q)
        n = len(full_list)
        # A single-element batch (common after the bare-string→array repair,
        # or when the model wraps one query) reads as a plain single search —
        # ``1 searches:`` is grammatically wrong and wastes a line.
        if n == 1:
            return full_list[0], {'toolName': 'web_search',
                                  '_batchQueries': full_list}
        # One query per line so the frontend wraps long terms instead of
        # squashing them onto one elided line. Indent each line with "• "
        # so the count header reads naturally.
        lines = '\n'.join(f'• {q}' for q in full_list)
        display = f'{n} searches:\n{lines}'
        return display, {
            'toolName': 'web_search',
            '_display_query': display,
            '_batchQueries': full_list,
        }
    query = fn_args.get('query', '')
    return query, {'toolName': 'web_search'}


def _short_url(url, max_len=60):
    """Return a human-friendly short URL: hostname + path (truncated).

    For URLs like ``https://github.com/org/repo``, the hostname alone
    (``github.com``) loses important context.  This helper keeps the
    path prefix so users can distinguish different pages on the same host.

    Args:
        url: Full URL string.
        max_len: Maximum character length for the result.

    Returns:
        Shortened URL string, e.g. ``github.com/org/repo``.
    """
    try:
        p = urlparse(url)
    except ValueError as e:
        logger.debug('[ToolDisplay] urlparse failed for %r: %s', url[:80], e)
        return url[:max_len]
    host = p.netloc or ''
    path = (p.path or '').rstrip('/')
    # Drop trivial index paths
    if path in ('', '/'):
        return host
    short = host + path
    if len(short) <= max_len:
        return short
    # Truncate path, keeping the beginning which is most informative
    avail = max_len - len(host) - 1  # 1 for the ellipsis '…'
    if avail > 5:
        return host + path[:avail] + '…'
    # Fallback: just hostname
    return host


def _tool_display_fetch_url(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for fetch_url tool calls.

    For batch mode (``urls`` array), every URL is rendered IN FULL on its
    own line so users can see exactly what the model is fetching — long
    URLs wrap rather than being elided.  The full list is also exposed via
    ``_batchUrls`` for structured rendering.
    """
    urls = fn_args.get('urls')
    if urls and isinstance(urls, list):
        full_list = []
        for s in urls:
            if isinstance(s, dict):
                u = s.get('url', '?') or '?'
            elif isinstance(s, str):
                u = s.strip() or '?'
            else:
                u = '?'
            full_list.append(u)
        n = len(full_list)
        # Single-element batch → plain single fetch (see web_search above).
        if n == 1:
            target_url = full_list[0]
            is_pdf_hint = target_url.lower().rstrip('/').endswith('.pdf')
            short = _short_url(target_url)
            display_query = f'{"PDF " if is_pdf_hint else ""}{short}'
            return target_url, {'toolName': 'fetch_url',
                                '_display_query': display_query,
                                '_batchUrls': full_list}
        lines = '\n'.join(f'• {u}' for u in full_list)
        display = f'{n} URLs:\n{lines}'
        return display, {
            'toolName': 'fetch_url',
            '_display_query': display,
            '_batchUrls': full_list,
        }
    target_url = fn_args.get('url', '')
    is_pdf_hint = target_url.lower().rstrip('/').endswith('.pdf')
    short = _short_url(target_url)
    display_query = f'{"PDF " if is_pdf_hint else ""}{short}'
    return target_url, {'toolName': 'fetch_url', '_display_query': display_query}


def _tool_display_code_exec(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for standalone code execution tool calls."""
    from lib.project_mod import project_tool_display
    display = project_tool_display(fn_name, fn_args)
    return display, {'toolName': 'code_exec'}


def _persisted_read_labels(fn_args):
    """When a read_files call targets spilled-to-disk tool results, return a
    friendly display string (e.g. ``Read web search result — "…"``) instead
    of the opaque persisted filename. Returns '' when no path is a known
    persisted result so the caller keeps the default rendering.
    """
    if not isinstance(fn_args, dict):
        return ''
    reads = fn_args.get('reads')
    if isinstance(reads, str):
        import json
        try:
            reads = json.loads(reads)
        except (ValueError, TypeError) as e:
            logger.debug('[ToolDisplay] read_files reads=str not JSON: %s', e)
            reads = None
    specs = []
    if isinstance(reads, list):
        for r in reads:
            if isinstance(r, dict) and r.get('path'):
                specs.append(r)
            elif isinstance(r, str) and r:
                specs.append({'path': r})
    elif fn_args.get('path'):
        specs.append(fn_args)
    if not specs:
        return ''

    from lib.tasks_pkg.persist_registry import (
        describe_filename, friendly_label, lookup,
    )
    labels = []
    persisted_count = 0
    for spec in specs:
        p = spec.get('path') or ''
        hit = lookup(p) or describe_filename(p)
        if hit is not None:
            persisted_count += 1
            labels.append(friendly_label(*hit))
            continue
        # Non-persisted (ordinary project file) in a mixed batch — keep the
        # normal basename + line-range rendering so its path/range survives.
        base = p.rsplit('/', 1)[-1] or p
        sl, el = spec.get('start_line'), spec.get('end_line')
        if sl is not None and el is not None:
            labels.append(f'{base} L{sl}-{el}')
        elif sl is not None:
            labels.append(f'{base} L{sl}+')
        else:
            labels.append(base)

    # Pure project read (nothing persisted) — defer to default rendering.
    if persisted_count == 0:
        return ''

    n = len(labels)
    # When every path is a persisted spill, the friendly "saved results"
    # header is accurate; a mixed batch also pulls in project files, so use
    # the neutral "files" header there.
    header = (f'Read {n} saved result{"s" if n != 1 else ""}'
              if persisted_count == n else f'Read {n} file{"s" if n != 1 else ""}')
    # One entry per line so the frontend (which turns \n → <br>) renders
    # every label in full instead of eliding to the first few.
    body = '\n'.join(f'• {lbl}' for lbl in labels)
    return f'{header}:\n{body}'


def _tool_display_project(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for project tool calls."""
    if fn_name == 'read_files':
        friendly = _persisted_read_labels(fn_args)
        if friendly:
            return friendly, {'toolName': fn_name}
    from lib.project_mod import project_tool_display
    display = project_tool_display(fn_name, fn_args)
    return display, {'toolName': fn_name}


def _tool_display_browser(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for browser tool calls (basic + advanced)."""
    from lib.browser import browser_tool_display
    display = browser_tool_display(fn_name, fn_args)
    return display, {'toolName': fn_name}


def _tool_display_memory(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for memory management tool calls.

    No emoji prefix — the frontend renders a per-tool SVG icon (see
    ``_webToolSvg`` in ``static/js/ui/tool_rounds.js``).
    """
    if fn_name == 'create_memory':
        display = f"Saving memory: {fn_args.get('name', '?')}"
    elif fn_name == 'update_memory':
        display = f"Updating memory: {fn_args.get('memory_id', '?')}"
    elif fn_name == 'delete_memory':
        display = f"Deleting memory: {fn_args.get('memory_id', '?')}"
    elif fn_name == 'merge_memories':
        ids = fn_args.get('memory_ids', [])
        display = f"Merging {len(ids)} memories → {fn_args.get('name', '?')}"
    elif fn_name == 'search_memories':
        query = fn_args.get('query', '')
        display = f"Searching memories: {query[:80]}" if query else "Searching memories"
    else:
        display = fn_name
    return display, {'toolName': fn_name}


def _tool_display_skills(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for skill activation calls.

    No emoji prefix — the frontend renders a per-tool SVG icon (§3.4).
    """
    skill = fn_args.get('skill', '?') if isinstance(fn_args, dict) else '?'
    return f'Activating skill: {skill}', {'toolName': fn_name}


def _tool_display_conv_ref(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for conversation reference tool calls.

    No emoji prefix — the frontend renders a per-tool SVG icon (see
    ``_webToolSvg`` in ``static/js/ui/tool_rounds.js``).
    """
    kw = fn_args.get('keyword', 'all') if fn_name == 'list_conversations' else fn_args.get('conversation_id', '?')[:8]
    display = f"{fn_name}: {kw}"
    return display, {'toolName': fn_name}


def _tool_display_brain(fn_name, fn_args, tc_id, tc_args_str):
    """Build a friendly collapsed-header label for project-brain tools.

    Without a dedicated handler these fall through to ``_tool_display_generic``,
    which (a) logs a spurious WARNING on EVERY call ("Unregistered tool … may
    need a dedicated display handler") and (b) shows the raw ``project_board_read``
    fn-name as the transcript preview. This returns a short human-readable
    summary keyed on the tool + its salient arg (the frontend still renders the
    SVG icon + the structured card body; this is only the collapsed preview
    line the user reads before expanding). No emoji prefix (§3.4).
    """
    args = fn_args if isinstance(fn_args, dict) else {}
    if fn_name == 'project_board_read':
        display = 'Read the project board'
    elif fn_name == 'project_charter_read':
        display = 'Read the project charter'
    elif fn_name == 'project_charter_propose':
        title = (args.get('title') or '').strip()
        display = f'Propose to charter: {title}' if title else 'Propose a charter amendment'
    elif fn_name == 'project_peer_status':
        cid = (args.get('conv_id') or '').strip()
        display = f'Peer status: conv {cid[:8]}' if cid else 'Live peer status'
    elif fn_name == 'project_feed_read':
        display = 'Read the project activity feed'
    elif fn_name == 'project_message':
        to = (args.get('to_conv_id') or '').strip()
        display = f'Message → conv {to[:8]}' if to else 'Send a peer message'
    elif fn_name == 'project_intervene':
        to = (args.get('to_conv_id') or '').strip()
        hard = bool(args.get('hard_abort'))
        kind = 'Hard intervene' if hard else 'Advisory intervene'
        display = f'{kind} → conv {to[:8]}' if to else kind
    elif fn_name.startswith('project_board_'):
        verb = fn_name.replace('project_board_', '', 1)
        tid = (args.get('task_id') or '').strip()
        display = f'Board {verb}: {tid}' if tid else f'Board {verb}'
    else:
        display = fn_name
    return display, {'toolName': fn_name}


def _tool_display_todo(fn_name, fn_args, tc_id, tc_args_str):
    """Build a friendly progress label for the ``todo_write`` checklist tool.

    Without a dedicated handler this falls through to ``_tool_display_generic``,
    which logs a spurious WARNING on EVERY call. ``todo_write`` fires several
    times per multi-step task, so that WARNING dominated logs/error.log. Render
    a compact "Planning: N steps (d done)" label instead.
    """
    todos = fn_args.get('todos') if isinstance(fn_args, dict) else None
    if not isinstance(todos, list) or not todos:
        return 'Updating checklist', {'toolName': fn_name}
    total = len(todos)
    done = sum(1 for t in todos if isinstance(t, dict) and t.get('status') == 'completed')
    display = f'Checklist: {total} step{"s" if total != 1 else ""}'
    if done:
        display += f' ({done} done)'
    return display, {'toolName': fn_name}


def _tool_display_scheduler(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for scheduler tool calls (frontend renders SVG icon)."""
    return fn_name, {'toolName': fn_name}


def _tool_display_desktop(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for desktop tool calls (frontend renders SVG icon)."""
    return fn_name, {'toolName': fn_name}


def _tool_display_swarm(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for swarm tool calls.

    Only ``spawn_agents`` is rendered as the full swarm panel
    (``_swarm: True`` upgrades the round into a dashboard with agent
    cards).  ``await_agents`` / ``get_agent_result`` are async-flow
    bookkeeping calls — they get a compact mini-card via the regular
    tool-round renderer (no ``_swarm`` flag), so the user sees them
    inline alongside other tools rather than as a second swarm panel.
    """
    if fn_name == 'spawn_agents':
        n_agents = len(fn_args.get('agents', [])) if isinstance(fn_args, dict) else 0
        # A spawn_agents call with NO agents launches nothing (the backend
        # returns ``{"error": "no agents specified"}``). It must NOT be
        # stamped ``_swarm: True`` — an empty swarm panel becomes an event
        # magnet: the frontend's "first _swarm round" lookup grafts a LATER
        # real swarm's agent events onto this orphan round, splitting one
        # swarm across two panels (the "ghost panel" / "ticked but waiting"
        # bug). Render it as an ordinary tool round instead.
        if not n_agents:
            return "Spawning agents…", {'toolName': 'spawn_agents'}
        display = f"Spawning {n_agents} agent{'s' if n_agents != 1 else ''}…"
        return display, {'toolName': 'spawn_agents', '_swarm': True}

    if fn_name == 'await_agents':
        ids = fn_args.get('ids') if isinstance(fn_args, dict) else None
        mode = (fn_args.get('mode', 'any') if isinstance(fn_args, dict) else 'any')
        if ids and isinstance(ids, list) and len(ids) > 0:
            label = f'Awaiting {len(ids)} agent{"s" if len(ids) != 1 else ""} ({mode})'
        else:
            label = f'Awaiting all running agents ({mode})'
        return label, {'toolName': 'await_agents'}

    if fn_name == 'get_agent_result':
        agent_ids = fn_args.get('agent_ids') if isinstance(fn_args, dict) else None
        if isinstance(agent_ids, list) and len(agent_ids) > 1:
            label = f'Fetching {len(agent_ids)} agent results'
            return label, {'toolName': 'get_agent_result'}
        if isinstance(agent_ids, list) and len(agent_ids) == 1:
            single = str(agent_ids[0] or '')
        else:
            single = (fn_args.get('agent_id', '') if isinstance(fn_args, dict) else '')
        label = f'Fetching result for {single[:12]}' if single else 'Fetching agent result'
        return label, {'toolName': 'get_agent_result'}

    # Artifact tools fall through to the generic renderer in the dispatch
    # table — they don't get _swarm: True either, so they appear as regular
    # tool rounds rather than separate swarm panels.
    display = fn_name.replace('_', ' ').title()
    return display, {'toolName': fn_name}


def _tool_display_compact(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for context_compact tool calls (frontend renders SVG icon)."""
    return 'Compacting context…', {'toolName': fn_name}


def _tool_display_image_gen(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for image generation tool calls.

    ★ No hard 80-char cap on the prompt — the frontend word-wraps the
    title line and users explicitly requested "do not truncate". A very
    generous soft cap (2000 chars) still protects against a pathological
    prompt bloating every SSE event. The full prompt is also exposed via
    ``imagePrompt`` so the frontend footer can render it untruncated.
    """
    _FULL_LIMIT = 2000
    prompt = fn_args.get('prompt', '…') or '…'
    if len(prompt) > _FULL_LIMIT:
        prompt = prompt[:_FULL_LIMIT - 1] + '…'
    return f'Generating: {prompt}', {
        'toolName': 'generate_image',
        'imagePrompt': prompt,
    }




def _tool_display_inspect_image(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for inspect_image tool calls.

    Surfaces the target file plus the requested transform (crop / zoom /
    rotate / grid) so the tool-call line reads e.g. ``diagram.png — crop, 2×``.
    """
    path = fn_args.get('path', '?') or '?'
    # The model may pass a chat-upload reference rather than a filesystem path:
    # a bare '/api/images/<f>', a proxy-prefixed '/proxy/<port>/api/images/<f>',
    # or even the whole '[image ref: /api/images/<f> — …]' hint string verbatim.
    # os.path.basename on those yields junk like 'uploaded]'. Recognise the
    # /api/images/ marker and show the real uploaded filename instead.
    try:
        from lib.attachments import canonical_image_ref
        import re as _re
        _m = _re.search(r'/api/images/[^\s\]\'"]+', path)
        _canon = canonical_image_ref(_m.group(0)) if _m else ''
    except Exception as e:
        logger.debug('[ToolDisplay] image ref canonicalization failed for %.80s: %s', path, e)
        _canon = ''
    if _canon:
        base = os.path.basename(_canon) or _canon
    else:
        base = os.path.basename(path) or path
    ops = []
    if fn_args.get('crop'):
        ops.append('crop')
    z = fn_args.get('zoom')
    if z:
        try:
            ops.append(f'{float(z):g}×')
        except (TypeError, ValueError):
            ops.append('zoom')
    rot = fn_args.get('rotate')
    if rot:
        ops.append(f'{rot}°')
    if fn_args.get('grid'):
        ops.append('grid')
    suffix = f' — {", ".join(ops)}' if ops else ''
    return f'{base}{suffix}', {'toolName': 'inspect_image'}


def _mv_base(path):
    """Basename of a tool path arg, '' when absent (never '?')."""
    return os.path.basename((path or '').rstrip('/')) if path else ''


def _tool_display_motion_video(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for the ``motion_video_*`` pipeline tools.

    Without a dedicated handler these fall through to the generic catch-all,
    which shows the raw fn_name (``motion_video_render``) and logs a spurious
    WARNING on EVERY call — the 2026-08-06 owner screenshot: a card carrying
    only a name and a badge, with no idea what ran. Each label names the
    salient file(s) so a 60-round storyboard→render session stays scannable.
    No emoji prefix — the frontend renders a per-tool SVG icon (§3.4).
    """
    args = fn_args if isinstance(fn_args, dict) else {}
    if fn_name == 'motion_video_env_check':
        install = args.get('install', True)
        display = ('Check the render environment' if install
                   else 'Check the render environment (no install)')
    elif fn_name == 'motion_video_storyboard_check':
        scenes = _mv_base(args.get('scenes_path', ''))
        srt = _mv_base(args.get('srt_path', ''))
        display = 'Validate storyboard'
        if scenes:
            display += f': {scenes}'
        if srt:
            display += f' vs {srt}'
    elif fn_name == 'motion_video_check':
        scene = _mv_base(args.get('project_dir', ''))
        display = f'Static gates: {scene}' if scene else 'Run static gates'
    elif fn_name == 'motion_video_render':
        scene = _mv_base(args.get('project_dir', ''))
        out = _mv_base(args.get('output', ''))
        quality = (args.get('quality') or 'standard').strip()
        display = 'Render'
        if scene:
            display += f' {scene}'
        if out:
            display += f' → {out}'
        display += f' ({quality})'
    elif fn_name == 'motion_video_probe':
        target = _mv_base(args.get('path', ''))
        display = f'Probe {target}' if target else 'Probe media file'
    elif fn_name == 'motion_video_concat':
        inputs = args.get('inputs') or []
        n = len(inputs) if isinstance(inputs, list) else 0
        out = _mv_base(args.get('output', ''))
        display = f'Concat {n} scene{"s" if n != 1 else ""}'
        if out:
            display += f' → {out}'
    elif fn_name == 'motion_video_narrate':
        out = _mv_base(args.get('out_dir', ''))
        alignment = (args.get('alignment') or 'loose').strip()
        display = 'Narrate scenes'
        if out:
            display += f' → {out}/'
        if alignment != 'loose':
            display += f' ({alignment})'
    elif fn_name == 'motion_video_mux':
        out = _mv_base(args.get('output', ''))
        display = f'Mux narration → {out}' if out else 'Mux narration + video'
    else:
        display = fn_name
    return display, {'toolName': fn_name}


def _tool_display_produce(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for the high-level ``produce_*`` tools.

    The salient arg is the topic/direction the user asked about — that is
    what makes the card recognizable in the transcript.
    """
    args = fn_args if isinstance(fn_args, dict) else {}
    if fn_name == 'produce_video':
        topic = (args.get('topic') or '').strip()
        display = f'Produce video: {topic[:80]}' if topic else 'Produce video'
    elif fn_name == 'produce_report':
        topic = (args.get('topic') or '').strip()
        depth = (args.get('depth') or 'standard').strip()
        display = f'Research report: {topic[:80]}' if topic else 'Research report'
        if depth != 'standard':
            display += f' ({depth})'
    elif fn_name == 'produce_slides':
        topic = (args.get('topic') or '').strip()
        style = (args.get('style') or '').strip()
        display = f'Produce slides: {topic[:80]}' if topic else 'Produce slides'
        if style:
            display += f' ({style[:30]})'
    elif fn_name == 'produce_research':
        direction = (args.get('direction') or '').strip()
        display = (f'Research ideas: {direction[:80]}' if direction
                   else 'Research ideas')
    else:
        display = fn_name
    return display, {'toolName': fn_name}


def _tool_display_search_settings(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for ``update_search_settings`` calls.

    No-arg call is a pure READ of the current values; a call with kwargs is
    a write — the label names the knobs being tuned.
    """
    args = fn_args if isinstance(fn_args, dict) else {}
    keys = sorted(k for k, v in args.items() if v is not None)
    if not keys:
        return 'Read search/fetch settings', {'toolName': fn_name}
    shown = ', '.join(keys[:4])
    if len(keys) > 4:
        shown += f' +{len(keys) - 4} more'
    return f'Tune search/fetch settings: {shown}', {'toolName': fn_name}


def _tool_display_human_guidance(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for ask_human tool calls.

    ★ No hard 80-char cap on the question text — the frontend word-wraps
    and users explicitly requested "incomplete displays are not allowed".
    A very generous soft cap (2000 chars) still protects against a
    pathological 100 KB prompt bloating every SSE event.
    """
    _FULL_LIMIT = 2000
    question = fn_args.get('question', '…') or '…'
    if len(question) > _FULL_LIMIT:
        question = question[:_FULL_LIMIT - 1] + '…'
    return question, {'toolName': 'ask_human'}


def _tool_display_mcp(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for MCP bridge tool calls (mcp__server__tool).

    Surfaces the most informative arg (file_path, name, section_title, short
    project_id, owner/repo, …) after the tool name so users can tell at a
    glance which file / project / resource the call is operating on —
    instead of seeing a uniform ``overleaf/create_file`` for every write.
    No emoji prefix — the frontend renders the plug SVG icon (§3.4).

    When the resource resolves to a known URL (e.g. an Overleaf project), a
    ``_mcpLinks`` map (label → href) is attached so the frontend can render
    that segment as a clickable link instead of an unreadable id jumble.
    """
    extra = {'toolName': fn_name}
    display, multiline = compose_mcp_display(fn_name, fn_args)
    if multiline:
        # Batch-file form (one path per line) — expose the multiline text via
        # _display_query so it survives verbatim into the SSE tool_start event.
        extra['_display_query'] = display
        return display, extra
    links = _mcp_links(fn_args)
    if links:
        extra['_mcpLinks'] = links
    return display, extra


def _tool_display_generic(fn_name, fn_args, tc_id, tc_args_str):
    """Catch-all display info for unknown/future tools."""
    # Check if this is an MCP tool before falling through to generic
    from lib.mcp.types import MCP_TOOL_PREFIX
    if fn_name.startswith(MCP_TOOL_PREFIX):
        return _tool_display_mcp(fn_name, fn_args, tc_id, tc_args_str)
    logger.warning('[Orchestrator] Unregistered tool %s — using generic round_entry. This tool may need a dedicated display handler.', fn_name)
    return fn_name, {'toolName': fn_name}
