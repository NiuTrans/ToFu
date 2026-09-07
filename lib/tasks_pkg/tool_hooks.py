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
from typing import Optional

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

# UserPromptSubmit hook: (prompt_text, task) → str | None
# Return None to keep prompt unchanged, or str to replace it.
# Inspired by Claude Agent SDK's UserPromptSubmit hook — fires once per
# user turn, BEFORE the prompt enters the agent loop.  Use cases: PII
# redaction, prompt rewriting, safety filters, telemetry.
UserPromptSubmitHook = Callable[[str, dict], Optional[str]]

# PreCompact hook: (messages, task) → None
# Fires before compaction modifies `messages` in place.  Hooks may
# inspect / archive the transcript but MUST NOT mutate `messages` —
# compaction is on the hot path and we don't want hook-induced
# transcript corruption.  Use cases: external archival, transcript
# snapshotting, audit trail.  Inspired by Claude Agent SDK's PreCompact.
PreCompactHook = Callable[[list, dict], None]

# PostCompact hook: (info, task) → None
# Fires AFTER a successful compaction replaced the transcript.  ``info``
# is a read-only summary dict (layer / trigger / tokens_before /
# tokens_after / messages_before / messages_after / round) — hooks never
# see the mutated message list itself, so a hook cannot corrupt the
# freshly compacted transcript.  Mirrors the post-compact lifecycle hook
# in Codex's compact.rs.  Use cases: analytics, cache-invalidation,
# downstream UI notification.
PostCompactHook = Callable[[dict, dict], None]


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
_user_prompt_hooks: list[UserPromptSubmitHook] = []
_pre_compact_hooks: list[PreCompactHook] = []
_post_compact_hooks: list[PostCompactHook] = []


def register_pre_hook(hook: PreToolHook):
    """Register a pre-tool execution hook."""
    _pre_hooks.append(hook)
    logger.debug('[Hooks] Registered pre-tool hook: %s', hook.__name__)


def register_post_hook(hook: PostToolHook):
    """Register a post-tool execution hook."""
    _post_hooks.append(hook)
    logger.debug('[Hooks] Registered post-tool hook: %s', hook.__name__)


def register_user_prompt_hook(hook: UserPromptSubmitHook):
    """Register a UserPromptSubmit hook (fires once per user turn)."""
    _user_prompt_hooks.append(hook)
    logger.debug('[Hooks] Registered user-prompt hook: %s', hook.__name__)


def register_pre_compact_hook(hook: PreCompactHook):
    """Register a PreCompact hook (fires before compaction modifies messages)."""
    _pre_compact_hooks.append(hook)
    logger.debug('[Hooks] Registered pre-compact hook: %s', hook.__name__)

def register_post_compact_hook(hook: PostCompactHook):
    """Register a PostCompact hook (fires after a successful compaction)."""
    _post_compact_hooks.append(hook)
    logger.debug('[Hooks] Registered post-compact hook: %s', hook.__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Hook execution
# ═══════════════════════════════════════════════════════════════════════════════

def run_pre_hooks(tool_name: str, args: dict, task: dict) -> HookResult | None:
    """Run all pre-tool hooks.

    Returns the first blocking result encountered (action='block').  For
    'modify' results, the args dict is mutated IN PLACE so every downstream
    consumer (parallel pool, serial dispatch, executor) sees the rewrite.
    Returns None if no hook blocks.
    """
    for hook in _pre_hooks:
        try:
            result = hook(tool_name, args, task)
        except Exception as e:
            logger.warning('[Hooks] Pre-hook %s failed for %s: %s',
                           hook.__name__, tool_name, e, exc_info=True)
            continue
        if not result:
            continue
        if result.action == 'block':
            logger.info('[Hooks] Pre-hook %s BLOCKED %s: %s',
                        hook.__name__, tool_name, result.message)
            return result
        if result.action == 'modify' and isinstance(result.modified_args, dict):
            # In-place mutation so every downstream tuple-bound reference
            # sees the rewrite without us having to thread a new dict.
            _orig_keys = set(args.keys())
            args.clear()
            args.update(result.modified_args)
            _new_keys = set(args.keys())
            logger.info('[Hooks] Pre-hook %s MODIFIED %s args (added=%s removed=%s)',
                        hook.__name__, tool_name,
                        sorted(_new_keys - _orig_keys),
                        sorted(_orig_keys - _new_keys))
            # Don't return — let later hooks see the modified args.
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


def run_user_prompt_hooks(prompt: str, task: dict) -> str:
    """Run all UserPromptSubmit hooks.  Returns the (possibly rewritten) prompt.

    Hooks run sequentially; each sees the previous hook's output.  A hook
    that returns None passes the prompt through unchanged.  Exceptions are
    logged and swallowed so a buggy hook never breaks the agent loop.
    """
    if not _user_prompt_hooks:
        return prompt
    current = prompt
    for hook in _user_prompt_hooks:
        try:
            rewritten = hook(current, task)
            if rewritten is not None and rewritten != current:
                logger.info('[Hooks] UserPromptSubmit %s rewrote prompt (%d → %d chars)',
                            hook.__name__, len(current), len(rewritten))
                current = rewritten
        except Exception as e:
            logger.warning('[Hooks] UserPromptSubmit %s failed: %s',
                           hook.__name__, e, exc_info=True)
    return current


def run_pre_compact_hooks(messages: list, task: dict) -> None:
    """Run all PreCompact hooks BEFORE compaction modifies messages.

    Hooks should treat ``messages`` as read-only.  If a hook needs to keep
    a snapshot, it should ``copy.deepcopy()`` the list.  This entry point
    exists so external systems (audit log, archival, exporters) can capture
    the full transcript before lossy compaction destroys it.
    """
    if not _pre_compact_hooks:
        return
    for hook in _pre_compact_hooks:
        try:
            hook(messages, task)
        except Exception as e:
            logger.warning('[Hooks] PreCompact %s failed: %s',
                           hook.__name__, e, exc_info=True)


def run_post_compact_hooks(info: dict, task: dict) -> None:
    """Run all PostCompact hooks AFTER compaction replaced the transcript.

    ``info`` is the compaction summary dict built by the pipeline.  Hooks
    must treat it as read-only.  Exceptions are logged and swallowed so a
    buggy observer can never break the agent loop mid-turn.
    """
    if not _post_compact_hooks:
        return
    for hook in _post_compact_hooks:
        try:
            hook(info, task)
        except Exception as e:
            logger.warning('[Hooks] PostCompact %s failed: %s',
                           hook.__name__, e, exc_info=True)


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

    # ── Catastrophic delete: parse the actual rm/rmdir/unlink targets ──
    # A bare substring test ('rm -rf /' in cmd) false-positives on every
    # legitimate scoped delete whose absolute target merely STARTS with a
    # slash — e.g. ``rm -rf /tmp/tofu-ci-export`` contains the substring
    # ``rm -rf /``. That wrongly blocked a routine ``export.py`` step (conv
    # mrltpk1t43e0mw). Delegate to the argument-parsing depth/workspace guard
    # that run_command itself uses, so only a genuine root-ish delete (``/``,
    # ``/mnt``, ``~``, ``/*``) is refused while ``/tmp/scoped/path`` passes.
    try:
        from lib.project_mod.command_analysis import _is_catastrophic_delete
        _bad_target = _is_catastrophic_delete(command)
    except Exception as e:
        logger.debug('[Hooks] catastrophic-delete parse failed for %r: %s',
                     command[:80], e)
        _bad_target = None
    if _bad_target:
        return HookResult(
            action='block',
            message=f'Blocked catastrophic delete of {_bad_target}',
            additional_context=(
                'Scope the deletion to a specific subdirectory instead of a '
                'filesystem or home root — e.g. a build/output folder under the '
                'project, or a path under /tmp. Re-issue run_command with that '
                'narrower target (deleting a scoped subpath like '
                '/tmp/my-workdir is allowed). Do not retry the identical command.'
            ),
        )
    # The depth parser expands ``~`` to the (deep) real home path, so a bare
    # ``rm -rf ~`` / ``rm -rf $HOME`` passes rule 1 — but wiping the whole home
    # dir is never a legitimate agent action. Catch the bare home-root form
    # explicitly (a SCOPED ``rm -rf ~/old_build`` is left alone).
    import re as _re
    if _re.search(r'\b(?:rm|rmdir)\b[^|;&]*\s(?:~|\$HOME)(?:/\s|/?$|\s)',
                  ' ' + command.strip() + ' '):
        return HookResult(
            action='block',
            message='Blocked catastrophic delete of home root (~)',
            additional_context=(
                'Deleting the entire home directory is refused. Target a '
                'specific subpath instead (e.g. ~/some-scoped-dir) and retry.'
            ),
        )

    # ── Other hard-blocked structural patterns (non-delete) ──
    # These are STRUCTURE, not scoped paths, so a substring test is adequate
    # (and still tolerant of quoted mentions in benign diagnostics is out of
    # scope here — run_command's own dangerous-pattern guard masks quotes).
    _DANGEROUS_PATTERNS = [
        'mkfs.',
        ':(){:|:&};:',  # fork bomb
        'dd if=/dev/zero of=/dev/',
        'chmod -r 777 /',
    ]
    cmd_lower = command.lower().strip()
    for pattern in _DANGEROUS_PATTERNS:
        if pattern in cmd_lower:
            return HookResult(
                action='block',
                message=f'Blocked dangerous command pattern: {pattern}',
                additional_context=(
                    'This command is refused for safety. If you need the '
                    'underlying result, find a non-destructive alternative that '
                    'does not match this pattern, and do not retry the identical '
                    'command.'
                ),
            )

    return None


# Register built-in hooks
register_post_hook(_empty_result_marker_hook)
register_pre_hook(_run_command_safety_hook)


__all__ = [
    'HookResult',
    'PreToolHook', 'PostToolHook',
    'UserPromptSubmitHook', 'PreCompactHook', 'PostCompactHook',
    'register_pre_hook', 'register_post_hook',
    'register_user_prompt_hook', 'register_pre_compact_hook',
    'register_post_compact_hook',
    'run_pre_hooks', 'run_post_hooks',
    'run_user_prompt_hooks', 'run_pre_compact_hooks',
    'run_post_compact_hooks',
]
