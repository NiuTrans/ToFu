"""Canonical context providers for conversational model calls.

Providers only describe context. They never mutate the message list; physical
placement, deduplication, budgeting, and observability belong to the renderer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
import threading
from concurrent.futures import FIRST_COMPLETED, Future, InvalidStateError, wait
from dataclasses import replace

from lib.log import get_logger
from lib.tasks_pkg import system_prompt_cc
from lib.tasks_pkg.context_composer._models import ComposeRequest, ContextBlock
from lib.tasks_pkg.context_composer._provider_executor import (
    context_provider_executor as _CONTEXT_PROVIDER_EXECUTOR,
)

logger = get_logger(__name__)

# Context acquisition is on the first-token critical path. The providers are
# best-effort inputs, so one wedged filesystem/database adapter must not hold
# the entire request forever. This is a single deadline for the whole batch,
# not eight additive per-provider timeouts.
_CONTEXT_PROVIDER_DEADLINE_SECONDS = 15.0
_CONTEXT_PROVIDER_ABORT_POLL_SECONDS = 0.05
_CONTEXT_PROVIDER_NAMES = (
    "static", "project_rules", "profile", "memory", "skills", "vault",
    "swarm", "project",
)
_CONTEXT_PROVIDER_SIDE_EFFECT_OWNERS = {
    "_appliedPreferences": "profile",
    "_skillsIndexSnapshot": "skills",
    # Cursor confirmation is owned by the live task after the provider has
    # frozen a page on its request-local task copy.
    "_projectNarrativeDelivery": "project",
}

# Per-conversation baselines for tail-block TRANSITION detection. The
# environment and mcp_tools_delta blocks re-render every turn; the
# model-facing note and the turn-provenance chip must fire only when the
# rendered situation actually changed (project path moved, MCP tools
# appeared/disappeared), not on every steady-state turn. In-memory only,
# keyed by the MCP selection scope (conversation-scoped): a process restart
# simply re-baselines on the next turn and never fires a false transition.
_TAIL_TRANSITION_MAX_SCOPES = 256
_tail_transition_lock = threading.Lock()
_tail_transition_store: dict[str, dict[str, object]] = {}


def _tail_transition(scope: str, key: str, current: object) -> tuple[object, bool]:
    """Return ``(previous, changed)`` and learn ``current`` as the baseline.

    First sight never fires (there is no baseline to diff against); a steady
    state never fires. Callers pass a falsy ``scope`` to skip tracking.
    """
    if not scope:
        return None, False
    with _tail_transition_lock:
        if (len(_tail_transition_store) >= _TAIL_TRANSITION_MAX_SCOPES
                and scope not in _tail_transition_store):
            _tail_transition_store.pop(next(iter(_tail_transition_store)))
        entry = _tail_transition_store.setdefault(scope, {})
        previous = entry.get(key)
        changed = key in entry and previous != current
        entry[key] = current
        return previous, changed


def _reset_tail_transitions_for_tests() -> None:
    """Test hook: clear every stored transition baseline."""
    with _tail_transition_lock:
        _tail_transition_store.clear()

def _tail_transition_scope(request: ComposeRequest) -> str:
    task = request.task or {}
    try:
        from lib.mcp.tool_search import mcp_selection_scope_id

        return mcp_selection_scope_id(
            task_id=str(task.get("id") or ""),
            conv_id=str(request.conv_id or task.get("convId") or ""),
            owner_user_id=int(request.user_id or 0),
        )
    except Exception as exc:
        logger.debug(
            '[ContextComposer] MCP selection scope unavailable: %s',
            exc,
            exc_info=True,
        )
        return ""


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


def _project_rules(request: ComposeRequest) -> str:
    if not (request.project_enabled and request.project_path):
        return ""
    prefetched = (request.task or {}).get("_prefetch_project")
    if isinstance(prefetched, Future):
        try:
            # Reuse the one task-owned read even while it is still running.
            # Falling through to a second synchronous read races the same FUSE
            # tree and was a measurable source of preparation-tail latency.
            return (
                prefetched.result(
                    timeout=_CONTEXT_PROVIDER_DEADLINE_SECONDS
                )
                or ""
            )
        except TimeoutError:
            logger.warning(
                "[ContextComposer] project-rules prefetch exceeded %.1fs; "
                "the duplicate fallback read was suppressed",
                _CONTEXT_PROVIDER_DEADLINE_SECONDS,
            )
            return ""
        except Exception as exc:
            logger.warning(
                "[ContextComposer] project-rules prefetch failed: %s", exc
            )
            return ""
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

    preferences_enabled = resolve_preferences_enabled(cfg)
    if isinstance(request.task, dict):
        request.task['_preferencesEnabledResolved'] = preferences_enabled
    if not preferences_enabled:
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
        raise


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
        raise


def _memory_guidance(request: ComposeRequest) -> str:
    if not (request.has_real_tools and request.memory_enabled):
        return ""
    try:
        from lib.memory.injection import (
            MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT,
            build_memory_context,
        )

        known_available = None
        prefetch_state = (request.task or {}).get("_memoryPrefetch")
        if isinstance(prefetch_state, dict):
            if prefetch_state.get("phase") == "done":
                known_available = True
            elif (prefetch_state.get("phase") == "skipped"
                  and prefetch_state.get("reason") == "no_memories"):
                known_available = False
        context_kwargs = {}
        if known_available is not None:
            context_kwargs["known_available"] = known_available
        hint = build_memory_context(
            request.project_path if request.project_enabled else None,
            **context_kwargs,
        ) or ""
        return "\n\n".join(
            x for x in (hint, MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT) if x
        )
    except Exception as exc:
        logger.warning("[ContextComposer] memory guidance unavailable: %s", exc)
        raise


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
        raise
    return """<parallel_execution>
For two or more independent branches, use one spawn_agents call; keep sequential
work local. The tool schema is the sole authority for roles and their live
tools. Give self-contained bounded objectives, never fabricate results, and
call await_agents only when no useful local work remains.
</parallel_execution>"""


def _programmatic_guidance(request: ComposeRequest) -> str:
    """Return one conversation-stable PTC boundary, without round state."""
    if not request.has_real_tools:
        return ""
    try:
        from lib.context_experiment_flags import normalize_context_experiment_flags

        mode = normalize_context_experiment_flags(
            (request.task or {}).get("config") or {}
        )["tools"]["programmaticCalling"]
        if mode == "off":
            return ""
    except Exception as exc:
        logger.debug("[ContextComposer] programmatic mode unavailable: %s", exc)
        return ""
    return """<programmatic_execution>
When programmatic tool calling is available, use it only for bounded read-only
filtering, joining, ranking, deduplication, aggregation, or validation. Keep
writes, approvals, semantic judgment, and final artifact validation in direct
tool calls. Exact schemas and execution authority remain authoritative.
</programmatic_execution>"""


def _project_blocks(request: ComposeRequest, query: str) -> list[ContextBlock]:
    del query
    if not (request.project_enabled and request.project_path):
        context = ""
        reason = "project_disabled"
    elif ((request.task or {}).get('config') or {}).get('_storageFreeRuntime'):
        context = ""
        reason = "project_disabled"
    else:
        try:
            from lib.conversations.project_brain import prepare_project_context
            context = prepare_project_context(
                request.project_path,
                request.conv_id or '',
                user_id=request.user_id,
                task=request.task,
            )
            reason = '' if context else 'empty'
        except Exception as exc:
            logger.debug('[ContextComposer] Project Context unavailable: %s', exc)
            context = ''
            # Acquisition failure is not evidence that an earlier project
            # context stopped being true. The renderer retains its last
            # content-addressed version instead of retracting it.
            reason = 'provider_unavailable'
    return [
        _block(
            'project_context',
            'project.brain',
            context,
            authority='project',
            placement='tail',
            stability='turn',
            lifecycle='task',
            priority=10,
            max_tokens=1800,
            reason=reason,
            layer='objective_constraints',
            required=True,
        )
    ]


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


def _tool_search_guidance_available(request: ComposeRequest) -> bool:
    """Return whether this frozen task can discover hidden tool schemas."""
    if not request.has_real_tools:
        return False
    if 'search_tools' in request.tool_names:
        return True
    task = request.task if isinstance(request.task, dict) else {}
    mode = str(task.get('_toolSearchMode') or '').strip().lower()
    if not mode or mode == 'off':
        return False
    try:
        searchable_count = max(0, int(task.get('_toolSearchableCount') or 0))
        catalog_size = max(0, int(task.get('_toolSearchCatalogSize') or 0))
    except (TypeError, ValueError):
        return False
    from lib.tools.gateway import LOCAL_TOOL_SEARCH_MIN_FUNCTIONS
    return (
        searchable_count > 0
        and catalog_size >= LOCAL_TOOL_SEARCH_MIN_FUNCTIONS
    )


def collect_context_blocks(
    messages: list[dict], request: ComposeRequest
) -> list[ContextBlock]:
    """Collect every ambient context source in one deterministic pass."""
    query = _last_user_text(messages)
    tool_search_available = _tool_search_guidance_available(request)
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
    replace_mode = (
        request.system_prompt_mode == "replace" and bool(existing_system)
    )

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
        if replace_mode:
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
                tool_search_available=tool_search_available,
                disabled_blocks=set(request.disabled_blocks) or None,
                include_date=False,
                # Rendered as the per-turn ``environment`` tail block below,
                # so a project-path change never rewrites the cached prefix.
                include_environment=False,
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
            raise

    # Providers get a detached task snapshot. A callable that outlives the
    # request deadline can finish safely, but it cannot mutate the live task or
    # create a mixed-time context snapshot after composition has moved on.
    live_task = request.task if isinstance(request.task, dict) else None
    provider_task = None
    if live_task is not None:
        provider_task = dict(live_task)
        raw_config = live_task.get("config")
        if isinstance(raw_config, dict):
            provider_task["config"] = dict(raw_config)
    provider_request = replace(request, task=provider_task)

    defaults = {
        "static": ("", "provider_timeout"),
        "project_rules": "",
        "profile": "",
        "memory": "",
        "skills": "",
        "vault": "",
        "swarm": "",
        "project": [],
    }

    def _run_provider(name, fn, *args):
        started = time.monotonic()
        try:
            return True, fn(*args), (time.monotonic() - started) * 1000, ""
        except Exception as exc:
            logger.warning("[ContextComposer] provider %s failed: %s", name, exc)
            return False, defaults[name], (time.monotonic() - started) * 1000, str(exc)

    specs = {
        "static": (_build_static, ()),
        "profile": (_profile_block, (provider_request,)),
        "memory": (_memory_guidance, (provider_request,)),
        "skills": (_skill_index, (provider_request,)),
        "vault": (_build_vault, ()),
        "swarm": (_swarm_guidance, (provider_request, query)),
        "project": (_project_blocks, (provider_request, query)),
    }
    submitted_at = time.monotonic()
    futures = {
        name: _CONTEXT_PROVIDER_EXECUTOR.submit(
            _run_provider, name, fn, *args
        )
        for name, (fn, args) in specs.items()
    }
    prefetched_project = (provider_task or {}).get("_prefetch_project")
    if isinstance(prefetched_project, Future):
        # Do not spend another provider worker merely waiting on a Future that
        # already owns the read. With a lean two-worker pool, one wedged
        # project read plus one waiter would otherwise starve every unrelated
        # provider until the batch deadline.
        project_proxy = Future()

        def _settle_project_proxy(source: Future) -> None:
            if project_proxy.done():
                return
            duration_ms = (time.monotonic() - submitted_at) * 1000
            try:
                value = source.result() or ""
                outcome = (True, value, duration_ms, "")
            except Exception as exc:
                logger.warning(
                    "[ContextComposer] provider project_rules failed: %s", exc
                )
                outcome = (False, "", duration_ms, str(exc))
            try:
                project_proxy.set_result(outcome)
            except InvalidStateError:
                # The request deadline may cancel the proxy between the
                # done-check and settlement; the detached source remains safe.
                pass

        prefetched_project.add_done_callback(_settle_project_proxy)
        futures["project_rules"] = project_proxy
    else:
        futures["project_rules"] = _CONTEXT_PROVIDER_EXECUTOR.submit(
            _run_provider,
            "project_rules",
            _project_rules,
            provider_request,
        )
    pending = set(futures.values())
    aborted = False
    try:
        deadline = submitted_at + _CONTEXT_PROVIDER_DEADLINE_SECONDS
        while pending:
            abort_event = (live_task or {}).get("abort_event")
            if bool((live_task or {}).get("aborted")) or (
                abort_event is not None
                and callable(getattr(abort_event, "is_set", None))
                and abort_event.is_set()
            ):
                aborted = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(
                pending,
                timeout=min(_CONTEXT_PROVIDER_ABORT_POLL_SECONDS, remaining),
                return_when=FIRST_COMPLETED,
            )
            del done
    finally:
        for future in pending:
            future.cancel()

    values = dict(defaults)
    statuses: dict[str, str] = {}
    timings: list[dict] = []
    elapsed_ms = (time.monotonic() - submitted_at) * 1000
    for name in _CONTEXT_PROVIDER_NAMES:
        future = futures[name]
        detail = ""
        if future.done() and not future.cancelled():
            try:
                ok, value, duration_ms, detail = future.result(timeout=0)
                values[name] = value
                status = "ok" if ok else "error"
            except Exception as exc:
                duration_ms = elapsed_ms
                detail = str(exc)
                status = "error"
                logger.debug(
                    "[ContextComposer] completed provider %s raised while "
                    "collecting its result: %s",
                    name,
                    exc,
                )
        else:
            duration_ms = elapsed_ms
            status = "aborted" if aborted else "timeout"
        statuses[name] = status
        timing = {
            "provider": name,
            "status": status,
            "durationMs": round(max(0.0, duration_ms), 3),
        }
        if detail:
            timing["detail"] = detail[:240]
        timings.append(timing)

    if live_task is not None:
        live_task["_contextProviderTimings"] = timings
        for key, owner in _CONTEXT_PROVIDER_SIDE_EFFECT_OWNERS.items():
            if statuses.get(owner) == "ok" and key in (provider_task or {}):
                live_task[key] = copy.deepcopy(provider_task[key])
    degraded = [row for row in timings if row["status"] != "ok"]
    if degraded:
        logger.warning(
            "[ContextComposer] provider batch degraded after %.1fms: %s",
            elapsed_ms,
            ", ".join(
                f"{row['provider']}={row['status']}" for row in degraded
            ),
        )

    static, static_reason = values["static"]
    rules = values["project_rules"]
    user_context = values["profile"]
    memory = values["memory"]
    skills = values["skills"]
    vault = values["vault"]
    swarm = values["swarm"]
    project_blocks = values["project"]

    from lib.context_telemetry import build_prompt_profile_evidence

    prompt_status = (
        "applied" if static else "suppressed" if replace_mode else
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

    rules_reason = ""
    if not rules:
        if statuses.get("project_rules") != "ok":
            rules_reason = "provider_unavailable"
        elif not (request.project_enabled and request.project_path):
            rules_reason = "project_disabled"
        else:
            rules_reason = "project_empty"
    blocks.append(
        _block(
            "platform_static",
            "system_prompt_cc",
            static,
            authority="platform",
            placement="tail",
            stability="static",
            lifecycle="conversation",
            priority=0,
            reason=static_reason,
            provenance={"promptProfile": prompt_evidence},
            layer="objective_constraints",
            required=True,
        )
    )
    swarm_reason = ""
    if not swarm:
        if statuses.get("swarm") != "ok":
            swarm_reason = "provider_unavailable"
        else:
            try:
                from lib.context_experiment_flags import (
                    normalize_context_experiment_flags,
                )
                swarm_mode = normalize_context_experiment_flags(
                    (request.task or {}).get("config") or {}
                )["orchestration"]["multiAgent"]
                swarm_reason = (
                    "multi_agent_disabled"
                    if swarm_mode == "off" else "not_applicable"
                )
            except Exception:
                swarm_reason = "provider_unavailable"
    blocks.append(
        _block(
            "role_directive",
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
            placement="tail",
            stability="conversation",
            lifecycle="conversation",
            priority=0,
            max_tokens=8000,
            reason=rules_reason,
            provenance={"path": request.project_path},
            layer="objective_constraints",
            required=True,
        )
    )
    profile_reason = ""
    if not user_context:
        if statuses.get("profile") != "ok":
            profile_reason = "provider_unavailable"
        elif (provider_task or {}).get('_preferencesEnabledResolved') is False:
            profile_reason = "preferences_disabled"
        else:
            profile_reason = "profile_empty"
    blocks.extend(
        [
            _block(
                "user_context",
                "memory.user_context",
                user_context,
                authority="preference",
                placement="tail",
                stability="conversation",
                lifecycle="conversation",
                priority=20,
                max_tokens=1000,
                reason=profile_reason,
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
            placement="tail",
            stability="conversation",
            lifecycle="conversation",
            priority=60,
            max_tokens=700,
            reason=("" if memory else "memory_disabled"
                    if not (request.has_real_tools and request.memory_enabled)
                    else "provider_unavailable"),
            layer="cold_history",
        )
    )
    blocks.append(
        _block(
            "skills_index",
            "skills.registry",
            skills,
            authority="workflow",
            placement="tail",
            stability="conversation",
            lifecycle="conversation",
            priority=40,
            max_tokens=1400,
            reason=("" if skills else "skills_disabled"
                    if not request.has_real_tools
                    else "provider_unavailable"
                    if statuses.get("skills") != "ok"
                    else "no_enabled_skills"),
            layer="cold_history",
        )
    )
    blocks.append(
        _block(
            "credential_vault",
            "credentials_vault",
            vault,
            authority="user",
            placement="tail",
            stability="conversation",
            lifecycle="conversation",
            priority=30,
            max_tokens=1000,
            reason=("" if vault else "vault_disabled"
                    if not request.has_real_tools
                    else "provider_unavailable"
                    if statuses.get("vault") != "ok"
                    else "vault_empty"),
            layer="cold_history",
        )
    )
    blocks.append(
        _block(
            "parallel_execution",
            "swarm",
            swarm,
            authority="workflow",
            placement="tail",
            stability="static",
            lifecycle="conversation",
            priority=50,
            max_tokens=128,
            reason=swarm_reason,
            layer="cold_history",
        )
    )
    programmatic = _programmatic_guidance(request)
    programmatic_reason = ""
    if not programmatic:
        if not request.has_real_tools:
            programmatic_reason = "programmatic_disabled"
        else:
            try:
                from lib.context_experiment_flags import (
                    normalize_context_experiment_flags,
                )
                programmatic_mode = normalize_context_experiment_flags(
                    (request.task or {}).get("config") or {}
                )["tools"]["programmaticCalling"]
                programmatic_reason = (
                    "programmatic_disabled"
                    if programmatic_mode == "off" else "provider_unavailable"
                )
            except Exception:
                programmatic_reason = "provider_unavailable"
    blocks.append(
        _block(
            "programmatic_execution",
            "tools.programmatic",
            programmatic,
            authority="workflow",
            placement="tail",
            stability="static",
            lifecycle="conversation",
            priority=55,
            max_tokens=128,
            reason=programmatic_reason,
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
    environment = ""
    environment_reason = "disabled"
    env_cwd = request.project_path if request.project_enabled else ""
    if "environment" not in request.disabled_blocks:
        environment_reason = "empty"
        try:
            import os

            _cfg = (request.task or {}).get("config") or {}
            _project_paths = _cfg.get("projectPaths") or []
            _extra_roots = (
                [str(path) for path in _project_paths[1:] if path]
                if isinstance(_project_paths, (list, tuple)) else []
            )
            environment = system_prompt_cc.section_environment(
                cwd=env_cwd,
                is_git=bool(env_cwd and os.path.isdir(os.path.join(env_cwd, ".git"))),
                model=request.model,
                extra_roots=_extra_roots,
                has_real_tools=request.has_real_tools,
            )
            if _extra_roots:
                environment += (
                    "\n - Multi-root path rule: use an absolute path or "
                    "`rootname:subdir`; a bare relative path uses the primary root."
                )
            if _cfg.get("project_remote"):
                environment += (
                    "\n - Remote worktree: project tools execute on the user's "
                    "local machine through the desktop agent. Paths are relative "
                    "to that bound root. Server-vault credentials are unavailable; "
                    "configure credentials on the desktop agent."
                )
            if request.memory_enabled and not (
                    request.project_enabled and request.project_path):
                environment += (
                    "\n - Memory scope: no project is attached; use global "
                    "scope for create_memory and merge_memories."
                )
            environment_reason = "" if environment else "empty"
        except Exception as exc:
            environment_reason = "build_failed"
            logger.warning("[ContextComposer] environment block failed: %s", exc)
    task = request.task or {}
    project_path_change = ""
    project_path_change_id = ""
    previous_path, path_changed = _tail_transition(
        _tail_transition_scope(request), "project_path", env_cwd)
    if path_changed:
        task["_projectPathChange"] = {
            "from": str(previous_path or ""), "to": env_cwd}
        shown_old = str(previous_path or "") or "(none)"
        shown_new = env_cwd or "(none)"
        project_path_change = (
            f"The project path changed from \"{shown_old}\" to "
            f"\"{shown_new}\" since your previous turn. Use the new path "
            "for file operations; absolute paths in earlier tool results "
            "may be stale.")
        transition_digest = hashlib.sha256(
            f"{shown_old}\0{shown_new}".encode("utf-8")
        ).hexdigest()[:12]
        project_path_change_id = f"project_path_change_{transition_digest}"
    else:
        task.pop("_projectPathChange", None)
    blocks.append(
        _block(
            "environment",
            "platform.environment",
            environment,
            authority="platform",
            placement="tail",
            stability="turn",
            lifecycle="task",
            priority=80,
            max_tokens=400,
            reason=environment_reason,
            layer="hot_tail",
            required=True,
        )
    )
    if project_path_change:
        # A path move is a historical event, not part of the current
        # environment snapshot. Give each transition a stable unique id so it
        # is appended once and never makes the environment change a second
        # time when the note naturally stops being emitted next turn.
        blocks.append(
            _block(
                project_path_change_id,
                "platform.environment.transition",
                project_path_change,
                authority="platform",
                placement="tail",
                stability="turn",
                lifecycle="task",
                priority=79,
                max_tokens=160,
                layer="hot_tail",
                required=True,
            )
        )
    mcp_delta, mcp_delta_reason, mcp_delta_provenance = _mcp_tools_delta(request)
    blocks.append(
        _block(
            "mcp_tools_delta",
            "mcp.catalog",
            mcp_delta,
            authority="workflow",
            placement="tail",
            stability="turn",
            lifecycle="task",
            priority=40,
            max_tokens=2000,
            reason=mcp_delta_reason,
            provenance=mcp_delta_provenance,
            layer="evidence",
            recovery_handle="tool:search_tools",
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


def _mcp_tools_delta(request: ComposeRequest) -> tuple[str, str, dict]:
    """Render connected-but-not-on-wire MCP tools as a per-turn tail block.

    The MCP wire freezes at the conversation's first tool assembly (see
    ``lib.mcp.tool_search.select_active_mcp_tools``), so a server that
    connects or reconnects afterwards can no longer enter the tools array
    without invalidating the whole provider prefix cache. Its schemas are
    surfaced here instead; ``execute_tools`` already holds execution
    authority for the full connected catalog, and the block refreshes on
    every turn (a mid-turn reconnect becomes visible next turn).
    """
    task = request.task or {}
    try:
        from lib.mcp.tool_search import (
            frozen_wire_tool_names,
            mcp_selection_scope_id,
        )

        scope = mcp_selection_scope_id(
            task_id=str(task.get("id") or ""),
            conv_id=str(request.conv_id or task.get("convId") or ""),
            owner_user_id=int(request.user_id or 0),
        )
        wire_names, frozen = frozen_wire_tool_names(scope)
    except Exception as exc:
        logger.debug("[ContextComposer] mcp wire state unavailable: %s", exc)
        return "", "state_unavailable", {}
    if not frozen:
        # First turn: the wire is assembled after composition, so there is
        # nothing frozen to diff against yet.
        return "", "wire_not_frozen", {}
    try:
        from lib.mcp import get_bridge

        bridge = get_bridge()
        defs = bridge.get_openai_tool_defs() if bridge.connected else []
    except Exception as exc:
        logger.debug("[ContextComposer] mcp bridge unavailable: %s", exc)
        return "", "bridge_unavailable", {}
    wire = set(wire_names)
    delta: list[tuple[str, str, dict]] = []
    for tool in defs or ():
        fn = tool.get("function") or {}
        name = str(fn.get("name") or "")
        if name and name not in wire:
            delta.append((
                name,
                str(fn.get("description") or ""),
                fn.get("parameters") or fn.get("input_schema") or {},
            ))
    names = sorted(row[0] for row in delta)
    previous_names, delta_changed = _tail_transition(scope, "mcp_delta", names)
    if delta_changed:
        current = set(names)
        previous = set(previous_names or [])
        task["_mcpToolsDelta"] = {
            "added": sorted(current - previous)[:8],
            "removed": sorted(previous - current)[:8],
            "total": len(names),
        }
    else:
        task.pop("_mcpToolsDelta", None)
    if not delta:
        return "", "no_delta", {"wire": len(wire)}
    delta.sort(key=lambda row: row[0])
    shown = delta[:8]
    lines = [
        "<available_mcp_tools>",
        "These MCP tools are connected now but are NOT in this conversation's "
        "frozen tool declarations (their server connected or reconnected "
        "after the first turn). Call any of them through execute_tools with "
        "the exact name and arguments matching input_schema; search_tools "
        "rediscovers them too. This list refreshes every turn — a tool that "
        "disappears means its server disconnected.",
        "",
    ]
    for name, description, schema in shown:
        desc = " ".join(description.split())[:160]
        schema_text = json.dumps(
            schema, ensure_ascii=False, separators=(",", ":"))
        if len(schema_text) > 1200:
            schema_text = schema_text[:1200] + "…"
        lines.append(f"- {name}" + (f" — {desc}" if desc else ""))
        lines.append(f"  input_schema: {schema_text}")
    overflow = len(delta) - len(shown)
    if overflow > 0:
        lines.append(f"…and {overflow} more; discover them with search_tools.")
    lines.append("</available_mcp_tools>")
    return ("\n".join(lines), "",
            {"delta": len(delta), "shown": len(shown), "wire": len(wire)})

__all__ = ["collect_context_blocks"]
