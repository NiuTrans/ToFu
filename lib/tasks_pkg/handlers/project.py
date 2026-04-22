# HOT_PATH
"""Project file tool handler: read/write/search/grep/run project files."""

from __future__ import annotations

import os

from lib.log import get_logger
from lib.tasks_pkg.executor import (
    _build_simple_meta,
    _finalize_tool_round,
    _resolve_content_ref,
    tool_registry,
)
from lib.tasks_pkg.handlers.code_exec import _make_stdin_callback
from lib.tools import PROJECT_TOOL_NAMES, build_project_tool_meta

logger = get_logger(__name__)


# Register read_files independently — it's a global tool that works with or
# without a project path (absolute paths route to lib.file_reader.read_local_file
# for PDFs/images/Office docs/text). The shared _handle_project_tool handles
# it uniformly; the no-project branch below routes via an anchor cwd of '.'.
@tool_registry.tool('read_files', category='files',
                    description='Read one or more files (relative or absolute)')
@tool_registry.tool_set(PROJECT_TOOL_NAMES, category='project',
                        description='Read/write/search project files')
def _handle_project_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    # ── content_ref resolution for write_file ──
    if fn_name == 'write_file' and 'content_ref' in fn_args and 'content' not in fn_args:
        ref = fn_args.pop('content_ref')
        resolved = _resolve_content_ref(task, ref)
        if resolved is None:
            ref_round = ref.get('tool_round', '?')
            error_msg = (
                f'Error: content_ref resolution failed — tool_round={ref_round} '
                f'not found or has no content. Provide explicit content instead.'
            )
            logger.warning('[Tool:write_file] content_ref resolution failed: ref=%s task=%s',
                           ref, task.get('id', '?')[:8])
            meta = build_project_tool_meta(fn_name, fn_args, error_msg)
            meta['badge'] = 'ref failed'
            _finalize_tool_round(task, rn, round_entry, [meta])
            return tc_id, error_msg, False
        fn_args['content'] = resolved
        logger.info('[Tool:write_file] content_ref resolved: tool_round=%s → %d chars, '
                    'path=%s task=%s',
                    ref.get('tool_round'), len(resolved),
                    fn_args.get('path', '?'), task.get('id', '?')[:8])

    from lib.project_mod import execute_tool
    # read_files is globally available — when no project is attached, absolute
    # paths still work (routed inside tool_read_files via lib.file_reader);
    # project-relative paths error out with a helpful message.
    if fn_name == 'read_files' and not project_path:
        tool_content = execute_tool(fn_name, fn_args, '.', conv_id=task['convId'], task_id=task['id'])
    else:
        _stdin_cb = _make_stdin_callback(task, rn, round_entry, fn_args.get('command', '')) if fn_name == 'run_command' else None
        _extra_kw = {'stdin_callback': _stdin_cb} if _stdin_cb else {}
        if fn_name == 'run_command':
            _extra_kw['task'] = task  # enable cooperative abort of subprocesses
        tool_content = execute_tool(fn_name, fn_args, project_path, conv_id=task['convId'], task_id=task['id'], **_extra_kw) if project_path else 'Error: No project path.'

    # read_files with absolute image paths returns a batch dict with __batch_images__
    is_batch_image = isinstance(tool_content, dict) and tool_content.get('__batch_images__')
    if is_batch_image:
        # Extract the first image for VLM upload, keep text content
        _images = tool_content['__batch_images__']
        _text = tool_content.get('_text_content', '')
        # Use the first image as the primary screenshot result
        first_img = next(iter(_images.values()))
        tool_content = first_img
        # Store the text content as fallback
        if _text:
            tool_content['_text_fallback'] = _text

    # read_files may return a __screenshot__ dict for images (single absolute image path)
    is_image_result = isinstance(tool_content, dict) and tool_content.get('__screenshot__')
    if is_image_result:
        tool_content.get('_text_fallback', '') or 'Image loaded.'
        file_path = fn_args.get('path', '?')
        filename = os.path.basename(file_path)
        fmt = tool_content.get('format', 'png')
        orig_size = tool_content.get('originalSize', 0)
        comp_size = tool_content.get('compressedSize', 0)
        size_info = f'{comp_size:,} bytes'
        if tool_content.get('compressionApplied') and orig_size:
            size_info = f'{orig_size:,} → {comp_size:,} bytes (compressed)'
        meta = {
            'toolName': fn_name, 'title': f'🖼️ {filename}',
            'snippet': f'{filename} ({fmt}, {size_info})',
            'source': 'Project', 'fetched': True,
            'fetchedChars': comp_size, 'url': '',
            'badge': f'🖼️ {fmt}',
        }
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, tool_content, False

    try:
        meta = build_project_tool_meta(fn_name, fn_args, tool_content)
    except Exception as e:
        logger.warning('[Executor] build_project_tool_meta failed for %s: %s', fn_name, e, exc_info=True)
        meta = _build_simple_meta(
            fn_name, tool_content, source='Project',
            snippet=f'{fn_name} (meta build error)',
            extra={'url': ''},
        )

    # For run_command: inject fileChanges from tracked modifications
    if fn_name == 'run_command' and project_path:
        try:
            from lib.project_mod.modifications import get_modifications
            task_mods = [m for m in get_modifications(project_path, conv_id=task.get('convId'))
                         if m.get('taskId') == task.get('id') and m.get('type') == 'run_command']
            if task_mods:
                file_changes = []
                for m in task_mods:
                    p = m.get('path', '')
                    existed = m.get('existed', True)
                    if not existed:
                        action = 'created'
                    elif 'originalContent' in m:
                        import os as _os
                        abs_p = _os.path.join(project_path, p) if not _os.path.isabs(p) else p
                        action = 'deleted' if not _os.path.exists(abs_p) else 'modified'
                    else:
                        action = 'modified'
                    file_changes.append({'path': p, 'action': action})
                meta['fileChanges'] = file_changes
        except Exception as e:
            logger.debug('[Executor] run_command fileChanges enrichment failed: %s', e)

    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
