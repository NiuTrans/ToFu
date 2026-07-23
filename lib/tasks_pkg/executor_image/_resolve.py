# HOT_PATH
"""Source-image resolution + prior image-gen history extraction.

Base submodule of the ``executor_image`` package. Owns the shared
``_APP_ROOT`` constant and the ``_images_dir()`` authority (both consumed by
the sibling ``_svg`` / ``_save`` submodules), so it must stay dependency-free
of the other package submodules to keep the import graph acyclic.
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)

# ── Shared constant: application root ──
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _images_dir() -> str:
    """Absolute path to the uploads/images dir under the resolved runtime base.

    User-generated images are USER STATE referenced from the DB by
    ``/api/images/`` URLs, so they must co-locate with the DB via
    ``runtime_paths.uploads_root()`` — not the code tree. Byte-identical to
    ``<repo>/uploads/images`` in the default in-tree layout; falls back to it
    if runtime_paths is somehow unavailable.
    """
    try:
        from lib.runtime_paths import uploads_root
        return os.path.join(uploads_root(), 'images')
    except Exception as e:  # pragma: no cover — defensive
        logger.debug('[Tool:generate_image] uploads_root() unavailable, using in-tree: %s', e)
        return os.path.join(_APP_ROOT, 'uploads', 'images')


def _resolve_source_image(image_ref: str) -> dict | None:
    """Resolve an image reference (URL or local path) to ``{image_b64, mime_type}``.

    Handles:
    - Local ``/api/images/xxx.png`` paths → read from disk
    - Remote ``https://...`` URLs → download
    - ``data:image/...;base64,...`` data URIs → extract

    Returns:
        dict ``{image_b64, mime_type}`` on success, None on failure.
    """
    import base64 as _b64

    from lib.http_client import http_get

    if not image_ref:
        return None

    # ── Data URI ──
    if image_ref.startswith('data:'):
        try:
            header, b64_part = image_ref.split(',', 1)
            mime_type = header.split(':')[1].split(';')[0]
            return {'image_b64': b64_part, 'mime_type': mime_type}
        except (ValueError, IndexError) as e:
            logger.warning('[Tool:generate_image] Failed to parse data URI: %s', e)
            return None

    # ── Local file: /api/images/xxx.png → read from disk ──
    if image_ref.startswith('/api/images/'):
        filename = os.path.basename(image_ref)
        filepath = os.path.join(_images_dir(), filename)
        try:
            with open(filepath, 'rb') as f:
                raw = f.read()
            image_b64 = _b64.b64encode(raw).decode('ascii')
            mime_type = 'image/png'
            if filename.endswith(('.jpg', '.jpeg')):
                mime_type = 'image/jpeg'
            elif filename.endswith('.webp'):
                mime_type = 'image/webp'
            return {'image_b64': image_b64, 'mime_type': mime_type}
        except Exception as e:
            logger.warning('[Tool:generate_image] Failed to read local image %s: %s', filepath, e)
            return None

    # ── Remote URL ──
    if image_ref.startswith(('http://', 'https://')):
        try:
            resp = http_get(image_ref, timeout=30)
            resp.raise_for_status()
            image_b64 = _b64.b64encode(resp.content).decode('ascii')
            ct = resp.headers.get('Content-Type', 'image/png')
            mime_type = ct.split(';')[0].strip() if ct.startswith('image/') else 'image/png'
            return {'image_b64': image_b64, 'mime_type': mime_type}
        except Exception as e:
            logger.warning('[Tool:generate_image] Failed to download image %.80s: %s', image_ref[:80], e)
            return None

    # ── Local filesystem path (absolute or relative to app root) ──
    filepath = image_ref
    if not os.path.isabs(filepath):
        filepath = os.path.join(_APP_ROOT, filepath)
    # Also handle absolute paths that point inside the app root (e.g. model passes full server path)
    if os.path.isfile(filepath):
        try:
            _EXT_MIME = {
                '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp',
                '.svg': 'image/svg+xml',
            }
            ext = os.path.splitext(filepath)[1].lower()
            mime_type = _EXT_MIME.get(ext, 'image/png')
            with open(filepath, 'rb') as f:
                raw = f.read()
            image_b64 = _b64.b64encode(raw).decode('ascii')
            logger.info('[Tool:generate_image] Resolved local file path: %.80s (%d KB)',
                        image_ref[:80], len(raw) // 1024)
            return {'image_b64': image_b64, 'mime_type': mime_type}
        except Exception as e:
            logger.warning('[Tool:generate_image] Failed to read local file %.80s: %s',
                           image_ref[:80], e)
            return None

    logger.warning('[Tool:generate_image] Unrecognized source_image format or file not found: %.80s',
                   image_ref[:80])
    return None


def _extract_image_gen_history(task, messages=None):
    """Extract prior image generation history for multi-turn editing.

    Scans two sources (oldest-first order):

    1. **Conversation messages** — tool result messages with ``image_url``
       content blocks from previous tasks (cross-turn history).
    2. **Current task toolRounds** — successful ``generate_image`` rounds
       from this task that have ``imageDataUri`` (intra-turn history).

    Returns:
        List of dicts ``{prompt, image_b64, text, mime_type}`` — oldest first.
    """
    history = []

    # ── Phase 1: Scan conversation messages ──
    if messages:
        for i, msg in enumerate(messages):
            if msg.get('role') != 'tool':
                continue
            content = msg.get('content')
            if not isinstance(content, list):
                continue
            image_b64 = ''
            mime_type = 'image/png'
            text_desc = ''
            has_image = False
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'image_url':
                    url = block.get('image_url', {}).get('url', '')
                    if url.startswith('data:'):
                        try:
                            header, b64_part = url.split(',', 1)
                            mime_type = header.split(':')[1].split(';')[0]
                            image_b64 = b64_part
                            has_image = True
                        except (ValueError, IndexError) as e:
                            logger.debug('Failed to parse base64 image data: %s', e)
                elif block.get('type') == 'text':
                    text_desc = block.get('text', '')
            if has_image and image_b64 and 'Image generated' in text_desc:
                prompt = ''
                for line in text_desc.split('\n'):
                    if line.startswith('Prompt: '):
                        prompt = line[len('Prompt: '):]
                        break
                history.append({
                    'prompt': prompt,
                    'image_b64': image_b64,
                    'text': '',
                    'mime_type': mime_type,
                })

    # ── Phase 2: Scan current task's toolRounds ──
    for sr in (task.get('toolRounds') or []):
        if sr.get('toolName') != 'generate_image':
            continue
        results = sr.get('results') or []
        if not results:
            continue
        meta = results[0] if isinstance(results, list) else results
        data_uri = meta.get('imageDataUri', '')
        if not data_uri:
            continue

        image_b64 = ''
        mime_type = 'image/png'
        if data_uri.startswith('data:'):
            try:
                header, b64_part = data_uri.split(',', 1)
                mime_type = header.split(':')[1].split(';')[0]
                image_b64 = b64_part
            except (ValueError, IndexError):
                logger.warning('[Tool:generate_image] Failed to parse imageDataUri for history')
                continue
        else:
            image_b64 = data_uri

        if not image_b64:
            continue

        history.append({
            'prompt': meta.get('imagePrompt', ''),
            'image_b64': image_b64,
            'text': meta.get('imageText', ''),
            'mime_type': mime_type,
        })

    return history
