"""Pin image bytes in the task-owned projection before provider conversion.

Only fresh image messages are hydrated. Durable source rows and caller-owned
history are never modified; provider retries reuse the same captured version.
A retained projection may reuse captured bytes only while they remain
reproducible from current authority (``retained_images_reproducible``).
"""
from __future__ import annotations

_TEXT_BLOCK_TYPES = frozenset({'text', 'input_text', 'output_text'})


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
        # The task owns ``message``; the validated blocks are fresh copies.
        message['content'] = _validate_image_blocks(
            [message], user_id=user_id)[0]['content']
        # Model-specific resizing stays in the fresh wire body. Retaining the
        # original bytes lets a different fallback model choose its own cap.
        message['_modelImageSources'] = list(dict.fromkeys(
            list(message.get('_modelImageSources') or [])
            + [source for source in sources if not source.startswith('data:')]))
        message['_modelImagesPinnedFor'] = model
        message['_modelImagesPinnedOwner'] = user_id


def _image_url(block):
    if not isinstance(block, dict) or block.get('type') != 'image_url':
        return None
    image = block.get('image_url')
    url = image.get('url') if isinstance(image, dict) else None
    return url if isinstance(url, str) else None


def _inline_payloads(messages):
    payloads = set()
    for message in messages:
        content = message.get('content') if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            url = _image_url(block)
            if url is not None and url.startswith('data:'):
                payloads.add(url.partition(',')[2])
    return payloads


def _source_payloads(sources, *, user_id):
    references = [source for source in sources or ()
                  if isinstance(source, str) and source and not source.startswith('data:')]
    if not references:
        return set()
    from lib.llm.body._images import _validate_image_blocks
    probe = [{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': reference}} for reference in references]}]
    return _inline_payloads(_validate_image_blocks(probe, user_id=user_id))


def retained_images_reproducible(messages, *, current_messages, user_id):
    """Whether every image a retained model view carries is current authority.

    Image references (``/api/images/``, ``tofu-media:``, remote URLs) are
    resolved again at dispatch, so only captured bytes need proof: each inline
    payload must equal one the current authoritative projection carries, or
    the bytes a source recorded on the same message resolves to now for this
    owner. A removed or replaced source is therefore never resurrected from a
    retained copy. Unprojected attachment refs and other media forms have no
    such proof and refuse.
    """
    from lib.tasks_pkg.message_semantics import has_model_media
    current = None
    for message in messages:
        if message.get('attachments') or has_model_media({
                key: message[key] for key in ('_responses_items', '_anthropic_content_blocks')
                if key in message}):
            return False
        content = message.get('content')
        if content is None or isinstance(content, str):
            continue
        if not isinstance(content, list):
            return False
        sources = None
        for block in content:
            if isinstance(block, dict) and block.get('type') in _TEXT_BLOCK_TYPES:
                continue
            url = _image_url(block)
            if not url:
                return False
            if not url.startswith('data:'):
                continue
            payload = url.partition(',')[2]
            if current is None:
                current = _inline_payloads(current_messages)
            if payload in current:
                continue
            if sources is None:
                sources = _source_payloads(message.get('_modelImageSources'), user_id=user_id)
            if payload not in sources:
                return False
    return True


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
