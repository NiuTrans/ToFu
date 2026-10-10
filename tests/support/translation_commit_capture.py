"""Consume loaned translation mappings inside synchronous fake commit hooks."""

def capture_translation_kwargs(kwargs):
    copied = dict(kwargs)
    for field in ('segment_translations', 'segment_sources'):
        value = copied.get(field)
        if value is not None:
            copied[field] = dict(value)
    return copied
