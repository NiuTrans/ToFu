# HOT_PATH
"""Pre/Post Tool Execution Hooks — extensible tool lifecycle.

Inspired by Claude Code's ``toolHooks.ts`` (620 lines) with its full
PreToolUse → execution → PostToolUse lifecycle.

Why we CAN'T replicate Claude Code's full hook system:
  - Claude Code hooks are user-configurable shell scripts executed before/after
    each tool invocation.  Our server-side architecture doesn't have a way for
    the user to register hook scripts at runtime.
  - Claude Code's PreToolUse can modify tool input and block execution.
    Our approval system (request_write_approval in tool_dispatch.py) already
    handles the "block" case.  Input modification would require a different
    dispatch architecture.
  - Claude Code's PostToolUse hooks can modify MCP tool output.  We don't
    have MCP integration.

What we CAN implement:
  1. Pre-tool hooks: logging, context injection, custom validation
  2. Post-tool hooks: result modification, follow-up context injection
  3. Both are registered programmatically (not user-configurable scripts)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Hook Types
# ═══════════════════════════════════════════════════════════════════════════════

# Pre-tool hook: (tool_name, args, task) → HookResult | None
# Return None to allow, or HookResult to block/modify
PreToolHook = Callable[[str, dict, dict], Optional['HookResult']]

# Post-tool hook: (tool_name, args, result_content, task) → str | None
# Return None to keep result unchanged, or str to replace result
PostToolHook = Callable[[str, dict, str, dict], str | None]


class HookResult:
    """Result from a pre-tool hook."""
    __slots__ = ('action', 'message', 'modified_args', 'additional_context')

    def __init__(self, action: str = 'allow', message: str = '',
                 modified_args: dict | None = None,
                 additional_context: str = ''):
        self.action = action          # 'allow', 'block', 'modify'
        self.message = message        # Reason for blocking
        self.modified_args = modified_args  # Modified args (for 'modify')
        self.additional_context = additional_context  # Extra context for model


# ═══════════════════════════════════════════════════════════════════════════════
#  Hook Registry
# ═══════════════════════════════════════════════════════════════════════════════

_pre_hooks: list[PreToolHook] = []
_post_hooks: list[PostToolHook] = []


def register_pre_hook(hook: PreToolHook):
    """Register a pre-tool execution hook."""
    _pre_hooks.append(hook)
    logger.debug('[Hooks] Registered pre-tool hook: %s', hook.__name__)


def register_post_hook(hook: PostToolHook):
    """Register a post-tool execution hook."""
    _post_hooks.append(hook)
    logger.debug('[Hooks] Registered post-tool hook: %s', hook.__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Hook execution
# ═══════════════════════════════════════════════════════════════════════════════

def run_pre_hooks(tool_name: str, args: dict, task: dict) -> HookResult | None:
    """Run all pre-tool hooks.  Returns first blocking/modifying result, or None."""
    for hook in _pre_hooks:
        try:
            result = hook(tool_name, args, task)
            if result and result.action in ('block', 'modify'):
                logger.info('[Hooks] Pre-hook %s returned %s for %s: %s',
                            hook.__name__, result.action, tool_name, result.message)
                return result
        except Exception as e:
            logger.warning('[Hooks] Pre-hook %s failed for %s: %s',
                           hook.__name__, tool_name, e, exc_info=True)
    return None


def run_post_hooks(tool_name: str, args: dict, result_content: str,
                   task: dict) -> str:
    """Run all post-tool hooks.  Returns (possibly modified) result content."""
    for hook in _post_hooks:
        try:
            modified = hook(tool_name, args, result_content, task)
            if modified is not None:
                logger.debug('[Hooks] Post-hook %s modified result for %s (%d → %d chars)',
                             hook.__name__, tool_name, len(result_content), len(modified))
                result_content = modified
        except Exception as e:
            logger.warning('[Hooks] Post-hook %s failed for %s: %s',
                           hook.__name__, tool_name, e, exc_info=True)
    return result_content


# ═══════════════════════════════════════════════════════════════════════════════
#  Built-in hooks
# ═══════════════════════════════════════════════════════════════════════════════

def _empty_result_marker_hook(tool_name: str, args: dict, result: str,
                               task: dict) -> str | None:
    """Replace empty tool results with a marker.

    Inspired by Claude Code's empty result handling which prevents model
    stop sequence issues when a tool returns nothing.
    """
    if not result or not result.strip():
        return f'({tool_name} completed with no output)'
    return None


def _run_command_safety_hook(tool_name: str, args: dict,
                              task: dict) -> HookResult | None:
    """Safety check for dangerous run_command invocations.

    Blocks obvious destructive commands when running in project mode.
    """
    if tool_name != 'run_command':
        return None

    command = args.get('command', '')
    if not command:
        return None

    # Block obviously dangerous commands
    _DANGEROUS_PATTERNS = [
        'rm -rf /',
        'rm -rf ~',
        'rm -rf /*',
        'mkfs.',
        ':(){:|:&};:',  # fork bomb
        'dd if=/dev/zero of=/dev/',
        'chmod -R 777 /',
    ]
    cmd_lower = command.lower().strip()
    for pattern in _DANGEROUS_PATTERNS:
        if pattern in cmd_lower:
            return HookResult(
                action='block',
                message=f'Blocked dangerous command pattern: {pattern}',
            )

    return None


# Register built-in hooks
register_post_hook(_empty_result_marker_hook)
register_pre_hook(_run_command_safety_hook)
