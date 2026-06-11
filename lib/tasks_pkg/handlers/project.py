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
from lib.tasks_pkg.handlers.code_exec import (
    _make_run_command_progress_cb,
    _make_stdin_callback,
)
from lib.tasks_pkg.handlers._read_gate import (
    check_read_before_edit,
    partition_batch_edits,
)
from lib.tools import PROJECT_TOOL_NAMES, build_project_tool_meta

logger = get_logger(__name__)


# ── Per-feature size cap for write_file artifacts ─────────────────────
# Smaller than the lib.artifacts hard cap (8 MiB) so write_file specifically
# rejects giant blobs early, before reading the file back from disk.
_WRITE_ARTIFACT_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB


def _screenshot_to_descriptor(img, filename=''):
    """Turn a ``__screenshot__`` dict into a frontend-render descriptor.

    Returns a dict ``{uri, format, filename}`` where ``uri`` is the full
    ``data:<mime>;base64,...`` string the browser can drop straight into an
    ``<img src>``. ``uri`` is empty when the dict lacks a usable data URL.
    """
    if not isinstance(img, dict):
        return {'uri': '', 'format': '', 'filename': filename}
    return {
        'uri': img.get('dataUrl', '') or '',
        'format': img.get('format', 'png'),
        'filename': filename,
    }


def _maybe_promote_write_to_artifact(task, fn_name, fn_args, project_path, meta):
    """Detect renderable writes (md/html/svg) and persist + emit artifact event.

    Mutates ``meta`` in place to attach ``artifactId`` so the chat-side
    tool round can render an "Open in panel" chip.

    This is best-effort: any failure (DB unavailable, read race, oversize)
    logs and degrades to "no artifact, just the normal tool round".
    """
    if fn_name not in ('write_file', 'apply_diff', 'apply_diffs', 'insert_content', 'insert_contents'):
        return
    rel_path = (fn_args.get('path') or '').strip()
    if not rel_path:
        return

    # Feature-flag gate — read live from lib at call time so a hot
    # config reload disables / re-enables without a restart.
    try:
        import lib as _lib_mod
        if not getattr(_lib_mod, 'ARTIFACTS_ENABLED', True):
            return
    except Exception as e:
        logger.debug('[Artifacts] feature flag check failed (non-fatal): %s', e)

    try:
        from lib.artifacts import (
            ArtifactSizeError,
            create_artifact,
            detect_format,
            emit_artifact_event,
            is_renderable_path,
        )
    except Exception as e:  # pragma: no cover — defensive import guard
        logger.debug('[Artifacts] subsystem unavailable: %s', e)
        return

    if not is_renderable_path(rel_path):
        return

    fmt = detect_format(rel_path)
    if fmt is None:
        return

    # Routine project doc edits (README.md, CHANGELOG.md, etc.) shouldn't
    # spawn artifact chips — only HTML/SVG outputs are "report-shaped"
    # enough to deserve the side panel.  Inline ```markdown fences are
    # still promoted by Producer B (lib/artifacts/scanner.py).
    if fmt == 'markdown':
        return

    # Resolve the on-disk target so we capture the canonical bytes (post
    # write).  ``apply_diff`` / ``insert_content`` lacks a `content` arg —
    # we always read from disk.
    try:
        from lib.project_mod.write_tools import _resolve_write_path
        target = _resolve_write_path(project_path, rel_path)
    except Exception as e:
        # _resolve_write_path raises ValueError when an absolute path
        # falls outside any registered root.  Either way, we can't safely
        # promote it.
        logger.debug('[Artifacts] cannot resolve write path %s: %s', rel_path, e)
        return

    try:
        size = os.path.getsize(target)
    except OSError as e:
        logger.debug('[Artifacts] stat after write failed for %s: %s', rel_path, e)
        return

    if size > _WRITE_ARTIFACT_MAX_BYTES:
        logger.info(
            '[Artifacts] skip oversize write_file artifact path=%s size=%d cap=%d',
            rel_path, size, _WRITE_ARTIFACT_MAX_BYTES,
        )
        return

    try:
        with open(target, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except OSError as e:
        logger.warning('[Artifacts] read-back failed for %s: %s', rel_path, e)
        return

    conv_id = task.get('convId') or ''
    if not conv_id:
        logger.debug('[Artifacts] task without convId, skipping')
        return

    msg_id = task.get('_assistantMsgId') or task.get('msgId') or ''
    title = os.path.basename(rel_path) or rel_path
    src_ref = {'path': rel_path, 'tool': fn_name}

    try:
        artifact_meta = create_artifact(
            conv_id=conv_id,
            content=content,
            format=fmt,
            source='write_file',
            source_ref=src_ref,
            task_id=task.get('id', ''),
            msg_id=msg_id,
            title=title,
        )
    except ArtifactSizeError as e:
        logger.info('[Artifacts] write_file size cap rejection: %s', e)
        return
    except Exception as e:
        logger.warning('[Artifacts] create_artifact failed for %s: %s',
                       rel_path, e, exc_info=True)
        return

    try:
        emit_artifact_event(task, artifact_meta)
    except Exception as e:
        logger.warning('[Artifacts] emit_artifact_event failed for id=%s: %s',
                       artifact_meta.get('id', '?')[:8], e, exc_info=True)

    # Annotate the chat-side meta so the tool round can render the chip.
    meta['artifactId']     = artifact_meta['id']
    meta['artifactFormat'] = artifact_meta['format']
    meta['artifactTitle']  = artifact_meta['title']
    meta['artifactSize']   = artifact_meta['size_bytes']
    logger.debug(
        '[Artifacts] promoted write_file path=%s id=%s fmt=%s size=%d',
        rel_path, artifact_meta['id'][:8], artifact_meta['format'], size,
    )


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

    # ── Read-before-edit gate ──
    # apply_diff / insert_content built from guessed content are the dominant
    # source of "Search text not found" failures. Refuse them unless the
    # target file was read (or written) earlier in the conversation. See
    # lib/tasks_pkg/handlers/_read_gate.py for the policy.
    #
    # Single-edit tools (apply_diff / insert_content) are refused wholesale.
    # Batch tools (apply_diffs / insert_contents) are gated per-path: only the
    # edits whose target file is unread are skipped, the rest run, and the
    # result names the skipped file(s).
    _gate_skip_note = None
    if fn_name in ('apply_diff', 'insert_content') and project_path:
        try:
            _gate_err = check_read_before_edit(task, fn_name, fn_args, project_path)
        except Exception as _ge:
            logger.warning('[ReadGate] check failed for %s (allowing through): %s',
                           fn_name, _ge, exc_info=True)
            _gate_err = None
        if _gate_err:
            meta = build_project_tool_meta(fn_name, fn_args, _gate_err)
            meta['badge'] = 'read first'
            _finalize_tool_round(task, rn, round_entry, [meta])
            return tc_id, _gate_err, False
    elif fn_name in ('apply_diffs', 'insert_contents') and project_path:
        try:
            _skip_idx, _unread_raw = partition_batch_edits(task, fn_name, fn_args, project_path)
        except Exception as _ge:
            logger.warning('[ReadGate] partition failed for %s (allowing through): %s',
                           fn_name, _ge, exc_info=True)
            _skip_idx, _unread_raw = [], []
        if _skip_idx:
            edits = fn_args.get('edits') or []
            _skip_set = set(_skip_idx)
            # All edits target unread files → full refusal (nothing to run).
            if len(_skip_set) >= len(edits):
                from lib.tasks_pkg.handlers._read_gate import _format_refusal
                _gate_err = _format_refusal(fn_name, _unread_raw)
                meta = build_project_tool_meta(fn_name, fn_args, _gate_err)
                meta['badge'] = 'read first'
                _finalize_tool_round(task, rn, round_entry, [meta])
                logger.info('[ReadGate] Refused all %d edit(s) of %s for unread file(s) %s (task=%s)',
                            len(edits), fn_name, ', '.join(_unread_raw), task.get('id', '?')[:8])
                return tc_id, _gate_err, False
            # Partial: drop the unread-target edits, keep the rest.
            fn_args['edits'] = [e for i, e in enumerate(edits) if i not in _skip_set]
            _gate_skip_note = (
                f'Read-before-edit gate: skipped {len(_skip_set)} edit(s) targeting '
                f'unread file(s): {", ".join(_unread_raw)}. The remaining '
                f'{len(fn_args["edits"])} edit(s) were applied. read_files those '
                f'path(s) this turn, then re-issue the skipped edit(s) NEXT turn '
                f'(a sibling read_files in the same parallel batch does not count).'
            )
            logger.info('[ReadGate] Partial %s: skipped %d/%d edit(s) for unread file(s) %s (task=%s)',
                        fn_name, len(_skip_set), len(edits), ', '.join(_unread_raw),
                        task.get('id', '?')[:8])

    from lib.project_mod import execute_tool
    from lib.project_mod.abs_path_guard import (
        reset_restricted, set_restricted, task_is_remote,
    )
    # Remote API callers (agents:run / chat keys, compat adapters) must not
    # use absolute / ~ paths to read or write outside a registered workspace
    # root. Cookie-auth UI and the local CLI are unaffected (task_is_remote
    # is False for them). See lib/project_mod/abs_path_guard.py.
    _abs_token = set_restricted(task_is_remote(task))
    try:
        # read_files is globally available — when no project is attached,
        # absolute paths still work (routed inside tool_read_files via
        # lib.file_reader); project-relative paths error out helpfully.
        if fn_name == 'read_files' and not project_path:
            tool_content = execute_tool(fn_name, fn_args, '.', conv_id=task['convId'], task_id=task['id'])
        else:
            _progress_cb = None
            _extra_kw = {}
            if fn_name == 'run_command':
                _cmd = fn_args.get('command', '') or ''
                _stdin_cb = _make_stdin_callback(task, rn, round_entry, _cmd)
                _progress_cb = _make_run_command_progress_cb(task, rn, round_entry, _cmd)
                _extra_kw = {
                    'stdin_callback': _stdin_cb,
                    'on_chunk': _progress_cb,
                    'task': task,  # enable cooperative abort of subprocesses
                }
            try:
                tool_content = (execute_tool(fn_name, fn_args, project_path,
                                             conv_id=task['convId'], task_id=task['id'],
                                             **_extra_kw)
                                if project_path else 'Error: No project path.')
            finally:
                # Flush any buffered run_command output tail.
                if _progress_cb is not None:
                    try:
                        _progress_cb.flush()
                    except Exception as e:
                        logger.debug('[run_command] progress flush failed: %s', e)
    finally:
        reset_restricted(_abs_token)

    # read_files with absolute image paths returns a batch dict with __batch_images__
    _img_descriptors = None  # frontend-render image list (all images in a batch)
    is_batch_image = isinstance(tool_content, dict) and tool_content.get('__batch_images__')
    if is_batch_image:
        # Extract the first image for VLM upload, keep text content
        _images = tool_content['__batch_images__']
        _text = tool_content.get('_text_content', '')
        # Capture every image's data URI for inline rendering before we
        # collapse to a single dict (only the first goes to the VLM wire).
        _img_descriptors = [_screenshot_to_descriptor(img) for img in _images.values()]
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
        if _img_descriptors is None:
            _img_descriptors = [_screenshot_to_descriptor(tool_content, filename)]
        meta = {
            'toolName': fn_name, 'title': f'🖼️ {filename}',
            'snippet': f'{filename} ({fmt}, {size_info})',
            'source': 'Project', 'fetched': True,
            'fetchedChars': comp_size, 'url': '',
            'badge': f'🖼️ {fmt}',
            # Inline-render payload — frontend (tool_rounds.js) draws an
            # <img> per descriptor. Each carries a full data: URL.
            'imageDataUris': [d for d in _img_descriptors if d.get('uri')],
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

    if _gate_skip_note:
        meta['badge'] = 'partial: read first'

    # ── Promote renderable writes to a chat artifact ──
    # Best-effort: failure here MUST NOT fail the tool round itself.
    if fn_name in ('write_file', 'apply_diff', 'apply_diffs', 'insert_content', 'insert_contents') and project_path:
        try:
            _maybe_promote_write_to_artifact(task, fn_name, fn_args, project_path, meta)
        except Exception as e:
            logger.debug('[Artifacts] promotion path failed (non-fatal): %s',
                         e, exc_info=True)

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

    # Prepend the read-gate skip note so the model sees which path(s) were
    # dropped. Done AFTER meta is built so the per-edit summaries parse the
    # unmodified batch header ("Applied N/M edits").
    if _gate_skip_note and isinstance(tool_content, str):
        tool_content = f'{_gate_skip_note}\n\n{tool_content}'

    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
