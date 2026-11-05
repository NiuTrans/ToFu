"""Destination request admission for fallback and switch-back.

Entries: prepare_switchback and prepare_fallback_body. Both refresh mandatory
turn requirements, build a fresh body for the destination model and attach the
round's request metadata through _request_metadata, so the shared final tool
projection matches an ordinary round on that model."""

from lib import llm
from lib.agent_core.events import Phase, build_phase
from lib.log import get_logger
from lib.tasks_pkg.manager import _events as task_events

from ._request_metadata import attach_rebuilt_request_metadata

logger = get_logger(__name__)


def prepare_switchback(
    task, body, model, fallback_model, round_num, max_tokens, tool_list, messages
):
    """Probe the original model after one served refusal-fallback round.

    A second refusal latches the fallback in the caller. Preparation is an
    admission boundary; failures propagate before a provider attempt starts.
    """
    tid = task['id'][:8]
    switchback_probe = (
        bool(fallback_model)
        and model == fallback_model
        and task.get('_fallback_kind') == 'content_filter'
        and not task.get('_refusal_switchback_failed')
        and task.get('_fallback_served_rounds', 0) >= 1
    )
    if switchback_probe:
        # Rebuild for the original model with its own admission; the round's
        # request metadata keeps its final projection on the ordinary surface.
        _probe_model = str(task.get('_fallback_from') or '')
        if not _probe_model or _probe_model == fallback_model:
            _probe_model = ''
        if _probe_model:
            logger.info(
                '[%s] 🔁 Switch-back probe: round %d re-tries %s after one '
                'fallback round on %s (rest-of-turn stickiness on a refusal '
                'proved over-conservative live)',
                tid,
                round_num + 1,
                _probe_model,
                fallback_model,
            )
            task_events.append_event(
                task,
                build_phase(
                    Phase.RETRYING,
                    detail=(
                        f'🔁 已自动切回 {_probe_model} 试一轮 — '
                        f'若再次被拒则本轮内保持在 {fallback_model}'
                    ),
                    detailKey='stream.phase.refusalSwitchback',
                    detailArgs={'model': _probe_model, 'fallback': fallback_model},
                ),
            )
            from lib.tasks_pkg.context_composer.turn_requirements import (
                refresh_turn_requirements,
            )

            refresh_turn_requirements(messages, task=task, model=_probe_model)
            from lib.tasks_pkg.model_images import task_image_reuse
            probe_body = llm.build_body(
                _probe_model,
                messages,
                reasoning_intent=body.get('_reasoning_intent'),
                defer_reasoning=True,
                max_tokens=max_tokens,
                temperature=1.0,
                thinking_enabled=True,
                # The probe target is the user's admitted selection; preset
                # is a model identity downstream of _resolve_model_config.
                preset=_probe_model,
                thinking_depth=body.get('_thinking_depth') or 'max',
                tools=tool_list,
                response_format=body.get('response_format'),
                stream=True,
                image_reuse=task_image_reuse(task, model=_probe_model),
                image_user_id=task.get('_userId'),
            )
            body = attach_rebuilt_request_metadata(
                task,
                body,
                probe_body,
                messages,
                tool_list,
                round_num=round_num,
                tid=tid,
            )
            model = _probe_model

    return body, model, switchback_probe


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
    tid = task['id'][:8]
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
