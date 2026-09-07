# HOT_PATH
"""Public entry point: wire the ``generate_image`` handler onto a registry.

Composes the sibling submodules (``_resolve`` / ``_thumbnail`` / ``_save`` /
``_svg``) into the tool handler. This is the top of the package import graph.
"""

from __future__ import annotations

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.executor_image._resolve import (
    _extract_image_gen_history,
    _resolve_source_image,
)
from lib.tasks_pkg.executor_image._save import (
    _save_image_to_disk,
    _save_image_to_project,
)
from lib.tasks_pkg.executor_image._svg import _convert_to_svg
from lib.tasks_pkg.executor_image._thumbnail import _downsize_for_llm

logger = get_logger(__name__)


def register_image_gen_handler(tool_registry, IMAGE_GEN_TOOL_NAMES, _finalize_tool_round, append_event):
    """Register the generate_image handler on the given tool registry.

    Called from executor.py to wire up the handler without circular imports.
    """

    @tool_registry.tool_set(IMAGE_GEN_TOOL_NAMES, category='image_gen',
                            description='Generate an image from a text prompt')
    def _handle_generate_image(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
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
                'badge': 'failed',
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
        # Keep a render-friendly reference to the source image for the edit
        # card's before→after strip — only when it's a URL the browser can
        # load directly (skip data-URIs / server paths to avoid meta bloat).
        source_display_url = (
            source_image
            if (is_edit and source_image.startswith(('/api/images/', 'http://', 'https://')))
            else ''
        )
        image_mode = 'edit' if is_edit else 'generate'

        # ── Emit progress event ──
        round_entry['status'] = 'running'
        badge_text = 'editing…' if is_edit else 'generating…'
        round_entry['results'] = [{
            'toolName': 'generate_image',
            'imagePrompt': prompt[:100],
            'imageAspectRatio': aspect_ratio, 'imageResolution': resolution,
            'imageMode': image_mode,
            'imageSourceUrl': source_display_url,
            'badge': badge_text,
        }]
        append_event(task, build_event(EventType.TOOL_RESULT, roundNum=rn,
                            query=round_entry['query'], results=round_entry['results']))

        # ── Extract image gen history ──
        history = _extract_image_gen_history(task, messages=task.get('messages'))
        if history:
            logger.info('[Tool:generate_image] Found %d prior image gen rounds for multi-turn',
                        len(history))

        t0 = _time.time()
        logger.info('[Tool:generate_image] prompt="%.80s" ar=%s res=%s output=%s rn=%d history=%d edit=%s',
                    prompt[:80], aspect_ratio, resolution, output_path or '(none)', rn,
                    len(history), is_edit)

        # ── 429 progress callback — update badge so user sees rate-limit status ──
        def _on_429(retry_count):
            badge_429 = 'rate limited, retrying (#%d)…' % retry_count
            round_entry['results'] = [{
                'toolName': 'generate_image',
                'imagePrompt': prompt[:100],
                'imageAspectRatio': aspect_ratio, 'imageResolution': resolution,
                'imageMode': image_mode,
                'imageSourceUrl': source_display_url,
                'badge': badge_429,
            }]
            append_event(task, build_event(EventType.TOOL_RESULT, roundNum=rn,
                                query=round_entry['query'], results=round_entry['results']))

        try:
            with log_context('generate_image_tool', logger=logger):
                from lib.tasks_pkg.manager import task_user_id
                result = generate_image(
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    history=history or None,
                    source_images=source_images,
                    on_429=_on_429,
                    owner_user_id=task_user_id(task),
                    tenant_id=task.get('_tenant_id'),
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
            project_save_base = ''
            project_save_rel = ''
            if output_path and project_path and project_enabled and image_b64:
                project_save_path, project_save_base, project_save_rel = _save_image_to_project(
                    image_b64, mime_type, output_path, project_path,
                    conv_id=task.get('convId'),
                    task_id=task.get('id'),
                )

            # ── SVG conversion (optional) ──
            svg_saved_url = ''
            svg_project_path = ''
            svg_failed = False
            if svg_convert and image_b64:
                svg_saved_url, svg_project_path = _convert_to_svg(
                    saved_url, project_save_rel, project_save_base or project_path,
                    conv_id=task.get('convId'), task_id=task.get('id'),
                )
                # Requested SVG but nothing came back → surface it, don't hide it.
                svg_failed = not (svg_saved_url or svg_project_path)

            _429_retries = result.get('_429_count', 0)
            _badge_suffix = ' (429×%d)' % _429_retries if _429_retries else ''
            if svg_failed:
                _badge_suffix += ' ⚠ svg failed'
            meta = {
                'toolName': 'generate_image',
                'imageDataUri': data_uri,
                'imagePrompt': prompt,
                'imageAspectRatio': aspect_ratio,
                'imageResolution': resolution,
                'imageModel': model_used,
                'imageText': text_response,
                'imageMode': image_mode,
                'badge': f'✓ {model_used}{_badge_suffix}',
            }
            if source_display_url:
                meta['imageSourceUrl'] = source_display_url
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
            # Surface a reusable reference so the model can EDIT this image on a
            # later turn by passing it back as `source_image`. Without this the
            # model has no URL/path to reference and always re-generates from
            # scratch. Prefer the project path; fall back to the uploads URL.
            _edit_ref = project_save_path or saved_url
            if _edit_ref:
                fallback_parts.append(
                    f'To EDIT this image later (recolor, change background, add/remove '
                    f'objects, restyle, etc.), call generate_image again with '
                    f'source_image="{_edit_ref}" and describe the change in the prompt.'
                )
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
            elif svg_failed:
                fallback_parts.append(
                    'Note: SVG conversion was requested (svg=true) but failed — only the '
                    'PNG was saved. See server logs ([Tool:generate_image] SVG …) for details.'
                )

            # ── Build downsized copy for the chat LLM wire ──
            # The full-res data_uri stays in meta.imageDataUri (frontend render +
            # intra-turn history) and on disk / in the project. Only the chat-LLM
            # tool_content gets the thumbnail, keeping wire bytes under the
            # gateway's ~4 MB cap regardless of requested resolution.
            thumb_b64, thumb_mime = _downsize_for_llm(image_b64, mime_type) if image_b64 else (image_b64, mime_type)
            thumb_data_uri = f'data:{thumb_mime};base64,{thumb_b64}' if thumb_b64 else data_uri
            _orig_bytes = len(image_b64) * 3 // 4
            _thumb_bytes = len(thumb_b64) * 3 // 4 if thumb_b64 else _orig_bytes
            _compression_applied = (thumb_b64 != image_b64) if thumb_b64 else False

            tool_content = {
                '__screenshot__': True,
                'dataUrl': thumb_data_uri,
                'format': thumb_mime.split('/')[-1],
                'originalSize': _orig_bytes,
                'compressedSize': _thumb_bytes,
                'compressionApplied': _compression_applied,
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
                'imageMode': image_mode,
                'imageSourceUrl': source_display_url,
                'badge': 'failed',
            }
            _finalize_tool_round(task, rn, round_entry, [meta])

            tool_content = f'Image generation failed: {error_msg}'
            return tc_id, tool_content, False
