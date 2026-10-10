"""Model-only execute_tools receipt projection; canonical/frontend stay untouched.

Entry point: project_gateway_result. Depends only on the standard library.
Problem: receipt transport fields and escaped multiline outputs waste model budget.
Decision: recognize the producer batch, strip only declared child controls, and
render text outputs as length-labelled sections. Application JSON text is never
decoded or unwrapped. Program values remain JSON values. Unknown metadata,
failures, and recovery references survive; uncertain/media shapes pass through.
The caller must retain canonical source and apply its normal budget afterward.
"""
from __future__ import annotations

from lib.tools.result_projection_json import (
    dump_projection_json as _json, load_projection_json,
)


# These keys are producer receipt controls, not recursive application keys.
_CHILD_CONTROLS = frozenset({
    'duration', 'source', 'raw_output_bytes', 'output_bytes', 'canonical', 'resultId',
})
_MEDIA_KEYS = frozenset({
    '__screenshot__', 'image_url', 'imageData', 'audio', 'video',
    'media', 'mediaBlocks', 'contentBlocks', 'modelBlocks',
})
_MEDIA_TYPES = frozenset({
    'image', 'image_url', 'input_image', 'audio', 'input_audio', 'video',
    'resource', 'resource_link',
})


def _has_media(value):
    """Fail closed for structured media; do not inspect/alter opaque text."""
    if isinstance(value, dict):
        return (bool(_MEDIA_KEYS.intersection(value))
                or value.get('type') in _MEDIA_TYPES
                or any(_has_media(item) for item in value.values()))
    if isinstance(value, list):
        return any(_has_media(item) for item in value)
    return False


def project_gateway_result(tool_name: str, text: str) -> str | None:
    """Return a compact text view or None (caller retains the exact source).

    A tool-name policy plus the producer's status/results/warnings shape is the
    admission boundary. Never infer success from an empty results array, strip
    fields inside child output, or reinterpret arbitrary application envelopes.
    Numeric tokens (including huge integers and exponent/negative-zero forms)
    are emitted verbatim; JSON strings stay strings and program objects stay
    objects. Only output/error strings use raw length-labelled text sections.
    """
    if tool_name != 'execute_tools' or not isinstance(text, str):
        return None
    try:
        value = load_projection_json(text)
        if (not isinstance(value, dict)
                or type(value.get('status')) is not str
                or not isinstance(value.get('results'), list)
                or not isinstance(value.get('warnings'), list)
                or 'contractVersion' in value
                or _has_media(value)):
            return None
        children = value['results']
        if not all(isinstance(child, dict)
                   and type(child.get('name')) is str
                   and type(child.get('call_id')) is str
                   and type(child.get('status')) is str for child in children):
            return None
        lines = [_json({key: item for key, item in value.items() if key != 'results'})]
        if not children:
            lines.append('results: []')
        for index, child in enumerate(children):
            metadata = {}
            bodies = []
            for key, item in child.items():
                if key in _CHILD_CONTROLS:
                    # Incomplete delivery may have only a capture identity,
                    # not a ready canonicalRef. Do not erase its recovery seam.
                    if key != 'resultId' or not child.get('output_truncated'):
                        continue
                if key == 'approval' and item == {'required': False, 'status': 'not_required'}:
                    continue
                if key in {'output', 'error'} and type(item) is str:
                    bodies.append((key, item))
                else:
                    metadata[key] = item
            lines.extend((f'\n## results[{index}]', _json(metadata)))
            for key, body in bodies:
                lines.extend((f'{key} (text; {len(body)} chars):', body))
        return '\n'.join(lines)
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None


__all__ = ['project_gateway_result']
