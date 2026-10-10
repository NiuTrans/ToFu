"""Pin image bytes in the task-owned projection before provider conversion.

Only fresh image messages are hydrated. Durable source rows and caller-owned
history are never modified; provider retries reuse the same captured version.
"""
from __future__ import annotations


def pin_model_images(messages, *, model, user_id):
    from lib.model_info import model_supports_vision
    if not model_supports_vision(model):
        return
    from lib.llm.body._images import _validate_image_blocks
    for message in messages:
        if ('_modelImagesPinnedOwner' in message
                and message['_modelImagesPinnedOwner'] != user_id):
            raise ValueError('pinned image projection owner mismatch')
        content = message.get('content')
        if not isinstance(content, list) or message.get('_modelImagesPinnedFor') == model:
            continue
        sources = [block.get('image_url', {}).get('url', '') for block in content
                   if isinstance(block, dict) and block.get('type') == 'image_url']
        if not sources:
            continue
        _validate_image_blocks([message], user_id=user_id)
        # Model-specific resizing stays in the fresh wire body. Retaining the
        # original bytes lets a different fallback model choose its own cap.
        message['_modelImageSources'] = list(dict.fromkeys(
            list(message.get('_modelImageSources') or [])
            + [source for source in sources if not source.startswith('data:')]))
        message['_modelImagesPinnedFor'] = model
        message['_modelImagesPinnedOwner'] = user_id


def task_image_reuse(task, *, model):
    """Return task-owned derived data; settlement drops _modelImageReuse."""
    from lib.model_info import model_supports_vision
    from lib.llm.body._image_reuse import ImagePreparationReuse
    if not model_supports_vision(model):
        return None
    owner = task.get('_userId')
    reuse = task.get('_modelImageReuse')
    if reuse is None or reuse.user_id != owner:
        if reuse is not None:
            reuse.clear()
        reuse = ImagePreparationReuse(user_id=owner)
        task['_modelImageReuse'] = reuse
    return reuse
