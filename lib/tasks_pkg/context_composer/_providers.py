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


def _block(
    block_id: str,
    source: str,
    content: str,
    *,
    authority: str,
    placement: str,
    stability: str,
    lifecycle: str,
    priority: int,
    max_tokens: int | None = None,
    reason: str = "",
    provenance: dict | None = None,
    layer: str = "cold_history",
    required: bool = False,
    required_permissions: frozenset[str] = frozenset(),
    access_count: int = 0,
    observed_at_ms: int = 0,
    world_version: str = "",
    recovery_handle: str = "",
) -> ContextBlock:
    return ContextBlock(
        id=block_id,
        source=source,
        content=content or "",
        authority=authority,
        placement=placement,
        stability=stability,
        lifecycle=lifecycle,
        priority=priority,
        max_tokens=max_tokens,
        suppressed_reason=reason,
        provenance=provenance or {},
        layer=layer,
        required=required,
        required_permissions=required_permissions,
        access_count=access_count,
        observed_at_ms=observed_at_ms,
        world_version=world_version,
        recovery_handle=recovery_handle,
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
        logger.debug("[ContextComposer] future %s failed: %s", key, exc)
        return None


def _project_rules(request: ComposeRequest) -> str:
    if not (request.project_enabled and request.project_path):
        return ""
    prefetched = _future_value(request.task, "_prefetch_project")
    if prefetched is not None:
        return prefetched or ""
    try:
        from lib.project_mod import get_context_for_prompt

        return (
            get_context_for_prompt(
                request.project_path, conv_id=request.conv_id or None
            )
            or ""
        )
    except Exception as exc:
        logger.warning("[ContextComposer] project rules unavailable: %s", exc)
        return ""


def _profile_block(request: ComposeRequest) -> str:
    cfg = (request.task or {}).get("config") or {}
    from lib.agent_core.personal_scope import resolve_preferences_enabled

    if not resolve_preferences_enabled(cfg):
        return ""
    try:
        from lib.memory.user_profile import (
            context_items_for_event,
            context_char_count,
            load_context,
            render_profile_block,
        )

        scope = (request.task or {}).get("_profileScope") or ""
        context = load_context(scope)
        block = render_profile_block(None, scope)
        if context["items"] and request.task is not None:
            items = context_items_for_event(None, scope)
            request.task["_appliedPreferences"] = {
                "chars": context_char_count(context["items"]),
                "items": items,
            }
        return block or ""
    except Exception as exc:
        logger.warning("[ContextComposer] preference profile unavailable: %s", exc)
        return ""


def _skill_index(request: ComposeRequest) -> str:
    if not request.has_real_tools:
        return ""
    task = request.task
    if isinstance(task, dict) and '_skillsIndexSnapshot' in task:
        snapshot = task.get('_skillsIndexSnapshot')
        return snapshot if isinstance(snapshot, str) else ""
    try:
        from lib.skills import build_skills_index

        paths = list(
            ((request.task or {}).get("config") or {}).get("projectPaths") or []
        )
        extras = [p for p in paths if p and p != request.project_path]
        snapshot = (
            build_skills_index(
                request.project_path if request.project_enabled else None,
                extra_paths=extras,
                # Zero is the retained ComposeRequest default for offline
                # tests/utilities. Authenticated task composition always
                # carries a positive owner; only the offline lane selects the
                # legacy compatibility store via None.
                owner_user_id=request.user_id or None,
                model=request.model,
                max_tokens=1200,
            )
            or ""
        )
        # Freeze even an empty index for this task. An approved same-turn
        # install returns its exact id and remains explicitly loadable without
        # mutating the already-composed system prefix.
        if isinstance(task, dict):
            task['_skillsIndexSnapshot'] = snapshot
        return snapshot
    except Exception as exc:
        logger.warning("[ContextComposer] skill index unavailable: %s", exc)
        return ""


def _memory_guidance(request: ComposeRequest) -> str:
    if not (request.has_real_tools and request.memory_enabled):
        return ""
    try:
        from lib.memory.injection import (
            MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT,
            build_memory_context,
        )

        hint = (
            build_memory_context(
                request.project_path if request.project_enabled else None
            )
            or ""
        )
        return "\n\n".join(
            x for x in (hint, MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT) if x
        )
    except Exception as exc:
        logger.warning("[ContextComposer] memory guidance unavailable: %s", exc)
        return ""


def _swarm_guidance(request: ComposeRequest, query: str) -> str:
    """Return guidance only when the request's task shape can use it."""
    try:
        from lib.context_experiment_flags import normalize_context_experiment_flags
        from lib.tasks_pkg.tool_orchestration_policy import multi_agent_task_shape

        mode = normalize_context_experiment_flags(
            (request.task or {}).get("config") or {}
        )["orchestration"]["multiAgent"]
        if mode == "off" or (mode == "auto" and not multi_agent_task_shape(query)):
            return ""
    except Exception as exc:
        logger.debug("[ContextComposer] swarm task-shape gate failed: %s", exc)
        return ""
    try:
        from lib.swarm.registry import format_role_catalogue

        roles = format_role_catalogue()
    except Exception as exc:
        logger.debug("[ContextComposer] swarm catalogue fallback: %s", exc)
        roles = "general — independent bounded work"
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
    task_config = (request.task or {}).get('config') or {}
    if task_config.get('_storageFreeRuntime'):
        return []
    out: list[ContextBlock] = []
    path = request.project_path
    try:
        from lib.conversations.project_charter import render_charter_injection_block

        charter = render_charter_injection_block(
            path, user_id=request.user_id)
    except Exception as exc:
        logger.debug("[ContextComposer] charter unavailable: %s", exc)
        charter = ""
    out.append(
        _block(
            "project_charter",
            "project.charter",
            charter,
            authority="project",
            placement="tail",
            stability="turn",
            lifecycle="task",
            priority=10,
            max_tokens=1200,
            reason="" if charter else "empty",
            layer="objective_constraints",
            required=True,
            recovery_handle="tool:project_charter_read",
        )
    )
    try:
        from lib.conversations.project_watch import render_goals_injection_block

        goals = render_goals_injection_block(path, user_id=request.user_id)
    except Exception as exc:
        logger.debug("[ContextComposer] goals unavailable: %s", exc)
        goals = ""
    out.append(
        _block(
            "project_goals",
            "project.goals",
            goals,
            authority="project",
            placement="tail",
            stability="turn",
            lifecycle="task",
            priority=20,
            max_tokens=800,
            reason="" if goals else "empty",
            layer="objective_constraints",
            required=True,
            recovery_handle="tool:project_goals_read",
        )
    )

    # The board is ambient only when an active claim/lease can change what
    # this agent should touch. Open/done backlog remains pull-based.
    board = ""
    active = 0
    try:
        from lib.conversations.project_board import (
            read_board,
            render_board_injection_block,
        )

        board_snapshot = read_board(path, user_id=request.user_id) or {}
        rows = board_snapshot.get("tasks") or []
        active = sum(1 for row in rows if row.get("status") == "claimed")
        if active:
            board = render_board_injection_block(
                path,
                current_conv_id=request.conv_id or "",
                user_id=request.user_id,
                board_snapshot=board_snapshot,
            )
    except Exception as exc:
        logger.debug("[ContextComposer] board unavailable: %s", exc)
    out.append(
        _block(
            "project_board",
            "project.board",
            board,
            authority="ambient",
            placement="tail",
            stability="turn",
            lifecycle="task",
            priority=40,
            max_tokens=900,
            reason="" if board else "no_active_claims",
            provenance={"activeClaims": active},
            layer="evidence",
            recovery_handle="tool:project_board_read",
        )
    )

    digest = ""
    try:
        from lib.conversations.project_summary import (
            build_project_digest_projection,
        )

        has_tools = bool(
            {"list_conversations", "get_conversation"} & set(request.tool_names)
        )
        projection = build_project_digest_projection(
            path,
            user_id=request.user_id,
            current_conv_id=request.conv_id or None,
            conv_tools_available=has_tools,
            query=query,
        )
        digest = projection.text
        if digest and request.task is not None:
            entries = [dict(entry) for entry in projection.entries]
            request.task["_relatedConversations"] = {
                "count": len(entries),
                "items": entries,
                "toolsAvailable": has_tools,
            }
    except Exception as exc:
        logger.debug("[ContextComposer] related conversations unavailable: %s", exc)
    out.append(
        _block(
            "related_conversations",
            "project.conversations",
            digest,
            authority="ambient",
            placement="tail",
            stability="turn",
            lifecycle="task",
            priority=50,
            max_tokens=800,
            reason="" if digest else "no_relevant_conversations",
            layer="evidence",
            recovery_handle="tool:get_conversation",
        )
    )
    return out


def _plan_mode_block(request: ComposeRequest) -> str:
    """Plan Mode behavioural contract (Codex plan.md adapted to Tofu).

    Reads ``planMode`` off the task config; empty when the toggle is off so
    the block suppresses itself with zero prompt cost on ordinary turns.
    """
    try:
        cfg = (request.task or {}).get("config") or {}
        from lib.tasks_pkg.plan_mode import plan_mode_enabled, plan_mode_prompt_block

        if not plan_mode_enabled(cfg):
            return ""
        return plan_mode_prompt_block()
    except Exception as exc:
        logger.warning("[ContextComposer] plan-mode block unavailable: %s", exc)
        return ""


def collect_context_blocks(
    messages: list[dict], request: ComposeRequest
) -> list[ContextBlock]:
    """Collect every ambient context source in one deterministic pass."""
    query = _last_user_text(messages)
    blocks: list[ContextBlock] = []
    role = (request.task or {}).get("_contextRoleBlock") or {}
    if isinstance(role, dict):
        role_name = str(role.get("name") or "").strip()
        role_content = str(role.get("content") or "").strip()
    else:
        role_name = ""
        role_content = ""
    existing_system = ""
    if messages and messages[0].get("role") == "system":
        existing_system = str(messages[0].get("content") or "").strip()
    replace = request.system_prompt_mode == "replace" and bool(existing_system)

    try:
        from lib.context_experiment_flags import normalize_context_experiment_flags

        requested_prompt_profile = normalize_context_experiment_flags(
            (request.task or {}).get("config") or {}
        )["responses"]["promptProfile"]
    except Exception as exc:
        logger.warning(
            "[ContextComposer] prompt profile unavailable; using auto: %s", exc
        )
        requested_prompt_profile = "auto"
    resolved_prompt_profile = system_prompt_cc.resolve_static_prompt_profile(
        request.model, requested_prompt_profile
    )

    def _build_static() -> tuple[str, str]:
        if replace:
            return "", "replace_mode"
        try:
            import os

            cwd = request.project_path if request.project_enabled else ""
            content = system_prompt_cc.build_static_prompt(
                cwd=cwd,
                is_git=bool(cwd and os.path.isdir(os.path.join(cwd, ".git"))),
                model=request.model,
                has_real_tools=request.has_real_tools,
                is_code_context=request.project_enabled,
                tool_names=set(request.tool_names) or None,
                disabled_blocks=set(request.disabled_blocks) or None,
                include_date=False,
                profile=resolved_prompt_profile,
            )
            return content, "" if content else "empty"
        except Exception as exc:
            logger.warning("[ContextComposer] static prompt unavailable: %s", exc)
            return "", "build_failed"

    def _build_vault() -> str:
        if not request.has_real_tools:
            return ""
        try:
            from lib.credentials_vault import build_vault_index

            return build_vault_index() or ""
        except Exception as exc:
            logger.debug("[ContextComposer] vault index unavailable: %s", exc)
            return ""

    # Independent providers run concurrently (mirrors the orchestrator's
    # _prefetch pool seam). Values are joined BEFORE any block is appended, so
    # output order and task side effects stay byte-identical to the serial path.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(
        max_workers=6, thread_name_prefix="context-provider"
    ) as _pool:
        _f_static = _pool.submit(_build_static)
        _f_rules = _pool.submit(_project_rules, request)
        _f_profile = _pool.submit(_profile_block, request)
        _f_memory = _pool.submit(_memory_guidance, request)
        _f_skills = _pool.submit(_skill_index, request)
        _f_vault = _pool.submit(_build_vault)
        _f_swarm = _pool.submit(_swarm_guidance, request, query)
        _f_project = _pool.submit(_project_blocks, request, query)

        static, static_reason = _f_static.result()
        rules = _f_rules.result()
        user_context = _f_profile.result()
        memory = _f_memory.result()
        skills = _f_skills.result()
        vault = _f_vault.result()
        swarm = _f_swarm.result()
        project_blocks = _f_project.result()

    from lib.context_telemetry import build_prompt_profile_evidence

    prompt_status = (
        "applied" if static else "suppressed" if replace else
        "error" if static_reason == "build_failed" else "empty"
    )
    prompt_evidence = build_prompt_profile_evidence(
        requested_profile=requested_prompt_profile,
        resolved_profile=resolved_prompt_profile,
        content=static,
        model=request.model,
        status=prompt_status,
        reason=static_reason,
    )
    prompt_evidence["disabledBlocks"] = sorted(request.disabled_blocks)
    if request.task is not None:
        request.task["_promptProfileV1"] = dict(prompt_evidence)

    blocks.append(
        _block(
            "platform_static",
            "system_prompt_cc",
            static,
            authority="platform",
            placement="system",
            stability="static",
            lifecycle="conversation",
            priority=0,
            reason=static_reason,
            provenance={"promptProfile": prompt_evidence},
            layer="objective_constraints",
            required=True,
        )
    )
    blocks.append(
        _block(
            f"role_{role_name or 'none'}",
            "endpoint.role",
            role_content,
            authority="workflow",
            placement="tail",
            stability="round",
            lifecycle="round",
            priority=0,
            max_tokens=5000,
            reason="" if role_content else "ordinary_agent_role",
            provenance={"role": role_name or "agent"},
            layer="objective_constraints",
            required=True,
        )
    )

    blocks.append(
        _block(
            "project_rules",
            "project.AGENTS_CLAUDE",
            rules,
            authority="project",
            placement="head",
            stability="conversation",
            lifecycle="conversation",
            priority=0,
            max_tokens=8000,
            reason="" if rules else "project_off_or_empty",
            provenance={"path": request.project_path},
            layer="objective_constraints",
            required=True,
        )
    )
    blocks.extend(
        [
            _block(
                "user_context",
                "memory.user_context",
                user_context,
                authority="preference",
                placement="head",
                stability="conversation",
                lifecycle="conversation",
                priority=20,
                max_tokens=1000,
                reason=("" if user_context
                        else "preferences_disabled_or_empty"),
                layer="objective_constraints",
            ),
        ]
    )

    blocks.append(
        _block(
            "memory_guidance",
            "memory",
            memory,
            authority="ambient",
            placement="system",
            stability="conversation",
            lifecycle="conversation",
            priority=60,
            max_tokens=700,
            reason="" if memory else "memory_disabled_or_no_tools",
            layer="cold_history",
        )
    )
    blocks.append(
        _block(
            "skills_index",
            "skills.registry",
            skills,
            authority="workflow",
            placement="system",
            stability="conversation",
            lifecycle="conversation",
            priority=40,
            max_tokens=1400,
            reason="" if skills else "no_enabled_skills",
            layer="cold_history",
        )
    )
    blocks.append(
        _block(
            "credential_vault",
            "credentials_vault",
            vault,
            authority="user",
            placement="system",
            stability="conversation",
            lifecycle="conversation",
            priority=30,
            max_tokens=1000,
            reason="" if vault else "empty_or_no_tools",
            layer="cold_history",
        )
    )
    blocks.append(
        _block(
            "parallel_execution",
            "swarm",
            swarm,
            authority="workflow",
            placement="system",
            stability="static",
            lifecycle="conversation",
            priority=50,
            max_tokens=1000,
            reason="" if swarm else "empty",
            layer="cold_history",
        )
    )
    blocks.extend(project_blocks)
    if request.global_budget_tokens is not None:
        from lib.tasks_pkg.context_composer.task_state import (
            derive_task_state_snapshot,
        )

        snapshot = derive_task_state_snapshot(messages, request.task)
        if request.task is not None:
            request.task["_taskStateSnapshotV1"] = snapshot.to_dict()
        blocks.append(
            _block(
                "task_state_v1",
                "task.events.projection",
                snapshot.to_context_text(),
                authority="workflow",
                placement="tail",
                stability="turn",
                lifecycle="task",
                priority=5,
                max_tokens=2400,
                layer="task_state",
                required=True,
                observed_at_ms=snapshot.observed_at_ms,
                world_version=snapshot.world_version,
                recovery_handle=f"transcript:{snapshot.source_digest}",
                provenance={"contractVersion": snapshot.contract_version,
                            "sourceDigest": snapshot.source_digest},
            )
        )
    prefetched = list((request.task or {}).get("_prefetchedMemories") or [])
    relevant = ""
    if prefetched:
        try:
            from lib.memory.prefetch._inject import (
                _render_relevant_memories_block,
            )

            relevant = _render_relevant_memories_block(prefetched)
        except Exception as exc:
            logger.warning("[ContextComposer] relevant memory render failed: %s", exc)
    blocks.append(
        _block(
            "relevant_memories",
            "memory.prefetch",
            relevant,
            authority="evidence",
            placement="tail",
            stability="turn",
            lifecycle="task",
            priority=70,
            max_tokens=1500,
            reason="" if relevant else "no_high_confidence_matches",
            provenance={
                "selected": len(prefetched),
                "strategy": "local_high_confidence",
                "matches": [
                    {
                        "name": row.get("name", ""),
                        "score": row.get("_prefetch_score", 0),
                        "reason": row.get("_prefetch_reason", ""),
                    }
                    for row in prefetched
                ],
            },
            layer="evidence",
            recovery_handle="tool:search_memory",
        )
    )
    # Plan Mode is turn-scoped and must sit close to the generation
    # boundary (the user may flip it any turn), so tail placement — the same
    # recency slot used by other turn-scoped role directives.
    _plan_mode = _plan_mode_block(request)
    blocks.append(
        _block(
            "plan_mode",
            "plan_mode.contract",
            _plan_mode,
            authority="workflow",
            placement="tail",
            stability="turn",
            lifecycle="task",
            priority=15,
            max_tokens=900,
            reason="" if _plan_mode else "plan_mode_off",
            layer="objective_constraints",
            required=True,
        )
    )
    date = system_prompt_cc.section_current_date()
    blocks.append(
        _block(
            "current_date",
            "clock",
            date,
            authority="ambient",
            placement="tail",
            stability="turn",
            lifecycle="task",
            priority=90,
            max_tokens=80,
            layer="hot_tail",
        )
    )
    return blocks


__all__ = ["collect_context_blocks"]
