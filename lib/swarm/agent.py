"""lib/swarm/agent.py — SubAgent: isolated execution environment for a single worker.

Each SubAgent is a self-contained unit that:
  1. Gets a SubTaskSpec (objective, context, role, tools)
  2. Runs an LLM loop with tool access
  3. Returns a SubAgentResult

Key design principles:
  • Isolation: each agent has its own message history
  • Unbounded by default: runs until task completion (timeout/abort as safety nets)
  • Observable: structured SwarmEvent events emitted for UI progress tracking
  • Artifact-aware: can store/read shared artifacts via local handling
  • Early-stop: detects final answers and stops when done
  • Graceful degradation: timeout/abort returns partial results, not empty
"""

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from lib.llm_client import build_body as _default_build_body
from lib.llm_dispatch import dispatch_stream as _default_dispatch_stream
from lib.log import get_logger
from lib.protocols import BodyBuilder
from lib.swarm.protocol import (
    ArtifactStore,
    SubAgentResult,
    SubAgentStatus,
    SubTaskSpec,
    SwarmEvent,
    SwarmEventType,
    compress_result,
)
from lib.swarm.registry import (
    get_role_model_hint,
    get_role_system_suffix,
    resolve_model_for_tier,
    scope_tools_for_role,
)
from lib.swarm.tools import ARTIFACT_TOOLS

logger = get_logger(__name__)

# ─────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────

# Default truncation limit for tool results (chars)
DEFAULT_TOOL_RESULT_MAX_CHARS = 30_000

# Max parallel tool calls per round
MAX_PARALLEL_TOOLS = int(os.environ.get('TOOL_MAX_PARALLEL_WORKERS', '16'))

# Patterns that suggest the agent has reached a final answer
# (used for early-stop detection on content without tool calls)
_DONE_PATTERNS = re.compile(
    r'(?:^|\n)\s*(?:'
    r'(?:in\s+(?:summary|conclusion))'
    r'|(?:final\s+answer)'
    r'|(?:to\s+summarize)'
    r'|(?:here\s+(?:is|are)\s+(?:the|my)\s+(?:final|complete))'
    r'|(?:task\s+(?:complete|done|finished))'
    r')',
    re.IGNORECASE,
)


class SubAgent:
    """An isolated worker agent that executes a single subtask.

    Usage:
        spec = SubTaskSpec(role='researcher', objective='Find ...')
        agent = SubAgent(spec, parent_task=task, all_tools=tool_list)
        result = agent.run()  # blocking
    """

    def __init__(self, spec: SubTaskSpec, *,
                 parent_task: dict,
                 all_tools: list,
                 system_prompt_base: str = '',
                 model: str = '',
                 thinking_enabled: bool = True,
                 on_event: Callable | None = None,
                 abort_check: Callable | None = None,
                 project_path: str = '',
                 artifact_store: ArtifactStore | None = None,
                 tool_result_max_chars: int = DEFAULT_TOOL_RESULT_MAX_CHARS,
                 build_body_fn: BodyBuilder | None = None,
                 dispatch_stream_fn: Callable | None = None):
        self.spec = spec
        self.parent_task = parent_task
        self.agent_id = f'agent-{spec.role}-{spec.id}'
        self.model = self._resolve_model(spec, model)
        self.thinking_enabled = thinking_enabled
        self.on_event = on_event
        self.abort_check = abort_check or (lambda: False)
        self.project_path = project_path
        self.artifact_store = artifact_store  # shared across agents
        self.tool_result_max_chars = tool_result_max_chars
        # DI: allow injecting custom build_body / dispatch_stream (e.g. mocks in tests)
        self._build_body = build_body_fn or _default_build_body
        self._dispatch_stream = dispatch_stream_fn or _default_dispatch_stream

        # Build scoped tool list
        self.tools = scope_tools_for_role(spec.role, all_tools)
        # Inject artifact tools if an artifact store is available
        if self.artifact_store is not None:
            self._inject_artifact_tools()

        self.max_rounds = spec.max_rounds  # 0 = unlimited

        # Result tracking
        self.result = SubAgentResult()

        # Internal message history
        self.messages = self._build_initial_messages(system_prompt_base)

        # Cleanup state
        self._started = False
        self._cleaned_up = False

        # ── Debug: log agent initialization details ──
        tool_names = []
        for t in self.tools:
            if isinstance(t, dict):
                tool_names.append(t.get('function', {}).get('name', '?'))
        logger.debug(
            '[%s] Initialized: model=%s, max_rounds=%d, thinking=%s, '
            'tools=[%s%s] (%d total), depends_on=%s, objective="%s"',
            self.agent_id, self.model, self.max_rounds, self.thinking_enabled,
            ', '.join(tool_names[:10]), '...' if len(tool_names) > 10 else '',
            len(tool_names), list(spec.depends_on or []), spec.objective[:100]
        )

    # ─────────────────────────────────────────────────
    #  Model resolution
    # ─────────────────────────────────────────────────

    def _resolve_model(self, spec: SubTaskSpec, default_model: str) -> str:
        """Resolve the model for this agent based on spec override → role hint → default."""
        if spec.model_override:
            return spec.model_override
        tier = get_role_model_hint(spec.role)
        return resolve_model_for_tier(tier, default_model)

    # ─────────────────────────────────────────────────
    #  Artifact tool injection
    # ─────────────────────────────────────────────────

    def _inject_artifact_tools(self):
        """Add artifact tools to the agent's tool list if not already present."""
        existing_names = set()
        for t in self.tools:
            if isinstance(t, dict):
                name = t.get('function', {}).get('name', '')
                existing_names.add(name)
        for at in ARTIFACT_TOOLS:
            name = at.get('function', {}).get('name', '')
            if name not in existing_names:
                self.tools.append(at)

    # ─────────────────────────────────────────────────
    #  Message construction
    # ─────────────────────────────────────────────────

    def _build_initial_messages(self, system_prompt_base: str) -> list:
        """Construct the initial messages for this agent."""
        role_suffix = get_role_system_suffix(self.spec.role)

        system_content = self._strip_parent_prompt(system_prompt_base)
        if role_suffix:
            system_content += f'\n\n{role_suffix}'

        # Agent identity and constraints
        system_content += (
            f'\n\nYou are sub-agent [{self.agent_id}] working on a specific subtask. '
            f'Focus exclusively on your objective. Do NOT attempt tasks outside your scope. '
            f'When your task is complete, provide a clear final answer. '
            f'Do NOT call tools after you have gathered sufficient information — '
            f'write your final answer directly.'
        )

        # Artifact store instructions
        if self.artifact_store is not None:
            existing_artifacts = self.artifact_store.list_keys()
            system_content += (
                '\n\n## Shared Artifact Store\n'
                'You have access to a shared artifact store for inter-agent data sharing:\n'
                '  • store_artifact(key, content) — save data for other agents\n'
                '  • read_artifact(key) — read data saved by previous agents\n'
                '  • list_artifacts() — see all available artifacts\n'
                'Use this to share important findings or data that other agents might need.'
            )
            if existing_artifacts:
                system_content += (
                    f'\n\nAvailable artifacts from previous agents: {", ".join(existing_artifacts)}'
                )

        messages = [{'role': 'system', 'content': system_content}]

        # User message with objective + context
        user_parts = [f'## Your Task\n{self.spec.objective}']
        if self.spec.context:
            user_parts.append(f'\n## Context\n{self.spec.context}')
        messages.append({'role': 'user', 'content': '\n'.join(user_parts)})

        return messages

    def _strip_parent_prompt(self, prompt: str) -> str:
        """Remove parent-specific instructions that don't apply to sub-agents."""
        if not prompt:
            return ''
        kept = []
        skip_section = False
        for line in prompt.split('\n'):
            lower = line.lower().strip()
            if any(kw in lower for kw in ['spawn_agents', 'swarm mode',
                                           'check_agents', 'spawn_more']):
                skip_section = True
                continue
            if skip_section and line.strip() == '':
                skip_section = False
                continue
            if not skip_section:
                kept.append(line)
            if len('\n'.join(kept)) > 4000:
                break
        return '\n'.join(kept)

    # ─────────────────────────────────────────────────
    #  Event emission (structured SwarmEvent)
    # ─────────────────────────────────────────────────

    def _emit_event(self, event_type: str, text: str, **extra):
        """Emit a structured SwarmEvent to the parent.

        Creates a proper SwarmEvent and calls on_event with both the
        structured object (via to_dict()) and backward-compatible format
        (via to_legacy()).

        Args:
            event_type: One of SwarmEventType values or custom string.
            text: Human-readable description.
            **extra: Additional fields for SwarmEvent.metadata.
        """
        if not self.on_event:
            return

        try:
            evt = SwarmEvent(
                type=event_type,
                text=text,
                agent_id=self.spec.id,       # Use spec.id — consistent with scheduler callbacks
                role=self.spec.role,
                phase=extra.pop('phase', ''),
                status=extra.pop('status', ''),
                duration_s=extra.pop('duration_s', 0.0),
                tokens=extra.pop('tokens', 0),
                round_num=extra.pop('round_num', 0),
                metadata=extra,  # remaining kwargs go into metadata
            )
            # Emit legacy format for backward compatibility
            self.on_event(evt.to_legacy())
        except Exception as e:
            logger.warning('[SubAgent] Event emission error (non-fatal): %s', e, exc_info=True)

    # ─────────────────────────────────────────────────
    #  Execution
    # ─────────────────────────────────────────────────

    def run(self) -> SubAgentResult:
        """Execute the sub-agent synchronously. Returns SubAgentResult."""
        start_time = time.time()
        self._started = True
        self.result.status = SubAgentStatus.RUNNING.value

        # ★ Per-client browser routing: set thread-local for this sub-agent thread
        _browser_cid = self.parent_task.get('config', {}).get('browserClientId')
        if _browser_cid:
            from lib.browser import _set_active_client
            _set_active_client(_browser_cid)

        logger.info('[Agent:%s] ========== RUN START ==========', self.agent_id)
        logger.info('[Agent:%s] role=%s model=%s max_rounds=%d tools=%s',
                     self.agent_id, self.spec.role, self.model, self.max_rounds,
                     [t.get('function', {}).get('name', '?') for t in (self.tools or [])])
        logger.info('[Agent:%s] objective: %s', self.agent_id, self.spec.objective[:200])
        logger.debug('[Agent:%s] context: %s', self.agent_id, (self.spec.context or '')[:300])

        # NOTE: Do NOT emit AGENT_START here — the scheduler's
        # _on_agent_start_callback already fires a 'running' phase event.
        # Emitting again here would regress the phase from 'running' → 'starting'
        # and (if IDs ever mismatch) create duplicate frontend cards.

        try:
            self._run_loop(start_time)
        except Exception as e:
            self.result.status = SubAgentStatus.FAILED.value
            self.result.error_message = f'{type(e).__name__}: {e}'
            logger.error('[%s] Failed: %s', self.agent_id, e, exc_info=True)
            self._emit_event(
                SwarmEventType.AGENT_FAILED.value,
                f'❌ [{self.spec.role}] Failed: {str(e)[:200]}',
                status='failed', error=str(e)[:200],
            )

        self.result.elapsed_seconds = time.time() - start_time

        logger.info('[Agent:%s] ========== RUN END ==========', self.agent_id)
        logger.info('[Agent:%s] status=%s elapsed=%.1fs rounds=%d tokens=%d answer_len=%d',
                     self.agent_id, self.result.status,
                     self.result.elapsed_seconds, self.result.rounds_used,
                     self.result.total_tokens,
                     len(self.result.final_answer or ''))

        # Finalize status
        if self.result.status == SubAgentStatus.RUNNING.value:
            # Still "running" means we fell through without explicit completion
            if self.result.final_answer:
                self.result.status = SubAgentStatus.COMPLETED.value
            else:
                self.result.status = SubAgentStatus.FAILED.value
                self.result.error_message = self.result.error_message or 'No final answer produced'

        # Cleanup
        self._cleanup()

        # NOTE: Do NOT emit AGENT_COMPLETE here — the MasterOrchestrator's
        # on_agent_complete callback already emits swarm_agent_complete with
        # richer data (elapsed, tokens, preview).  Emitting here too would
        # cause the frontend to process TWO completion events per agent.

        return self.result

    def _run_loop(self, start_time: float):
        """Core agent loop: LLM call → tool execution → repeat.

        Runs until the agent produces a final answer (content without tool
        calls).  Safety nets: timeout and abort checks each round.
        If max_rounds > 0, also stops after that many rounds.
        """
        timeout_seconds = getattr(self.spec, 'timeout_seconds', None)

        round_num = 0
        while True:
            round_num += 1

            # ── Max rounds check (0 = unlimited) ──
            if self.max_rounds and round_num > self.max_rounds:
                logger.info('[%s] Exhausted %d rounds', self.agent_id, self.max_rounds)
                self._extract_partial_answer(f'Max rounds ({self.max_rounds}) reached')
                self.result.status = SubAgentStatus.COMPLETED.value
                return

            # ── Abort check ──
            if self.abort_check():
                logger.info('[%s] Aborted at round %d', self.agent_id, round_num)
                self.result.status = SubAgentStatus.CANCELLED.value
                self._extract_partial_answer(f'Agent cancelled at round {round_num}')
                return

            # ── Timeout check ──
            if timeout_seconds and (time.time() - start_time) > timeout_seconds:
                logger.warning(
                    '[%s] Timeout after %ss at round %d',
                    self.agent_id, timeout_seconds, round_num
                )
                self.result.status = SubAgentStatus.COMPLETED.value
                self._extract_partial_answer(
                    f'Agent timed out after {timeout_seconds}s (completed {round_num - 1} rounds)'
                )
                self._emit_event(
                    'timeout',
                    f'⏰ [{self.spec.role}] Timed out after {timeout_seconds}s',
                    status='timeout', phase='timeout',
                    round_num=round_num,
                )
                return

            self.result.rounds_used = round_num
            round_start = time.time()

            logger.debug('[Agent:%s] ── Round %d/%d START ── messages=%d',
                         self.agent_id, round_num, self.max_rounds, len(self.messages))

            # ── LLM call (uses DI-injected or default build_body / dispatch_stream) ──
            body = self._build_body(
                model=self.model,
                messages=self.messages,
                tools=self.tools if self.tools else None,
                max_tokens=64000,
                thinking_enabled=self.thinking_enabled,
                temperature=1.0,
            )

            content_parts = []
            thinking_parts = []

            def on_content(chunk):
                content_parts.append(chunk)

            def on_thinking(chunk):
                thinking_parts.append(chunk)

            try:
                msg, stop_reason, usage = self._dispatch_stream(
                    body,
                    on_content=on_content,
                    on_thinking=on_thinking,
                    abort_check=self.abort_check,
                    prefer_model=body.get('model', ''),
                    log_prefix=f'[{self.agent_id}]',
                )
            except Exception as e:
                logger.error('[%s] LLM call failed round %d: %s', self.agent_id, round_num, e, exc_info=True)
                # On LLM error, try to extract partial answer from previous rounds
                self.result.error_message = f'LLM call failed at round {round_num}: {e}'
                self._extract_partial_answer(f'LLM error at round {round_num}')
                if self.result.final_answer and round_num > 1:
                    # We have partial results — mark completed with caveat
                    self.result.status = SubAgentStatus.COMPLETED.value
                else:
                    self.result.status = SubAgentStatus.FAILED.value
                return

            # Track token usage
            round_elapsed = time.time() - round_start
            if usage:
                self.result.prompt_tokens += usage.get('prompt_tokens', 0)
                self.result.completion_tokens += usage.get('completion_tokens', 0)
                self.result.total_tokens += usage.get('total_tokens', 0)
            logger.debug('[Agent:%s] Round %d LLM done in %.1fs — stop=%s usage=%s content_len=%d',
                         self.agent_id, round_num, round_elapsed, stop_reason,
                         usage, len(''.join(content_parts)))

            # Save thinking for trace
            if thinking_parts:
                self.result.reasoning_trace += (
                    f'\n--- Round {round_num} ---\n' +
                    ''.join(thinking_parts)[:2000]
                )

            # Append assistant message
            self.messages.append(msg)

            # ── Check for tool calls ──
            tool_calls = msg.get('tool_calls', [])

            if not tool_calls:
                # No tool calls → agent has produced a text response
                content = msg.get('content', ''.join(content_parts))
                self.result.final_answer = content
                self.result.status = SubAgentStatus.COMPLETED.value
                logger.debug('[Agent:%s] Round %d: FINAL ANSWER produced (len=%d)',
                             self.agent_id, round_num, len(content or ''))

                self._emit_event(
                    'progress',
                    f'📝 [{self.spec.role}] Round {round_num}: produced final answer',
                    status='running', phase='done',
                    round_num=round_num,
                    preview=(content or '')[:200],
                )
                return  # ← Early stop: agent gave final answer

            # ── Execute tool calls ──
            tool_names = []
            for tc in tool_calls:
                fn = tc.get('function', {})
                tool_names.append(fn.get('name', '?'))

            logger.debug('[Agent:%s] Round %d: %d tool call(s) → %s',
                         self.agent_id, round_num, len(tool_calls), tool_names)

            self._emit_event(
                'progress',
                f'🔧 [{self.spec.role}] Round {round_num}: '
                f'{len(tool_calls)} tool call(s): '
                f'{", ".join(tool_names[:5])}'
                f'{"..." if len(tool_names) > 5 else ""}',
                status='running', phase='tool_use',
                round_num=round_num,
                toolNames=tool_names,
            )

            self._execute_tool_calls(tool_calls, round_num)

            # ── Post-tool-execution abort check ──
            if self.abort_check():
                logger.info('[%s] Aborted after tools in round %d', self.agent_id, round_num)
                self.result.status = SubAgentStatus.CANCELLED.value
                self._extract_partial_answer(f'Agent cancelled after tools in round {round_num}')
                return

    # ─────────────────────────────────────────────────
    #  Answer extraction helpers
    # ─────────────────────────────────────────────────

    def _extract_partial_answer(self, reason: str = ''):
        """Extract the best available answer from message history.

        Scans backwards through messages looking for the last substantive
        assistant content. Sets result.final_answer if found.
        """
        if self.result.final_answer:
            return  # Already have an answer

        # Walk backwards through messages for assistant content
        for msg in reversed(self.messages):
            if msg.get('role') == 'assistant' and msg.get('content'):
                content = msg['content'].strip()
                if len(content) > 20:  # Skip trivial responses
                    prefix = f'[Partial — {reason}]\n\n' if reason else ''
                    self.result.final_answer = prefix + content
                    return

        # If nothing found, note the reason
        if reason:
            self.result.final_answer = f'[{reason}] No substantive answer was produced.'

    # ─────────────────────────────────────────────────
    #  Tool execution
    # ─────────────────────────────────────────────────

    def _execute_tool_calls(self, tool_calls: list, round_num: int):
        """Execute one or more tool calls, potentially in parallel."""

        if len(tool_calls) == 1:
            # Single tool call — run directly (no thread overhead)
            tc = tool_calls[0]
            result = self._execute_single_tool(tc, round_num)
            self.messages.append({
                'role': 'tool',
                'tool_call_id': tc.get('id', str(uuid.uuid4())[:8]),
                'content': result,
            })
        else:
            # Multiple tool calls — run in parallel
            results = {}
            max_workers = min(len(tool_calls), MAX_PARALLEL_TOOLS)
            with ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=f'{self.agent_id}-tools',
            ) as pool:
                futures = {}
                for tc in tool_calls:
                    future = pool.submit(self._execute_single_tool, tc, round_num)
                    futures[future] = tc

                for future in as_completed(futures):
                    tc = futures[future]
                    tc_id = tc.get('id', str(uuid.uuid4())[:8])
                    try:
                        result = future.result(timeout=300)
                    except Exception as e:
                        fn_name = tc.get('function', {}).get('name', '?')
                        logger.warning(
                            '[%s] Tool %s raised in thread: %s',
                            self.agent_id, fn_name, e,
                            exc_info=True)

                        result = f'Tool execution error ({fn_name}): {type(e).__name__}: {e}'
                    results[tc_id] = result

            # Append results in original order (important for reproducibility)
            for tc in tool_calls:
                tc_id = tc.get('id', str(uuid.uuid4())[:8])
                self.messages.append({
                    'role': 'tool',
                    'tool_call_id': tc_id,
                    'content': results.get(tc_id, '(no result)'),
                })

    def _execute_single_tool(self, tool_call: dict, round_num: int) -> str:
        """Execute a single tool call and return the result string."""
        fn_info = tool_call.get('function', {})
        fn_name = fn_info.get('name', '?')
        fn_args_raw = fn_info.get('arguments', '{}')
        tool_start = time.time()

        logger.debug('[Agent:%s] Round %d → TOOL_CALL %s args_raw=%s',
                     self.agent_id, round_num, fn_name,
                     (fn_args_raw if isinstance(fn_args_raw, str) else str(fn_args_raw))[:300])

        try:
            fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else fn_args_raw
        except json.JSONDecodeError:
            # Attempt best-effort JSON repair
            try:
                from lib.utils import repair_json as _repair_json
                fn_args = _repair_json(fn_args_raw if isinstance(fn_args_raw, str) else '{}')
                logger.debug('[Agent:%s] Repaired malformed JSON for %s', self.agent_id, fn_name)
            except Exception as e:
                logger.warning('[%s] Invalid JSON args for %s: %s: %s', self.agent_id, fn_name, fn_args_raw[:100], e, exc_info=True)
                return f'Invalid JSON arguments for {fn_name}: {fn_args_raw[:200]}'

        if not isinstance(fn_args, dict):
            fn_args = {}

        # Log the tool call
        self.result.tool_log.append({
            'round': round_num,
            'tool': fn_name,
            'args_brief': str(fn_args)[:200],
            'timestamp': time.time(),
        })

        # ── Handle artifact tools locally ──
        if fn_name == 'store_artifact':
            return self._handle_store_artifact(fn_args)
        if fn_name == 'read_artifact':
            return self._handle_read_artifact(fn_args)
        if fn_name == 'list_artifacts':
            return self._handle_list_artifacts(fn_args)

        # ── Dispatch to real tools via executor ──
        try:
            logger.debug('[Agent:%s] Dispatching tool %s args=%s',
                         self.agent_id, fn_name, str(fn_args)[:200])
            result = self._dispatch_tool(tool_call, fn_name, fn_args, round_num)
            truncated = self._truncate_tool_result(result)
            tool_elapsed = time.time() - tool_start
            logger.debug('[Agent:%s] Tool %s completed in %.2fs result_len=%d',
                         self.agent_id, fn_name, tool_elapsed, len(truncated))
            logger.debug('[Agent:%s] Tool %s result preview: %s',
                         self.agent_id, fn_name, truncated[:300])
            return truncated
        except Exception as e:
            tool_elapsed = time.time() - tool_start
            logger.warning('[Agent:%s] Tool %s FAILED in %.2fs: %s',
                           self.agent_id, fn_name, tool_elapsed, e, exc_info=True)
            return f'Tool error ({fn_name}): {type(e).__name__}: {e}'

    # ─────────────────────────────────────────────────
    #  Artifact handling
    # ─────────────────────────────────────────────────

    def _handle_store_artifact(self, args: dict) -> str:
        """Handle the store_artifact tool call."""
        if self.artifact_store is None:
            logger.warning('[Agent:%s] store_artifact called but no artifact_store', self.agent_id)
            return 'Error: artifact store not available'
        key = args.get('key', '')
        content = args.get('content', '')
        if not key:
            return 'Error: "key" is required'
        if not content:
            return 'Error: "content" is required (cannot store empty artifact)'
        tags = args.get('tags', [])
        logger.debug('[Agent:%s] STORE_ARTIFACT key=%s len=%d tags=%s',
                      self.agent_id, key, len(content), tags)
        try:
            self.artifact_store.put(key, content, writer_id=self.agent_id, tags=tags)
        except Exception as e:
            logger.warning('[%s] artifact store put failed: %s', self.agent_id, e, exc_info=True)
            return f'Error storing artifact "{key}": {e}'
        self.result.artifacts_written.append(key)
        return f'Stored artifact "{key}" ({len(content):,} chars)'

    def _handle_read_artifact(self, args: dict) -> str:
        """Handle the read_artifact tool call."""
        if self.artifact_store is None:
            logger.warning('[Agent:%s] read_artifact called but no artifact_store', self.agent_id)
            return 'Error: artifact store not available'
        key = args.get('key', '')
        if not key:
            return 'Error: "key" is required'
        logger.debug('[Agent:%s] READ_ARTIFACT key=%s', self.agent_id, key)
        try:
            content = self.artifact_store.get(key)
        except Exception as e:
            logger.warning('[%s] artifact store get failed: %s', self.agent_id, e, exc_info=True)
            return f'Error reading artifact "{key}": {e}'
        if not content:
            available = self.artifact_store.list_keys()
            logger.debug('[Agent:%s] READ_ARTIFACT key=%s NOT FOUND, available=%s',
                          self.agent_id, key, available)
            return f'Artifact "{key}" not found. Available: {", ".join(available) or "(none)"}'
        logger.debug('[Agent:%s] READ_ARTIFACT key=%s → OK len=%d',
                      self.agent_id, key, len(content))
        self.result.artifacts_read.append(key)
        return content

    def _handle_list_artifacts(self, args: dict = None) -> str:
        """Handle the list_artifacts tool call."""
        if self.artifact_store is None:
            return 'Error: artifact store not available'
        tag = (args or {}).get('tag', '')
        try:
            if tag:
                keys = self.artifact_store.list_keys(tag=tag)
                return f'Artifacts with tag "{tag}": {", ".join(keys) or "(none)"}'
            return self.artifact_store.summary()
        except Exception as e:
            logger.warning('[%s] artifact store list failed: %s', self.agent_id, e, exc_info=True)
            return f'Error listing artifacts: {e}'

    # ─────────────────────────────────────────────────
    #  Tool dispatch
    # ─────────────────────────────────────────────────

    def _dispatch_tool(self, tool_call: dict, fn_name: str, fn_args: dict,
                       round_num: int) -> str:
        """Execute a tool by name using the project tools executor.

        Delegates to ``_execute_tool_one`` from the executor module, which
        handles web_search, fetch_url, project tools, browser tools, etc.
        """
        from lib.tasks_pkg.executor import _execute_tool_one

        # Build a minimal task dict for tool execution
        task_proxy = {
            'id': self.parent_task.get('id', 'unknown'),
            'convId': self.parent_task.get('convId', 'unknown'),
            'status': 'running',
            'events': self.parent_task.get('events', []),
            'events_lock': self.parent_task.get('events_lock', threading.Lock()),
            'searchRounds': self.parent_task.get('searchRounds', []),
            'phase': self.parent_task.get('phase'),
        }

        tc_id = tool_call.get('id', str(uuid.uuid4())[:8])

        # Build a round_entry stub (executor expects this for side-effects)
        round_entry = {
            'roundNum': round_num,
            'query': f'{fn_name}({str(fn_args)[:60]})',
            'results': None,
            'status': 'searching',
            'toolName': fn_name,
        }

        # Config for the executor — include browserClientId for per-device routing
        cfg = {
            'model': self.model,
            'thinking_enabled': self.thinking_enabled,
            'search_mode': 'multi',
            'browserClientId': self.parent_task.get('config', {}).get('browserClientId'),
        }

        try:
            _, tool_content, _ = _execute_tool_one(
                task_proxy, tool_call, fn_name, tc_id, fn_args,
                round_num, round_entry, cfg,
                self.project_path, bool(self.project_path),
            )
            if isinstance(tool_content, dict):
                # Some tools (e.g. browser_screenshot) return dicts
                return json.dumps(tool_content, ensure_ascii=False)
            return str(tool_content) if tool_content is not None else ''
        except Exception as e:
            logger.error('[%s] Tool dispatch error for %s: %s', self.agent_id, fn_name, e, exc_info=True)
            return f'Error executing {fn_name}: {type(e).__name__}: {e}'

    # ─────────────────────────────────────────────────
    #  Result truncation
    # ─────────────────────────────────────────────────

    def _truncate_tool_result(self, result: str, max_chars: int = None) -> str:
        """Truncate tool results to avoid blowing up the sub-agent's context.

        Strategy:
          • If result fits within limit, return as-is.
          • Otherwise, keep the first ~70% and last ~15% of the limit,
            with a clear truncation marker in between.
          • The truncation marker tells the model the full size so it can
            decide whether to make a more specific query.

        Args:
            result: Raw tool output string.
            max_chars: Override for self.tool_result_max_chars.
        """
        if not result:
            return result or ''

        limit = max_chars if max_chars is not None else self.tool_result_max_chars
        if len(result) <= limit:
            return result

        # Preserve structure: head (70%) + truncation notice + tail (15%)
        head_size = int(limit * 0.70)
        tail_size = int(limit * 0.15)
        # Ensure we don't overshoot
        marker = (
            f'\n\n... [TRUNCATED: showing {head_size:,} + {tail_size:,} of '
            f'{len(result):,} total chars. Use more specific queries to narrow results.]\n\n'
        )
        available = limit - len(marker)
        if available < 100:
            # Very small limit — just hard truncate
            return result[:limit]

        head_size = int(available * 0.82)
        tail_size = available - head_size

        return result[:head_size] + marker + result[-tail_size:]

    # ─────────────────────────────────────────────────
    #  Cleanup
    # ─────────────────────────────────────────────────

    def _cleanup(self):
        """Release resources after agent execution.

        Called automatically at the end of run(). Safe to call multiple times.
        Truncates the message history to free memory while keeping the
        result intact for the caller.
        """
        if self._cleaned_up:
            return
        self._cleaned_up = True

        # Compact message history — keep only system, first user, and last 2 messages
        # This frees memory from potentially large tool results
        if len(self.messages) > 6:
            # Keep: system prompt + first user msg + last 2 messages
            self.messages = self.messages[:2] + self.messages[-2:]

        logger.info(
            '[Agent:%s] Cleanup complete — status=%s answer_len=%d tokens=%d rounds=%d artifacts_w=%s artifacts_r=%s',
            self.agent_id, self.result.status,
            len(self.result.final_answer or ''),
            self.result.total_tokens,
            self.result.rounds_used,
            self.result.artifacts_written,
            self.result.artifacts_read,
        )
