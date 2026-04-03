# HOT_PATH
"""Image generation tool handler — extracted from executor.py for modularity."""

from __future__ import annotations

import os
import re
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

# ── Shared constant: application root ──
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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

    import requests as _requests

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
        filepath = os.path.join(_APP_ROOT, 'uploads', 'images', filename)
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
            resp = _requests.get(image_ref, timeout=30)
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
    2. **Current task searchRounds** — successful ``generate_image`` rounds
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

    # ── Phase 2: Scan current task's searchRounds ──
    for sr in (task.get('searchRounds') or []):
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


def _save_image_to_disk(image_b64, mime_type='image/png'):
    """Save base64 image to uploads/images/ and return the local URL path."""
    import base64 as _b64
    import time as _time

    ext_map = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}
    ext = ext_map.get(mime_type, '.png')
    filename = f'gen_{int(_time.time() * 1000)}{ext}'

    upload_dir = os.path.join(_APP_ROOT, 'uploads', 'images')
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, filename)

    try:
        raw_bytes = _b64.b64decode(image_b64)
        with open(filepath, 'wb') as f:
            f.write(raw_bytes)
        logger.info('[Tool:generate_image] Saved image to %s (%d KB)',
                    filename, len(raw_bytes) // 1024)
        return f'/api/images/{filename}'
    except Exception as e:
        logger.warning('[Tool:generate_image] Failed to save image to disk: %s', e)
        return ''


def _save_image_to_project(image_b64, mime_type, output_path, project_path,
                           conv_id=None, task_id=None):
    """Save base64 image to a path inside the active project directory."""
    import base64 as _b64

    from lib.project_mod.modifications import _record_modification, _schedule_index_update
    from lib.project_mod.scanner import _safe_path
    from lib.project_mod.tools import _touch_for_vscode

    try:
        target = _safe_path(project_path, output_path)
    except ValueError as e:
        logger.warning('[Tool:generate_image] Project save path rejected %s: %s',
                       output_path, e)
        return ''

    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            logger.warning('[Tool:generate_image] makedirs failed for %s: %s',
                           parent, e, exc_info=True)
            return ''

    existed = os.path.isfile(target)
    original_content = None
    if existed:
        try:
            with open(target, 'rb') as f:
                original_content = f.read()
        except Exception as e:
            logger.debug('[Tool:generate_image] Could not read original %s: %s',
                         output_path, e)

    try:
        raw_bytes = _b64.b64decode(image_b64)
        with open(target, 'wb') as f:
            f.write(raw_bytes)
            f.flush()
            os.fsync(f.fileno())
        _touch_for_vscode(target)

        logger.info('[Tool:generate_image] Saved image to project path %s (%d KB)',
                    output_path, len(raw_bytes) // 1024)

        _record_modification(
            project_path, 'write_file', output_path,
            original_content=original_content if existed else None,
            conv_id=conv_id, task_id=task_id,
        )
        _schedule_index_update(project_path, output_path)

        return output_path
    except Exception as e:
        logger.error('[Tool:generate_image] Failed to save image to project path %s: %s',
                     output_path, e, exc_info=True)
        return ''


def _convert_to_svg(saved_url: str, project_save_path: str,
                    project_path: str | None,
                    conv_id: str | None = None,
                    task_id: str | None = None) -> tuple:
    """Convert a saved PNG to SVG using vtracer (background removal + tracing).

    Converts the PNG file that was already saved to disk/project into an SVG
    placed alongside it (same directory, same basename, .svg extension).

    Args:
        saved_url: Local URL path like ``/api/images/gen_xxx.png`` (uploads folder).
        project_save_path: Relative path inside the project (e.g. ``static/logo.png``).
        project_path: Absolute path to the active project root.
        conv_id: Conversation ID for modification tracking.
        task_id: Task ID for modification tracking.

    Returns:
        Tuple of ``(svg_saved_url, svg_project_path)`` — empty strings on failure.
    """
    try:
        import importlib.util
        _svg_script = os.path.join(_APP_ROOT, 'scripts', 'png_to_svg.py')
        spec = importlib.util.spec_from_file_location('png_to_svg', _svg_script)
        _mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_mod)
        convert_png_to_svg = _mod.convert_png_to_svg
    except Exception as e:
        logger.warning('[Tool:generate_image] SVG conversion unavailable: %s', e)
        return '', ''

    svg_saved_url = ''
    svg_project_path = ''

    # ── Convert the uploads copy ──
    if saved_url:
        filename = os.path.basename(saved_url)
        png_path = os.path.join(_APP_ROOT, 'uploads', 'images', filename)
        svg_filename = os.path.splitext(filename)[0] + '.svg'
        svg_path = os.path.join(_APP_ROOT, 'uploads', 'images', svg_filename)
        try:
            ok = convert_png_to_svg(png_path, svg_path)
            if ok:
                svg_saved_url = f'/api/images/{svg_filename}'
                logger.info('[Tool:generate_image] SVG saved to uploads: %s', svg_filename)
            else:
                logger.warning('[Tool:generate_image] SVG conversion failed for uploads copy')
        except Exception as e:
            logger.error('[Tool:generate_image] SVG conversion error (uploads): %s', e, exc_info=True)

    # ── Convert the project copy ──
    if project_save_path and project_path:
        png_abs = os.path.join(project_path, project_save_path)
        svg_rel = os.path.splitext(project_save_path)[0] + '.svg'
        svg_abs = os.path.join(project_path, svg_rel)
        try:
            ok = convert_png_to_svg(png_abs, svg_abs)
            if ok:
                svg_project_path = svg_rel
                logger.info('[Tool:generate_image] SVG saved to project: %s', svg_rel)

                # Record modification for undo support
                try:
                    from lib.project_mod.modifications import _record_modification, _schedule_index_update
                    from lib.project_mod.tools import _touch_for_vscode
                    _record_modification(
                        project_path, 'write_file', svg_rel,
                        original_content=None,
                        conv_id=conv_id, task_id=task_id,
                    )
                    _schedule_index_update(project_path, svg_rel)
                    _touch_for_vscode(svg_abs)
                except Exception as e:
                    logger.debug('[Tool:generate_image] SVG mod tracking failed: %s', e)
            else:
                logger.warning('[Tool:generate_image] SVG conversion failed for project copy')
        except Exception as e:
            logger.error('[Tool:generate_image] SVG conversion error (project): %s', e, exc_info=True)

    return svg_saved_url, svg_project_path


def register_image_gen_handler(tool_registry, IMAGE_GEN_TOOL_NAMES, _finalize_tool_round, append_event):
    """Register the generate_image handler on the given tool registry.

    Called from executor.py to wire up the handler without circular imports.
    """

    @tool_registry.tool_set(IMAGE_GEN_TOOL_NAMES, category='image_gen',
                            description='Generate an image from a text prompt')
    def _handle_generate_image(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
        import base64 as _b64
        import time as _time

        from lib.image_gen import generate_image
        from lib.log import log_context

        prompt = fn_args.get('prompt', '')
        aspect_ratio = fn_args.get('aspect_ratio', '1:1')
        resolution = fn_args.get('resolution', '1K')
        output_path = fn_args.get('output_path', '')
        source_image = fn_args.get('source_image', '')
        svg_convert = fn_args.get('svg', False)

        if not prompt:
            logger.warning('[Tool:generate_image] Empty prompt, rn=%d', rn)
            meta = {
                'toolName': 'generate_image',
                'imagePrompt': '', 'imageError': 'No prompt provided',
                'imageAspectRatio': aspect_ratio, 'imageResolution': resolution,
                'badge': '❌ failed',
            }
            _finalize_tool_round(task, rn, round_entry, [meta])
            return tc_id, 'Error: No image prompt provided.', False

        # ── Resolve source_image for editing ──
        source_images = None
        if source_image:
            resolved = _resolve_source_image(source_image)
            if resolved:
                source_images = [resolved]
                logger.info('[Tool:generate_image] Resolved source_image for editing: %.80s → %d bytes b64',
                            source_image[:80], len(resolved.get('image_b64', '')))
            else:
                logger.warning('[Tool:generate_image] Failed to resolve source_image: %.80s', source_image[:80])

        is_edit = bool(source_images)

        # ── Emit progress event ──
        round_entry['status'] = 'running'
        badge_text = '⏳ editing…' if is_edit else '⏳ generating…'
        round_entry['results'] = [{
            'toolName': 'generate_image',
            'imagePrompt': prompt[:100],
            'imageAspectRatio': aspect_ratio, 'imageResolution': resolution,
            'badge': badge_text,
        }]
        append_event(task, {'type': 'tool_result', 'roundNum': rn,
                            'query': round_entry['query'], 'results': round_entry['results']})

        # ── Extract image gen history ──
        history = _extract_image_gen_history(task, messages=task.get('messages'))
        if history:
            logger.info('[Tool:generate_image] Found %d prior image gen rounds for multi-turn',
                        len(history))

        t0 = _time.time()
        logger.info('[Tool:generate_image] prompt="%.80s" ar=%s res=%s output=%s rn=%d history=%d edit=%s',
                    prompt[:80], aspect_ratio, resolution, output_path or '(none)', rn,
                    len(history), is_edit)

        try:
            with log_context('generate_image_tool', logger=logger):
                result = generate_image(
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    history=history or None,
                    source_images=source_images,
                )
        except Exception as e:
            logger.error('[Tool:generate_image] failed: %s', e, exc_info=True)
            result = {'ok': False, 'error': str(e)}

        elapsed = _time.time() - t0
        model_used = result.get('model', '?')

        if result.get('ok'):
            image_b64 = result.get('image_b64', '')
            mime_type = result.get('mime_type', 'image/png')
            data_uri = f'data:{mime_type};base64,{image_b64}'
            text_response = result.get('text', '')

            logger.info('[Tool:generate_image] ✓ model=%s ar=%s %.1fs b64_len=%d rn=%d',
                        model_used, aspect_ratio, elapsed, len(image_b64), rn)

            saved_url = _save_image_to_disk(image_b64, mime_type) if image_b64 else ''

            project_save_path = ''
            if output_path and project_path and project_enabled and image_b64:
                project_save_path = _save_image_to_project(
                    image_b64, mime_type, output_path, project_path,
                    conv_id=task.get('convId'),
                    task_id=task.get('id'),
                )

            # ── SVG conversion (optional) ──
            svg_saved_url = ''
            svg_project_path = ''
            if svg_convert and image_b64:
                svg_saved_url, svg_project_path = _convert_to_svg(
                    saved_url, project_save_path, project_path,
                    conv_id=task.get('convId'), task_id=task.get('id'),
                )

            meta = {
                'toolName': 'generate_image',
                'imageDataUri': data_uri,
                'imagePrompt': prompt,
                'imageAspectRatio': aspect_ratio,
                'imageResolution': resolution,
                'imageModel': model_used,
                'imageText': text_response,
                'badge': f'✓ {model_used}',
            }
            if saved_url:
                meta['imageSavedUrl'] = saved_url
            if project_save_path:
                meta['imageProjectPath'] = project_save_path
            if svg_saved_url:
                meta['svgSavedUrl'] = svg_saved_url
            if svg_project_path:
                meta['svgProjectPath'] = svg_project_path
            _finalize_tool_round(task, rn, round_entry, [meta])

            fallback_parts = [f'Image generated successfully using {model_used}.']
            if text_response:
                fallback_parts.append(f'Model response: {text_response}')
            fallback_parts.append(f'Prompt: {prompt[:200]}')
            fallback_parts.append(f'Aspect ratio: {aspect_ratio}, Resolution: {resolution}')
            if project_save_path:
                fallback_parts.append(f'Image saved to project path: {project_save_path}')
            elif output_path and not project_save_path:
                if not project_enabled:
                    fallback_parts.append(
                        f'Note: output_path="{output_path}" was specified but no project is active. '
                        'Image was saved to the server uploads folder only.'
                    )
                else:
                    fallback_parts.append(
                        f'Note: Failed to save image to project path "{output_path}". '
                        'Image was saved to the server uploads folder instead.'
                    )
            if svg_project_path:
                fallback_parts.append(f'SVG version saved to project path: {svg_project_path}')
            elif svg_saved_url:
                fallback_parts.append(f'SVG version saved to: {svg_saved_url}')

            tool_content = {
                '__screenshot__': True,
                'dataUrl': data_uri,
                'format': mime_type.split('/')[-1],
                'originalSize': len(image_b64) * 3 // 4,
                'compressedSize': len(image_b64) * 3 // 4,
                'compressionApplied': False,
                '_text_fallback': '\n'.join(fallback_parts),
            }
            return tc_id, tool_content, False
        else:
            error_msg = result.get('error', 'Unknown error')
            logger.warning('[Tool:generate_image] ✗ model=%s error=%s %.1fs rn=%d',
                           model_used, error_msg[:200], elapsed, rn)

            meta = {
                'toolName': 'generate_image',
                'imagePrompt': prompt,
                'imageError': error_msg,
                'imageAspectRatio': aspect_ratio, 'imageResolution': resolution,
                'badge': '❌ failed',
            }
            _finalize_tool_round(task, rn, round_entry, [meta])

            tool_content = f'Image generation failed: {error_msg}'
            return tc_id, tool_content, False
