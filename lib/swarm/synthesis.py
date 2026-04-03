"""lib/swarm/synthesis.py — Result synthesis for swarm sub-agent outputs.

Extracted from master.py:
  • _build_synthesis_prompt() — build prompt combining all sub-agent results
  • _synthesise() — LLM call to combine sub-agent results into final answer
"""

from collections.abc import Callable
from typing import Optional

from lib.llm_client import build_body
from lib.llm_dispatch import dispatch_stream as _dispatch_stream
from lib.log import get_logger
from lib.swarm.protocol import (
    ArtifactStore,
    SubAgentResult,
    SubAgentStatus,
    SubTaskSpec,
    compress_result,
)

logger = get_logger(__name__)


def _build_synthesis_prompt(user_query: str,
                            all_results: list[tuple[SubTaskSpec, SubAgentResult]],
                            artifact_store: ArtifactStore | None = None) -> str:
    """Build the prompt for result synthesis."""
    parts = [
        f'# Original Task\n{user_query}\n',
        '# Sub-Agent Results\n',
    ]

    for i, (spec, result) in enumerate(all_results):
        status_icon = '✅' if result.status == SubAgentStatus.COMPLETED.value else '❌'
        retried = f' (retried {result.retry_count}x)' if result.retry_count > 0 else ''
        parts.append(
            f'## Agent {i+1}: [{spec.role}] {spec.objective[:80]}\n'
            f'Status: {status_icon} {result.status}{retried}\n\n'
            f'{compress_result(result.final_answer, max_chars=4000)}\n'
        )

    if artifact_store and len(artifact_store) > 0:
        parts.append(
            f'\n# Shared Artifacts\n{artifact_store.summary(max_preview=200)}\n'
        )

    parts.append(
        '\n# Instructions\n'
        'Synthesise the above sub-agent results into a comprehensive, '
        'well-structured response to the original task. '
        'Resolve any conflicts between agents. '
        'If any agents failed, note what was missing. '
        'Present the information clearly with proper formatting.'
    )

    prompt_text = '\n'.join(parts)
    logger.info('[Swarm-Synth] Built synthesis prompt: %d chars, %d agents, artifacts=%s',
                len(prompt_text), len(all_results),
                len(artifact_store) if artifact_store else 0)
    return prompt_text


def _synthesise(prompt: str, model: str, *,
                thinking_enabled: bool = True,
                thinking_depth: str = None,
                abort_check: Callable | None = None,
                on_event: Callable | None = None) -> str:
    """Make a synthesis LLM call to combine sub-agent results."""
    logger.info('[Swarm-Synth] Starting synthesis LLM call (prompt_len=%d, model=%s)',
                len(prompt), model)

    messages = [
        {'role': 'system', 'content':
            'You are a helpful assistant synthesising research results.'},
        {'role': 'user', 'content': prompt},
    ]

    body = build_body(
        model=model,
        messages=messages,
        tools=None,
        thinking_enabled=thinking_enabled,
        thinking_depth=thinking_depth,
    )

    content_parts: list[str] = []

    def on_content(chunk):
        content_parts.append(chunk)
        if on_event:
            on_event({
                'type': 'swarm_synthesis_content',
                'content': chunk,
                'incremental': True,
            })

    msg, _, usage = _dispatch_stream(
        body,
        on_content=on_content,
        abort_check=abort_check,
        prefer_model=body.get('model', ''),
        log_prefix='[Swarm-Synth]',
    )

    result = msg.get('content', ''.join(content_parts))
    logger.info('[Swarm-Synth] Synthesis complete — output_len=%d usage=%s',
                len(result), usage)
    return result
