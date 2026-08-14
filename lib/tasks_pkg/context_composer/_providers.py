"""Canonical context providers for conversational model calls.

Providers only describe context. They never mutate the message list; physical
placement, deduplication, budgeting, and observability belong to the renderer.
"""

from __future__ import annotations

from concurrent.futures import Future

from lib.log import get_logger
from lib.tasks_pkg import system_prompt_cc
from lib.tasks_pkg.context_composer._models import ComposeRequest, ContextBlock

logger = get_logger(__name__)


def _block(block_id: str, source: str, content: str, *, authority: str,
           placement: str, stability: str, lifecycle: str,
           priority: int, max_tokens: int | None = None,
           reason: str = '', provenance: dict | None = None) -> ContextBlock:
    return ContextBlock(
        id=block_id, source=source, content=content or '',
        authority=authority, placement=placement, stability=stability,
        lifecycle=lifecycle, priority=priority, max_tokens=max_tokens,
        suppressed_reason=reason, provenance=provenance or {},
    )


def _last_user_text(messages: list[dict]) -> str:
    from lib.memory.prefetch._query import _extract_current_user_request
    return _extract_current_user_request(messages)


def _future_value(task: dict | None, key: str):
    future = (task or {}).get(key)
    if not isinstance(future, Future) or not future.done():
        return None
    try:
        return future.result(timeout=0)
    except Exception as exc:
        logger.debug('[ContextComposer] future %s failed: %s', key, exc)
        return None


def _project_rules(request: ComposeRequest) -> str:
    if not (request.project_enabled and request.project_path):
        return ''
    prefetched = _future_value(request.task, '_prefetch_project')
    if prefetched is not None:
        return prefetched or ''
    try:
        from lib.project_mod import get_context_for_prompt
        return get_context_for_prompt(
            request.project_path, conv_id=request.conv_id or None) or ''
    except Exception as exc:
        logger.warning('[ContextComposer] project rules unavailable: %s', exc)
        return ''


def _profile_blocks(request: ComposeRequest, query: str) -> tuple[str, str]:
    cfg = ((request.task or {}).get('config') or {})
    from lib.agent_core.personal_scope import resolve_preferences_enabled
    if not resolve_preferences_enabled(cfg,
                                       memory_enabled=request.memory_enabled):
        return '', ''
    try:
        from lib.memory.user_profile import (
            applied_profile_items,
            context_char_count,
            load_context,
            render_profile_tiers,
        )
        scope = ((request.task or {}).get('_profileScope') or '')
        context = load_context(scope)
        core, detail = render_profile_tiers(None, scope, query=query)
        if context['items'] and request.task is not None:
            applied = applied_profile_items(None, scope, query=query)
            request.task['_appliedPreferences'] = {
                'chars': context_char_count(context['items']),
                'items': applied['core'] + applied['detail'],
                'core': applied['core'], 'detail': applied['detail'],
            }
        return core or '', detail or ''
    except Exception as exc:
        logger.warning('[ContextComposer] preference profile unavailable: %s',
                       exc)
        return '', ''


def _skill_index(request: ComposeRequest) -> str:
    if not request.has_real_tools:
        return ''
    try:
        from lib.skills import build_skills_index
        paths = list(((request.task or {}).get('config') or {}).get(
            'projectPaths') or [])
        extras = [p for p in paths if p and p != request.project_path]
        return build_skills_index(
            request.project_path if request.project_enabled else None,
            extra_paths=extras) or ''
    except Exception as exc:
        logger.warning('[ContextComposer] skill index unavailable: %s', exc)
        return ''


def _memory_guidance(request: ComposeRequest) -> str:
    if not (request.has_real_tools and request.memory_enabled):
        return ''
    try:
        from lib.memory import (
            MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT,
            build_memory_context,
        )
        hint = build_memory_context(
            request.project_path if request.project_enabled else None) or ''
        return '\n\n'.join(x for x in (hint,
                                        MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT)
                            if x)
    except Exception as exc:
        logger.warning('[ContextComposer] memory guidance unavailable: %s', exc)
        return ''


def _swarm_guidance(request: ComposeRequest) -> str:
    if not request.swarm_enabled:
        return ''
    try:
        from lib.swarm.registry import format_role_catalogue
        roles = format_role_catalogue()
    except Exception as exc:
        logger.debug('[ContextComposer] swarm catalogue fallback: %s', exc)
        roles = 'general — independent bounded work'
    return f"""<parallel_execution>
Use spawn_agents for two or more genuinely independent investigations, large
search/read branches, or an independent review. Keep sequential work local.
Spawn all parallel agents in one call, give each a bounded objective and
expected output, never fabricate results, and use await_agents only when there
is no useful local work left. Available roles:

{roles}
</parallel_execution>"""


def _project_blocks(request: ComposeRequest, query: str) -> list[ContextBlock]:
    if not (request.project_enabled and request.project_path):
        return []
    out: list[ContextBlock] = []
    path = request.project_path
    try:
        from lib.conversations.project_charter import render_charter_injection_block
        charter = render_charter_injection_block(path)
    except Exception as exc:
        logger.debug('[ContextComposer] charter unavailable: %s', exc)
        charter = ''
    out.append(_block('project_charter', 'project.charter', charter,
                      authority='project', placement='tail', stability='turn',
                      lifecycle='task', priority=10, max_tokens=1200,
                      reason='' if charter else 'empty'))
    try:
        from lib.conversations.project_watch import render_goals_injection_block
        goals = render_goals_injection_block(path)
    except Exception as exc:
        logger.debug('[ContextComposer] goals unavailable: %s', exc)
        goals = ''
    out.append(_block('project_goals', 'project.goals', goals,
                      authority='project', placement='tail', stability='turn',
                      lifecycle='task', priority=20, max_tokens=800,
                      reason='' if goals else 'empty'))

    # The board is ambient only when an active claim/lease can change what
    # this agent should touch. Open/done backlog remains pull-based.
    board = ''
    active = 0
    try:
        from lib.conversations.project_board import (
            read_board,
            render_board_injection_block,
        )
        rows = (read_board(path) or {}).get('tasks') or []
        active = sum(1 for row in rows if row.get('status') == 'claimed')
        if active:
            board = render_board_injection_block(
                path, current_conv_id=request.conv_id or '')
    except Exception as exc:
        logger.debug('[ContextComposer] board unavailable: %s', exc)
    out.append(_block('project_board', 'project.board', board,
                      authority='ambient', placement='tail', stability='turn',
                      lifecycle='task', priority=40, max_tokens=900,
                      reason='' if board else 'no_active_claims',
                      provenance={'activeClaims': active}))

    digest = ''
    try:
        from lib.conversations.project_summary import build_project_digest
        has_tools = bool({'list_conversations', 'get_conversation'} &
                         set(request.tool_names))
        digest = build_project_digest(
            path, current_conv_id=request.conv_id or None,
            conv_tools_available=has_tools, query=query)
        if digest and request.task is not None:
            from lib.conversations.project_summary import project_digest_entries
            entries = project_digest_entries(
                path, current_conv_id=request.conv_id or None, query=query)
            request.task['_relatedConversations'] = {
                'count': len(entries), 'items': entries,
                'toolsAvailable': has_tools,
            }
    except Exception as exc:
        logger.debug('[ContextComposer] related conversations unavailable: %s',
                     exc)
    out.append(_block('related_conversations', 'project.conversations', digest,
                      authority='ambient', placement='tail', stability='turn',
                      lifecycle='task', priority=50, max_tokens=800,
                      reason='' if digest else 'no_relevant_conversations'))
    return out


def collect_context_blocks(messages: list[dict],
                           request: ComposeRequest) -> list[ContextBlock]:
    """Collect every ambient context source in one deterministic pass."""
    query = _last_user_text(messages)
    blocks: list[ContextBlock] = []
    role = (request.task or {}).get('_contextRoleBlock') or {}
    if isinstance(role, dict):
        role_name = str(role.get('name') or '').strip()
        role_content = str(role.get('content') or '').strip()
    else:
        role_name = ''
        role_content = ''
    existing_system = ''
    if messages and messages[0].get('role') == 'system':
        existing_system = str(messages[0].get('content') or '').strip()
    replace = request.system_prompt_mode == 'replace' and bool(existing_system)
    static = ''
    if not replace:
        try:
            import os
            cwd = request.project_path if request.project_enabled else ''
            from lib.context_experiment_flags import (
                normalize_context_experiment_flags)
            prompt_profile = normalize_context_experiment_flags(
                (request.task or {}).get('config') or {})['responses'][
                    'promptProfile']
            static = system_prompt_cc.build_static_prompt(
                cwd=cwd,
                is_git=bool(cwd and os.path.isdir(os.path.join(cwd, '.git'))),
                model=request.model, has_real_tools=request.has_real_tools,
                is_code_context=request.project_enabled,
                tool_names=set(request.tool_names) or None,
                disabled_blocks=set(request.disabled_blocks) or None,
                include_date=False,
                profile=prompt_profile,
            )
        except Exception as exc:
            logger.warning('[ContextComposer] static prompt unavailable: %s', exc)
    blocks.append(_block('platform_static', 'system_prompt_cc', static,
                         authority='platform', placement='system',
                         stability='static', lifecycle='conversation', priority=0,
                         reason='replace_mode' if replace else ('' if static else 'empty')))
    blocks.append(_block(
        f'role_{role_name or "none"}', 'endpoint.role', role_content,
        authority='workflow', placement='tail', stability='round',
        lifecycle='round', priority=0, max_tokens=5000,
        reason='' if role_content else 'ordinary_agent_role',
        provenance={'role': role_name or 'agent'},
    ))

    rules = _project_rules(request)
    blocks.append(_block('project_rules', 'project.AGENTS_CLAUDE', rules,
                         authority='project', placement='head',
                         stability='conversation', lifecycle='conversation',
                         priority=0, max_tokens=8000,
                         reason='' if rules else 'project_off_or_empty',
                         provenance={'path': request.project_path}))
    pref_core, pref_detail = _profile_blocks(request, query)
    blocks.extend([
        _block('user_context', 'memory.user_context', pref_core,
               authority='preference', placement='head',
               stability='conversation', lifecycle='conversation', priority=20,
               max_tokens=1000,
               reason='' if pref_core else 'preferences_disabled_or_empty'),
        _block('preference_detail_legacy', 'memory.user_context', pref_detail,
               authority='preference', placement='tail', stability='turn',
               lifecycle='task', priority=30, max_tokens=500,
               reason='' if pref_detail else 'all_context_is_always_on'),
    ])

    memory = _memory_guidance(request)
    blocks.append(_block('memory_guidance', 'memory', memory,
                         authority='ambient', placement='system',
                         stability='conversation', lifecycle='conversation',
                         priority=60, max_tokens=700,
                         reason='' if memory else 'memory_disabled_or_no_tools'))
    skills = _skill_index(request)
    blocks.append(_block('skills_index', 'skills.registry', skills,
                         authority='workflow', placement='system',
                         stability='conversation', lifecycle='conversation',
                         priority=40, max_tokens=1400,
                         reason='' if skills else 'no_enabled_skills'))
    vault = ''
    if request.has_real_tools:
        try:
            from lib.credentials_vault import build_vault_index
            vault = build_vault_index() or ''
        except Exception as exc:
            logger.debug('[ContextComposer] vault index unavailable: %s', exc)
    blocks.append(_block('credential_vault', 'credentials_vault', vault,
                         authority='user', placement='system',
                         stability='conversation', lifecycle='conversation',
                         priority=30, max_tokens=1000,
                         reason='' if vault else 'empty_or_no_tools'))
    swarm = _swarm_guidance(request)
    blocks.append(_block('parallel_execution', 'swarm', swarm,
                         authority='workflow', placement='system',
                         stability='static', lifecycle='conversation',
                         priority=50, max_tokens=1000,
                         reason='' if swarm else 'swarm_disabled'))
    blocks.extend(_project_blocks(request, query))
    prefetched = list((request.task or {}).get('_prefetchedMemories') or [])
    relevant = ''
    if prefetched:
        try:
            from lib.memory.prefetch._inject import (
                _render_relevant_memories_block,
            )
            relevant = _render_relevant_memories_block(prefetched)
        except Exception as exc:
            logger.warning('[ContextComposer] relevant memory render failed: %s',
                           exc)
    blocks.append(_block(
        'relevant_memories', 'memory.prefetch', relevant,
        authority='evidence', placement='tail', stability='turn',
        lifecycle='task', priority=70, max_tokens=1500,
        reason='' if relevant else 'no_high_confidence_matches',
        provenance={
            'selected': len(prefetched),
            'strategy': 'local_high_confidence',
            'matches': [
                {'name': row.get('name', ''),
                 'score': row.get('_prefetch_score', 0),
                 'reason': row.get('_prefetch_reason', '')}
                for row in prefetched
            ],
        },
    ))
    date = system_prompt_cc.section_current_date()
    blocks.append(_block('current_date', 'clock', date,
                         authority='ambient', placement='tail',
                         stability='turn', lifecycle='task', priority=90,
                         max_tokens=80))
    return blocks


__all__ = ['collect_context_blocks']
