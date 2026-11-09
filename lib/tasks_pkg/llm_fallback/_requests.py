"""Destination request admission for fallback.

prepare_fallback_body refreshes mandatory turn requirements, builds a fresh
body for the destination model and attaches the round's request metadata
through _request_metadata, so the shared final tool projection matches an
ordinary round on that model."""

from lib import llm
from lib.log import get_logger, task_log_tag

from ._request_metadata import attach_rebuilt_request_metadata

logger = get_logger(__name__)


def prepare_fallback_body(
    task,
    body,
    original_model,
    fallback_model,
    max_tokens,
    tool_list,
    messages,
    *,
    round_num=0,
):
    """Re-admit the destination with the round's request metadata."""
    tid = task_log_tag(task['id'])
    _vision_fallback_from = (
        original_model
        if (
            llm.model_supports_vision(original_model)
            and not llm.model_supports_vision(fallback_model)
        )
        else ''
    )
    # A rebuilt fallback is another admission, unlike a transport resend of
    # an already frozen body. Compaction may have partially changed messages.
    from lib.tasks_pkg.context_composer.turn_requirements import (
        refresh_turn_requirements,
    )

    refresh_turn_requirements(messages, task=task, model=fallback_model)
    from lib.tasks_pkg.model_images import task_image_reuse
    fallback_body = llm.build_body(
        fallback_model,
        messages,
        reasoning_intent=body.get('_reasoning_intent'),
        # Source capabilities do not authorize a different destination.
        defer_reasoning=True,
        max_tokens=max_tokens,
        temperature=1.0,
        thinking_enabled=True,
        preset='opus',
        thinking_depth=body.get('_thinking_depth') or 'medium',
        tools=tool_list,
        response_format=body.get('response_format'),
        stream=True,
        vision_fallback_from=_vision_fallback_from,
        image_reuse=task_image_reuse(task, model=fallback_model),
        image_user_id=task.get('_userId'),
    )
    # The task's tool authority/visibility and the round's stable identity make
    # the final projection send the schemas of an ordinary destination round.
    attach_rebuilt_request_metadata(
        task,
        body,
        fallback_body,
        messages,
        tool_list,
        round_num=round_num,
        tid=tid,
    )

    return fallback_body, _vision_fallback_from
