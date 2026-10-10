# HOT_PATH
"""Shared helpers for the built-in Layer-1 compaction steps.

Small, LLM-free utilities used across the step submodules (thinking /
tool-results / interstitial / images / assistant).  They live here so
each step submodule can import them without duplication.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def _log_id(conv_id: str) -> str:
    return conv_id[:8] if conv_id else '?'


def _image_recovery_note(msg: dict, *, limit: int = 4) -> str:
    """Suffix naming the durable refs a stripped image can be re-read through.

    A multimodal tool message pins its non-data: image sources on
    ``_modelImageSources`` (``tofu-media:<id>`` for retained bytes, possibly
    ``/api/images/...`` upload paths); checkpoint-restored messages may also
    carry ``tofu-media:`` URLs inside their image_url blocks. Both are
    converted to the model-facing ``att_media_<id>`` form that
    ``inspect_image`` / ``read_files`` already resolve. Returns ``''`` when no
    durable ref exists (caller falls back to "re-call tool").
    """
    refs: list[str] = []

    def _add(source) -> None:
        if not isinstance(source, str) or not source:
            return
        ref = ('att_media_' + source[len('tofu-media:'):]
               if source.startswith('tofu-media:') else source)
        if ref not in refs and len(refs) < limit:
            refs.append(ref)

    for source in msg.get('_modelImageSources') or []:
        _add(source)
    content = msg.get('content')
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'image_url':
                url = block.get('image_url', {}).get('url', '')
                if url.startswith('tofu-media:'):
                    _add(url)
    if not refs:
        return ''
    quoted = ', '.join(f'"{ref}"' for ref in refs)
    return f'[Image(s) retained — view again with inspect_image({quoted})]'
