"""Approval delegation — an agent-registered policy adjudicated by a sub-agent.

The orchestrator registers a natural-language approval policy through the
``set_approval_delegate`` tool (e.g. "read-only build/test commands are fine;
anything touching git remotes or package installs is not"). From then on, a
tool call that WOULD reach the human write-approval gate is first judged by a
bounded, tool-less adjudicator child invocation against that policy.

Safety shape (fail-closed everywhere):

  * The adjudicator runs with NO tools, a fresh context, one bounded round,
    and a wall-clock deadline. It can only talk.
  * Approve only binds a single-use receipt (``authority='agent_adjudicator'``)
    to the exact call — the same authority discipline as a human decision.
  * Undecided / malformed / timed-out adjudication falls THROUGH to the
    ordinary human gate. A delegate can never make the gate weaker than
    ``ToolSpec.auto_approval_tools`` already allows, and it never applies to
    child invocations (which have no human-interaction port at all).
  * The policy is task-scoped: it dies with the task that registered it and
    is never inherited by child invocations.

This module owns the config contract, the tool handler, and the adjudicator
runtime. The gate insertion point lives in
``lib/tasks_pkg/tool_dispatch/_pipeline.py::_approval_stops_tool_call``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'DELEGATE_CONFIG_KEY',
    'SET_APPROVAL_DELEGATE_TOOL',
    'adjudicate_tool_call',
    'approval_delegate_for',
    'parse_adjudicator_verdict',
    'register_approval_delegate',
]

DELEGATE_CONFIG_KEY = '_approvalDelegate'

_POLICY_MAX_CHARS = 4_000
_TOOLS_MAX_ITEMS = 32
_TOOL_NAME_MAX_CHARS = 128
_DEFAULT_TIMEOUT_SECONDS = 30
_MAX_TIMEOUT_SECONDS = 120
_MIN_TIMEOUT_SECONDS = 5
_REASON_MAX_CHARS = 500
_ARGS_PREVIEW_CHARS = 6_000

SET_APPROVAL_DELEGATE_TOOL = {
    "type": "function",
    "function": {
        "name": "set_approval_delegate",
        "description": (
            "Delegate tool-call approval decisions to a bounded adjudicator "
            "sub-agent governed by a natural-language policy you write. While "
            "a policy is active, a gated tool call (e.g. a destructive "
            "run_command) is judged against the policy FIRST: a clear approve "
            "executes without prompting the human, a clear deny rejects the "
            "call with the reason returned to you, and anything uncertain "
            "falls back to the human approval gate. Use this to keep working "
            "autonomously through batches of predictable approvals. The "
            "policy is task-scoped, never widens what the gate allows, and "
            "never applies to sub-agents. Requires an attended session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": (
                        "false clears the active policy; default true."
                    ),
                },
                "policy": {
                    "type": "string",
                    "description": (
                        "Natural-language approval rules the adjudicator "
                        "applies verbatim, e.g. 'Allow read-only build and "
                        "test commands; deny anything touching git remotes, "
                        "package managers, or files outside the project.' "
                        "Required when enabling."
                    ),
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Tool names this policy covers (default "
                        "['run_command']). Explicit names only — no "
                        "wildcards."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        "Adjudicator wall-clock budget per decision "
                        "(default 30, range 5..120). On timeout the call "
                        "falls back to the human gate."
                    ),
                },
                "model_tier": {
                    "type": "string",
                    "enum": ["light", "standard"],
                    "description": (
                        "Adjudicator model tier (default 'light'). Choose "
                        "'standard' for subtle policies."
                    ),
                },
            },
        },
    },
}


# ═══════════════════════════════════════════════════════════
#  Tool handler — register / clear the task-scoped policy
# ═══════════════════════════════════════════════════════════

def register_approval_delegate(task: dict, fn_args: dict) -> str:
    """Handle one ``set_approval_delegate`` call; returns JSON for the model."""
    cfg = task.setdefault('config', {})
    enabled = fn_args.get('enabled', True)
    if enabled is False or str(enabled).strip().lower() in ('false', '0', 'off'):
        existed = cfg.pop(DELEGATE_CONFIG_KEY, None) is not None
        return json.dumps({
            'status': 'ok',
            'delegate': 'cleared' if existed else 'already_inactive',
            'message': ('Approval delegation cleared; gated calls return to '
                        'the human approval gate.' if existed else
                        'No approval delegation was active.'),
        }, ensure_ascii=False)

    policy = str(fn_args.get('policy') or '').strip()
    if not policy:
        return json.dumps({
            'status': 'error',
            'error': 'policy_required',
            'message': 'Enabling the delegate requires a non-empty policy.',
        })
    if len(policy) > _POLICY_MAX_CHARS:
        return json.dumps({
            'status': 'error',
            'error': 'policy_too_long',
            'message': f'Policy exceeds {_POLICY_MAX_CHARS} characters.',
        })

    raw_tools = fn_args.get('tools')
    tools: list[str] = []
    if isinstance(raw_tools, list):
        for raw in raw_tools[:_TOOLS_MAX_ITEMS]:
            name = str(raw or '').strip()
            if not name or len(name) > _TOOL_NAME_MAX_CHARS:
                continue
            if any(ch in name for ch in '*?[]'):
                return json.dumps({
                    'status': 'error',
                    'error': 'wildcards_rejected',
                    'message': ('Delegate scope requires explicit tool names; '
                                'wildcards are not allowed.'),
                })
            if name not in tools:
                tools.append(name)
    if not tools:
        tools = ['run_command']

    try:
        timeout = int(fn_args.get('timeout_seconds') or _DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError, OverflowError):
        timeout = _DEFAULT_TIMEOUT_SECONDS
    timeout = max(_MIN_TIMEOUT_SECONDS, min(timeout, _MAX_TIMEOUT_SECONDS))

    model_tier = str(fn_args.get('model_tier') or 'light').strip().lower()
    if model_tier not in ('light', 'standard'):
        model_tier = 'light'

    cfg[DELEGATE_CONFIG_KEY] = {
        'policy': policy,
        'tools': tools,
        'timeout_seconds': timeout,
        'model_tier': model_tier,
        'registered_at': time.time(),
    }
    logger.info('[ApprovalDelegate] registered for task %s: tools=%s '
                'timeout=%ds tier=%s', str(task.get('id'))[:8], tools,
                timeout, model_tier)
    return json.dumps({
        'status': 'ok',
        'delegate': 'active',
        'tools': tools,
        'timeout_seconds': timeout,
        'model_tier': model_tier,
        'message': (
            f'Approval delegation active for: {", ".join(tools)}. Gated '
            'calls are now judged against your policy first; uncertain '
            'judgments fall back to the human gate. Call again with '
            'enabled=false to clear.'),
    }, ensure_ascii=False)


def approval_delegate_for(task: dict, fn_name: str) -> dict | None:
    """Return the active policy when it covers ``fn_name``, else None."""
    cfg = task.get('config') or {}
    policy = cfg.get(DELEGATE_CONFIG_KEY)
    if not isinstance(policy, dict):
        return None
    if not str(policy.get('policy') or '').strip():
        return None
    covered = policy.get('tools') or []
    if fn_name not in covered:
        return None
    return policy


# ═══════════════════════════════════════════════════════════
#  Adjudicator runtime — one bounded, tool-less child verdict
# ═══════════════════════════════════════════════════════════

def adjudicate_tool_call(task: dict, *, fn_name: str, fn_args: dict,
                         policy: dict, model: str,
                         approval_meta: dict | None = None) -> dict | None:
    """Judge one gated tool call against the registered policy.

    Returns ``{'approve': bool, 'reason': str}``, or None when the
    adjudicator is unavailable, times out, or produces no parseable verdict
    (the caller then falls through to the human gate). Never raises.
    """
    try:
        content = _run_adjudicator(
            task, fn_name=fn_name, fn_args=fn_args, policy=policy,
            model=model, approval_meta=approval_meta)
    except Exception:
        logger.warning('[ApprovalDelegate] adjudicator invocation failed',
                       exc_info=True)
        return None
    verdict = parse_adjudicator_verdict(content)
    if verdict is None:
        logger.info('[ApprovalDelegate] no parseable verdict for %s — '
                    'falling back to human gate', fn_name)
    return verdict


def _run_adjudicator(task: dict, *, fn_name: str, fn_args: dict,
                     policy: dict, model: str,
                     approval_meta: dict | None = None) -> str:
    """Run the bounded adjudicator child and return its raw content.

    Isolated so tests can substitute a canned verdict without an LLM.
    """
    from lib.swarm.registry import resolve_model_for_tier
    from lib.tasks_pkg.child_invocation import (
        create_child_invocation, run_child_invocation)
    from lib.tasks_pkg.manager import task_user_id

    tier = str(policy.get('model_tier') or 'light')
    owner_user_id = task_user_id(task)
    adjudicator_model = resolve_model_for_tier(
        tier, model, role='approval_adjudicator',
        provider_id=(task.get('_pinned_provider_id') or ''),
        owner_user_id=owner_user_id,
        tenant_id=task.get('_tenant_id')) or model

    meta = approval_meta if isinstance(approval_meta, dict) else \
        _risk_meta_for(fn_name, fn_args)
    try:
        args_text = json.dumps(fn_args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        args_text = str(fn_args)
    if len(args_text) > _ARGS_PREVIEW_CHARS:
        args_text = args_text[:_ARGS_PREVIEW_CHARS] + '…[truncated]'
    try:
        meta_text = json.dumps(meta, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        meta_text = str(meta)

    objective = (
        'Decide whether the following tool call may execute under the '
        'delegation policy below.\n\n'
        f'## Delegation policy\n{policy["policy"]}\n\n'
        f'## Tool call\nname: {fn_name}\narguments (JSON):\n{args_text}\n\n'
        f'## Risk metadata\n{meta_text}\n\n'
        'Approve ONLY when the policy clearly permits this exact call. When '
        'uncertain, or when the policy is silent on this case, deny — a '
        'wrong approval is far worse than a wrong denial. Answer with '
        'EXACTLY one JSON object on the final line, no markdown fences, no '
        'other JSON: {"approve": true|false, "reason": "<=200 chars"}')

    child = create_child_invocation(
        task, model=adjudicator_model,
        tools=[],  # No authority: the adjudicator can only talk.
        objective=objective,
        role_policy=(
            'You are an approval adjudicator. You have no tools and must '
            'not attempt any tool call. Judge conservatively against the '
            'supplied policy and nothing else.'),
        project_path=str((task.get('config') or {}).get('projectPath') or ''),
        thinking_enabled=False,
        event_sink=lambda _event: None,  # Verdicts are audit state, not UI.
        abort_check=lambda: bool(task.get('aborted')),
        timeout_seconds=float(policy.get('timeout_seconds')
                              or _DEFAULT_TIMEOUT_SECONDS),
        max_429_attempts=1)
    run_child_invocation(child)
    if child.get('_invocationStopReason') == 'deadline':
        logger.info('[ApprovalDelegate] adjudicator deadline for %s', fn_name)
        return ''
    return str(child.get('content') or '')


def _risk_meta_for(fn_name: str, fn_args: dict) -> dict:
    """Reuse the human-gate enrichers so the adjudicator sees the same risk."""
    meta: dict[str, Any] = {
        'toolName': fn_name,
        'path': fn_args.get('path', ''),
        'description': fn_args.get('description', ''),
    }
    try:
        from lib.tasks_pkg.tool_dispatch._approval import (
            _APPROVAL_META_ENRICHERS)
        enricher = _APPROVAL_META_ENRICHERS.get(fn_name)
        if enricher is not None:
            enricher(meta, fn_args)
    except Exception as exc:
        logger.debug('[ApprovalDelegate] risk enricher skipped: %s', exc)
    return meta


def parse_adjudicator_verdict(content: str) -> dict | None:
    """Extract the final JSON verdict; conservative on any ambiguity.

    Accepts exactly one object with a boolean ``approve``. Anything else —
    missing, malformed, multi-object, non-boolean — is undecided (None).
    """
    text = str(content or '')
    verdict: dict | None = None
    decoder = json.JSONDecoder()
    for pos in range(len(text)):
        if text[pos] != '{':
            continue
        try:
            candidate, end = decoder.raw_decode(text[pos:])
        except ValueError:
            continue
        if isinstance(candidate, dict) and 'approve' in candidate:
            verdict = candidate
            _ = end  # last candidate wins; keep scanning for the final one
    if verdict is None or not isinstance(verdict.get('approve'), bool):
        return None
    reason = str(verdict.get('reason') or '').strip()[:_REASON_MAX_CHARS]
    return {'approve': verdict['approve'],
            'reason': reason or ('approved by policy'
                                 if verdict['approve'] else 'denied by policy')}
