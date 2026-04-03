"""lib/swarm/review.py — Master review logic for reactive swarm orchestration.

Extracted from master.py as a mixin class:
  • ReviewMixin._check_fast_path_eligible() — fast-path eligibility check
  • ReviewMixin._build_dashboard() — compact status table
  • ReviewMixin._build_review_prompt() — full review prompt
  • ReviewMixin._build_incremental_review_prompt() — incremental review prompt
  • ReviewMixin._master_review() — LLM review call for reactive loop

MasterOrchestrator inherits ReviewMixin to gain these methods.
"""

import json
import uuid

from lib.llm_client import build_body
from lib.llm_dispatch import dispatch_stream as _dispatch_stream
from lib.log import get_logger
from lib.swarm.protocol import (
    SubAgentResult,
    SubAgentStatus,
    SubTaskSpec,
    compress_result,
)
from lib.swarm.scheduler import StreamingScheduler

logger = get_logger(__name__)


class ReviewMixin:
    """Mixin providing master review methods for MasterOrchestrator.

    Expects the following attributes on ``self``:
      - fast_path_enabled: bool
      - _results: list[tuple[SubTaskSpec, SubAgentResult]]
      - artifact_store: ArtifactStore
      - all_tools: list
      - model: str
      - thinking_enabled: bool
      - thinking_depth: str | None
      - _aborted: bool
      - abort_check: Callable
      - on_progress: Callable | None
      - task_id: str
      - max_retries: int
    """

    # ── Error indicators for fast-path eligibility ───
    _FAST_PATH_ERROR_INDICATORS = (
        'error', 'failed', 'exception', 'traceback',
        'could not', 'unable to', 'timed out',
    )

    # ── Fast-path eligibility check ──────────────────

    def _check_fast_path_eligible(self, batch: list[tuple[SubTaskSpec, SubAgentResult]]) -> bool:
        """Check whether a batch of results qualifies for fast-path (skip review).

        A batch is eligible when **all** of the following are true:
          1. ``fast_path_enabled`` is True on this orchestrator.
          2. Every agent in *batch* has status == COMPLETED.
          3. None of the agents' ``final_answer`` texts contain
             error indicator substrings (case-insensitive).

        Returns True if review can be skipped.
        """
        if not self.fast_path_enabled:
            return False

        for spec, result in batch:
            if result.status != SubAgentStatus.COMPLETED.value:
                return False
            answer_lower = (result.final_answer or '').lower()
            for indicator in self._FAST_PATH_ERROR_INDICATORS:
                if indicator in answer_lower:
                    return False
        return True

    # ── Dashboard ────────────────────────────────────

    def _build_dashboard(self) -> str:
        """Build a compact status table of all agent results.

        Example output::

            | # | Role     | Status | Time  | Tokens | Key Finding              |
            |---|----------|--------|-------|--------|--------------------------|
            | 1 | coder    | ✅     | 4.2s  | 1,200  | Found bug in parser.py   |
            | 2 | analyst  | ✅     | 3.1s  | 800    | Data shows 20% growth    |
            | 3 | coder    | ❌     | 12.0s | 2,400  | Timeout on API call      |
        """
        if not self._results:
            return '(no agent results yet)'

        lines = [
            '| # | Role | Status | Time | Tokens | Key Finding |',
            '|---|------|--------|------|--------|-------------|',
        ]

        for i, (spec, result) in enumerate(self._results, 1):
            status = ('✅' if result.status == SubAgentStatus.COMPLETED.value
                      else '❌')
            elapsed = f'{result.elapsed_seconds:.1f}s'
            tokens = f'{result.total_tokens:,}'

            if result.status == SubAgentStatus.COMPLETED.value:
                answer = (result.final_answer or '').replace('\n', ' ').strip()
                key_finding = answer[:50] + ('…' if len(answer) > 50 else '')
            else:
                key_finding = (result.error_message or 'Failed')[:50]

            lines.append(
                f'| {i} | {spec.role[:10]} | {status} | '
                f'{elapsed} | {tokens} | {key_finding} |'
            )

        # Summary line
        total_tokens = sum(r.total_tokens for _, r in self._results)
        total_time = sum(r.elapsed_seconds for _, r in self._results)
        failed = sum(1 for _, r in self._results
                     if r.status == SubAgentStatus.FAILED.value)
        lines.append('')
        lines.append(
            f'**Total**: {len(self._results)} agents, '
            f'{failed} failed, '
            f'{total_tokens:,} tokens, '
            f'{total_time:.1f}s agent-time'
        )

        return '\n'.join(lines)

    # ── Helper: build review prompt ──

    def _build_review_prompt(self, original_query: str) -> str:
        """Build the prompt for the master's reactive review."""
        parts = [f'# Original Task\n{original_query}\n']

        # Include dashboard
        dashboard = self._build_dashboard()
        parts.append(f'# Agent Dashboard\n{dashboard}\n')

        parts.append(
            f'# Detailed Results ({len(self._results)} agents completed)\n')

        for i, (spec, result) in enumerate(self._results):
            status_icon = ('✅' if result.status == SubAgentStatus.COMPLETED.value
                           else '❌')
            retried = (f' (retried {result.retry_count}x)'
                       if result.retry_count > 0 else '')
            parts.append(
                f'## Agent {i+1}: [{spec.role}] {spec.objective[:80]}\n'
                f'Status: {status_icon} {result.status}{retried}\n\n'
                f'{compress_result(result.final_answer, max_chars=2000)}\n'
            )

        if len(self.artifact_store) > 0:
            parts.append(
                f'\n# Shared Artifacts\n{self.artifact_store.summary()}\n')

        parts.append(
            '\n# Your Decision\n'
            'Review the results above. Are they sufficient to answer '
            'the original task?\n'
            '  • If YES → call swarm_done(summary="brief summary")\n'
            '  • If NO → call spawn_more_agents with additional agents '
            'to fill gaps\n'
            'Be strategic: don\'t spawn more unless genuinely needed.\n'
            'NEVER re-spawn agents whose objective duplicates a completed '
            'or running agent.'
        )

        return '\n'.join(parts)

    def _build_incremental_review_prompt(self, original_query: str,
                                          last_reviewed_index: int) -> str:
        """Build an incremental review prompt with compressed history.

        Instead of sending ALL results in full detail every review,
        this method:
          - Includes a **compressed summary** of results[:last_reviewed_index]
            (already reviewed — master has seen these before)
          - Includes **full details** only for results[last_reviewed_index:]
            (new since the last review)

        This significantly reduces token usage on subsequent reviews
        while preserving full context for new information.
        """
        parts = [f'# Original Task\n{original_query}\n']

        # Include dashboard (always full — it's compact)
        dashboard = self._build_dashboard()
        parts.append(f'# Agent Dashboard\n{dashboard}\n')

        # ── Previously reviewed results (compressed summary) ──
        old_results = self._results[:last_reviewed_index]
        new_results = self._results[last_reviewed_index:]

        if old_results:
            parts.append(
                f'# Previously Reviewed Results '
                f'({len(old_results)} agents — summary only)\n')
            for i, (spec, result) in enumerate(old_results):
                status_icon = ('✅' if result.status == SubAgentStatus.COMPLETED.value
                               else '❌')
                # Compressed: role + status + one-line key finding
                if result.status == SubAgentStatus.COMPLETED.value:
                    answer = (result.final_answer or '').replace('\n', ' ').strip()
                    key_finding = answer[:120] + ('…' if len(answer) > 120 else '')
                else:
                    key_finding = (result.error_message or 'Failed')[:120]
                parts.append(
                    f'  {i+1}. {status_icon} [{spec.role}] '
                    f'{spec.objective[:60]} → {key_finding}'
                )
            parts.append('')  # blank line separator

        # ── New results (full detail) ──
        if new_results:
            parts.append(
                f'# NEW Results Since Last Review '
                f'({len(new_results)} agents — full detail)\n')
            for i, (spec, result) in enumerate(new_results):
                global_index = last_reviewed_index + i
                status_icon = ('✅' if result.status == SubAgentStatus.COMPLETED.value
                               else '❌')
                retried = (f' (retried {result.retry_count}x)'
                           if result.retry_count > 0 else '')
                parts.append(
                    f'## Agent {global_index+1}: [{spec.role}] '
                    f'{spec.objective[:80]}\n'
                    f'Status: {status_icon} {result.status}{retried}\n\n'
                    f'{compress_result(result.final_answer, max_chars=2000)}\n'
                )
        else:
            parts.append('# No New Results\n(no new agents completed since last review)\n')

        if len(self.artifact_store) > 0:
            parts.append(
                f'\n# Shared Artifacts\n{self.artifact_store.summary()}\n')

        parts.append(
            '\n# Your Decision\n'
            'Review the NEW results above (you have already seen the '
            'previously reviewed results). Are all results sufficient to '
            'answer the original task?\n'
            '  • If YES → call swarm_done(summary="brief summary")\n'
            '  • If NO → call spawn_more_agents with additional agents '
            'to fill gaps\n'
            'Be strategic: don\'t spawn more unless genuinely needed.\n'
            'NEVER re-spawn agents whose objective duplicates a completed '
            'or running agent.'
        )

        return '\n'.join(parts)

    # ── Master review (LLM call) ─────────────────────

    def _master_review(self, original_query: str,
                       scheduler: StreamingScheduler,
                       log_prefix: str,
                       last_reviewed_index: int = 0) -> tuple:
        """Ask the master LLM to review results and decide next action.

        Calls the LLM with the current dashboard + results, using
        REACTIVE_MASTER_TOOLS (spawn_more_agents, swarm_done) +
        ARTIFACT_TOOLS (read_artifact, list_artifacts).

        When ``last_reviewed_index`` > 0, uses an incremental prompt
        that includes a compressed summary of already-reviewed results
        and full details only for NEW results since the last review.
        This reduces token usage on subsequent reviews.

        Returns:
            (decision_type, new_specs) where:
            - decision_type: 'swarm_done' | 'spawn_more' | 'continue'
            - new_specs: list[SubTaskSpec] (only if decision_type == 'spawn_more')
        """
        from lib.swarm.tools import REACTIVE_MASTER_TOOLS

        logger.debug('%s _master_review START — total_results=%d last_reviewed=%d incremental=%s',
                     log_prefix, len(self._results), last_reviewed_index,
                     last_reviewed_index > 0)

        # Build review messages — use incremental prompt if we have
        # a watermark from a previous review
        if last_reviewed_index > 0:
            review_prompt = self._build_incremental_review_prompt(
                original_query, last_reviewed_index)
        else:
            review_prompt = self._build_review_prompt(original_query)
        logger.debug('%s Review prompt length: %d chars (incremental=%s)',
                     log_prefix, len(review_prompt), last_reviewed_index > 0)

        messages = [
            {'role': 'system', 'content': (
                'You are a master orchestrator reviewing sub-agent results. '
                'Decide if you need more agents or if you have enough to '
                'produce a final answer.\n\n'
                'Available actions:\n'
                '  • spawn_more_agents — add more sub-agents for deeper '
                'investigation\n'
                '  • swarm_done — signal that you have enough results\n\n'
                'Be strategic: only spawn more if the results are genuinely '
                'insufficient. NEVER re-spawn agents with the same or very '
                'similar objective as agents that already completed or are '
                'still running.'
            )},
            {'role': 'user', 'content': review_prompt},
        ]

        # Master gets ALL user-selected tools plus swarm control tools
        master_tools = list(self.all_tools or [])
        # Append reactive swarm control tools (spawn_more_agents, swarm_done, etc.)
        master_tool_names = {t.get('function', {}).get('name') or t.get('name', '') for t in master_tools}
        for rt in REACTIVE_MASTER_TOOLS:
            rt_name = rt.get('function', {}).get('name') or rt.get('name', '')
            if rt_name not in master_tool_names:
                master_tools.append(rt)

        body = build_body(
            model=self.model,
            messages=messages,
            tools=master_tools,
            thinking_enabled=self.thinking_enabled,
            thinking_depth=self.thinking_depth,
        )

        content_parts: list[str] = []

        def on_content(chunk):
            content_parts.append(chunk)

        try:
            msg, stop_reason, usage = _dispatch_stream(
                body,
                on_content=on_content,
                abort_check=lambda: self._aborted or self.abort_check(),
                prefer_model=body.get('model', ''),
                log_prefix=f'{log_prefix}-review-r{len(self._results)}',
            )
        except Exception as e:
            logger.error('%s Master review LLM call failed: %s', log_prefix, e, exc_info=True)
            # On failure, treat as continue if scheduler has work, else done
            if scheduler.is_idle:
                return ('swarm_done', [])
            return ('continue', [])

        # Check for tool calls
        tool_calls = msg.get('tool_calls', [])
        logger.info('%s Master review LLM done — stop_reason=%s usage=%s tool_calls=%d content_len=%d',
                    log_prefix, stop_reason, usage,
                    len(tool_calls), len(''.join(content_parts)))

        if not tool_calls:
            # No tool calls = master produced text response.
            # If scheduler is idle, treat as done. Otherwise, continue.
            logger.info(
                '%s Master produced text response (no tool calls)', log_prefix)
            if scheduler.is_idle:
                return ('swarm_done', [])
            return ('continue', [])

        # Process tool calls
        new_specs: list[SubTaskSpec] = []
        for tc in tool_calls:
            fn_name = tc['function']['name']
            fn_args_raw = tc['function'].get('arguments', '{}')
            try:
                fn_args = (json.loads(fn_args_raw)
                           if isinstance(fn_args_raw, str) else fn_args_raw)
            except json.JSONDecodeError:
                logger.warning('[SwarmReview] Malformed JSON args for tool %s: %s', fn_name, fn_args_raw[:200], exc_info=True)
                fn_args = {}

            if fn_name == 'swarm_done':
                summary = fn_args.get('summary', '')
                logger.info(
                    '%s Master signalled done: %s', log_prefix, summary[:100])
                if self.on_progress:
                    self.on_progress({
                        'type': 'swarm_phase', 'phase': 'master_done',
                        'content': f'✋ Master: done — {summary[:200]}',
                    })
                return ('swarm_done', [])

            elif fn_name == 'spawn_more_agents':
                new_agents = fn_args.get('agents', [])
                reason = fn_args.get('reason', '')
                logger.info(
                    '%s Master spawning %d more agents: %s',
                    log_prefix, len(new_agents), reason[:100])

                for agent_def in new_agents:
                    spec = SubTaskSpec(
                        role=agent_def.get('role', 'general'),
                        objective=agent_def.get('objective', ''),
                        context=agent_def.get('context', ''),
                        depends_on=agent_def.get('depends_on', []),
                        id=agent_def.get('id', str(uuid.uuid4())[:8]),
                        max_retries=self.max_retries,
                    )
                    # Add artifact store summary as context
                    if len(self.artifact_store) > 0:
                        artifact_summary = self.artifact_store.summary(
                            max_preview=200)
                        spec.context += (
                            f'\n\nAvailable shared artifacts:\n'
                            f'{artifact_summary}')
                    new_specs.append(spec)

            elif fn_name == 'read_artifact':
                # Master reading an artifact during review — handle locally
                key = fn_args.get('key', '')
                content = self.artifact_store.get(key)
                if content:
                    logger.debug(
                        '%s Master read artifact: %s', log_prefix, key)

            elif fn_name == 'list_artifacts':
                # Master listing artifacts — handle locally
                logger.debug('%s Master listed artifacts', log_prefix)

            elif fn_name == 'check_agents':
                # Return status info — not actionable in reactive mode
                pass

        if new_specs:
            return ('spawn_more', new_specs)

        # No actionable tool calls
        if scheduler.is_idle:
            return ('swarm_done', [])
        return ('continue', [])
