# HOT_PATH
"""Project file tool handler: read/write/search/grep/run project files."""

from __future__ import annotations

import os
import threading

from lib.log import get_logger
from lib.tasks_pkg.executor import (
    _build_simple_meta,
    _finalize_tool_round,
    _resolve_content_ref,
    tool_registry,
)
from lib.tasks_pkg.handlers.code_exec import (
    _make_grep_intercept_cb,
    _make_run_command_progress_cb,
    _make_run_command_spawn_cb,
    _make_stdin_callback,
)
from lib.tasks_pkg.handlers._read_gate import (
    _collect_target_paths,
    check_read_before_edit,
    partition_batch_edits,
)
from lib.tasks_pkg.handlers._write_freshness_gate import (
    check_write_freshness,
    partition_stale_edits,
    record_read_paths,
)
from lib.desktop.remote import remote_worktree_binding
from lib.tools.meta import build_project_tool_meta
from lib.tools.project import PROJECT_TOOL_NAMES
from lib.tools.result_projection import (
    TOOL_RESULT_PRODUCER_METADATA_KEY,
    TOOL_RESULT_PROJECTION_ITEMS_KEY,
)

logger = get_logger(__name__)


# PROJECT_TOOL_NAMES is a schema/disclosure group, not an execution-owner set.
# Background controls belong to code_exec even in project/remote sessions;
# registering them here would evict those built-in exact bindings at import.
PROJECT_HANDLER_TOOL_NAMES = frozenset(PROJECT_TOOL_NAMES) - {
    'check_background_command', 'kill_background_command',
}


def _record_producer_result_metadata(fn_name, content, round_entry):
    """Carry producer-side omission evidence to the settlement boundary."""
    if fn_name != 'grep_search' or not isinstance(round_entry, dict):
        return
    from lib.project_mod.read_tools import grep_result_was_truncated

    if grep_result_was_truncated(content):
        round_entry[TOOL_RESULT_PRODUCER_METADATA_KEY] = {
            'status': 'partial',
            'truncated': True,
        }


# ── RWA P3:远程工作树路由(拍板 3A 同名策略) ──
# 会话绑定 cfg['project_remote'] = {agent_id, root}(总闸
# TOFU_REMOTE_WORKTREE)时,同名项目工具翻译为 project_<fn> 命令按
# agent_id 寻址入队,在用户的本地机器上执行。
_REMOTE_CMD_MAP = {
    'list_dir': 'project_list_dir',
    'read_files': 'project_read_files',
    'write_file': 'project_write_file',
    'apply_diff': 'project_apply_diff',
    'edit_file': 'project_edit_file',
    'grep_search': 'project_grep_search',
    'find_files': 'project_find_files',
    'run_command': 'project_run_command',
}


def _execute_remote_run_command(task, tc_id, fn_args, rn, round_entry,
                                remote):
    """Remote run_command with LIVE output (RWA P4b-2b).

    The bridge stream frames (agent → poll → :func:`lib.desktop.resolve_streams`)
    are fanned into the SAME ``tool_progress`` channel the server-side
    run_command uses (:func:`_make_run_command_progress_cb`), so the chat's
    terminal block renders remote output incrementally with ZERO frontend
    changes. ``cmd_id`` is minted up front so the watcher can follow the
    command's stream while the blocking RPC wait runs.
    """
    import uuid as _uuid

    from lib.desktop import (
        format_desktop_result, get_command_stream, send_desktop_command)
    from lib.tasks_pkg.handlers.code_exec import _make_run_command_progress_cb

    command = fn_args.get('command', '')
    round_entry.setdefault('toolCallId', tc_id)
    round_entry.setdefault('toolName', 'run_command')

    def _finish(text, extra=None, badge=''):
        meta = {
            'toolName': 'run_command',
            'command': command,
            'output': (text or ''),
            'source': f'Remote:{remote["agent_id"][:8]}',
            'remoteRoot': remote['root'],
        }
        if badge:
            meta['badge'] = badge
        meta.update(extra or {})
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, text, False

    if 'credentials' in fn_args:
        text = ('Error: server vault credentials are unavailable for remote '
                'worktree commands; configure credentials on the desktop '
                'agent or run the command on the server workspace.')
        meta = {
            'toolName': 'run_command', 'command': command, 'output': text,
            'source': f'Remote:{remote["agent_id"][:8]}',
            'remoteRoot': remote['root'], 'badge': 'credential unavailable',
            'exitCode': 'not-run', 'notRun': True,
        }
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, text, False


    from lib.project_mod.run_command import (
        parse_safe_directory_listing_command,
        parse_safe_file_find_command,
    )
    import posixpath

    from lib.project_mod.tool_admission import Cancellation

    working_dir = str(fn_args.get('working_dir') or '').strip()

    def _within_working_dir(path):
        if not working_dir:
            return path
        return posixpath.normpath(posixpath.join(
            working_dir, '' if path == '.' else path))

    directory_listing = (
        parse_safe_directory_listing_command(command)
        if os.environ.get('TOFU_RUN_LS_FASTPATH', '1') != '0' else None)
    if directory_listing is not None:
        listing_path = _within_working_dir(directory_listing['path'])
        params = {
            'root': remote['root'],
            'path': listing_path,
            'show_hidden': directory_listing['show_hidden'],
            'shell_compatible': True,
        }
        logger.info(
            '[Remote] run_command routed plain ls → project_list_dir @%s:%s',
            remote['agent_id'][:8], remote['root'])
        result, error = send_desktop_command(
            'project_list_dir', params, timeout=None,
            target_agent_id=remote['agent_id'],
            user_id=task.get('_userId', '') or '',
            cancellation=Cancellation(task))
        if error:
            return _finish(
                f'Error: remote worktree {remote["root"]}: {error}',
                {'exitCode': 'error', 'routedAs': 'project_list_dir'},
                badge='remote error')
        if isinstance(result, dict) and result.get('error'):
            logger.info(
                '[Remote] bounded ls declined operand; retaining shell: %s',
                result['error'])
        else:
            text = format_desktop_result(
                'project_list_dir', result if isinstance(result, dict) else {})
            return _finish(
                text,
                {'exitCode': 0, 'routedAs': 'project_list_dir'},
                badge='directory')

    file_find = (
        parse_safe_file_find_command(command)
        if os.environ.get('TOFU_RUN_FIND_FASTPATH', '1') != '0' else None)
    # Only the legacy subset (single pattern, -type f, unbounded depth, no
    # pipe tail) maps onto the remote project_find_files delegate; broader
    # parsed shapes (unions, -type d, depth bounds, head/sort/wc tails) keep
    # the shell path below.
    if file_find is not None and file_find.get('remote_safe'):
        find_path = _within_working_dir(file_find['path'])
        params = {
            'root': remote['root'],
            'path': find_path,
            'pattern': file_find['pattern'],
            'max_results': file_find['max_results'],
            'case_sensitive': file_find['case_sensitive'],
            'shell_output': True,
            'respect_project_ignores': False,
        }
        logger.info(
            '[Remote] run_command routed bounded find → project_find_files '
            '@%s:%s', remote['agent_id'][:8], remote['root'])
        result, error = send_desktop_command(
            'project_find_files', params, timeout=None,
            target_agent_id=remote['agent_id'],
            user_id=task.get('_userId', '') or '',
            cancellation=Cancellation(task))
        if error:
            return _finish(
                f'Error: remote worktree {remote["root"]}: {error}',
                {'exitCode': 'error', 'routedAs': 'project_find_files'},
                badge='remote error')
        if isinstance(result, dict) and result.get('error'):
            logger.info(
                '[Remote] bounded find declined operand; retaining shell: %s',
                result['error'])
        else:
            text = (
                str(result.get('files') or '')
                if isinstance(result, dict) else format_desktop_result(
                    'project_find_files', result))
            return _finish(
                text,
                {'exitCode': 0, 'routedAs': 'project_find_files'},
                badge='files')

    cmd_id = _uuid.uuid4().hex
    # Worker execution owns an explicit timeout; bridge transport waits for its
    # observed outcome rather than inventing a second execution deadline.
    bridge_timeout = None

    progress_cb = _make_run_command_progress_cb(task, rn, round_entry, command)
    seen = {'stdout': 0, 'stderr': 0}
    stop = threading.Event()

    def _drain_once():
        stream = get_command_stream(cmd_id)
        if not stream:
            return False
        for name in ('stdout', 'stderr'):
            text = stream.get(name) or ''
            if len(text) > seen[name]:
                progress_cb(name, text[seen[name]:])
                seen[name] = len(text)
        return bool(stream.get('done'))

    def _watch():
        while not stop.is_set():
            if _drain_once():
                return
            stop.wait(0.25)  # 与 tool_progress 200ms 合并节奏同量级

    params = {k: v for k, v in fn_args.items()
              if k not in ('content_ref', 'timeout_s', 'timeout')}
    explicit_timeout = fn_args.get('timeout_s', fn_args.get('timeout'))
    if explicit_timeout is not None:
        params['timeout'] = explicit_timeout
    params['root'] = remote['root']
    logger.info('[Remote] run_command streaming @%s:%s cmd=%s (task=%s)',
                remote['agent_id'][:8], remote['root'], cmd_id[:8],
                task.get('id', '?')[:8])
    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()
    try:
        result, error = send_desktop_command(
            'project_run_command', params, timeout=bridge_timeout,
            target_agent_id=remote['agent_id'],
            user_id=task.get('_userId', '') or '', cmd_id=cmd_id,
            cancellation=Cancellation(task))
    finally:
        stop.set()
        watcher.join()
    _drain_once()          # 尾帧:wait 返回后可能还有最后一批
    progress_cb.flush()

    if error:
        return _finish(f'Error: remote worktree {remote["root"]}: {error}',
                       {'exitCode': 'error'}, badge='remote error')
    result = result if isinstance(result, dict) else {}
    text = format_desktop_result('project_run_command', result)
    output = (result.get('stdout') or '')
    if result.get('stderr'):
        output += ('\n[stderr]\n' if output else '[stderr]\n') + result['stderr']
    timed_out = bool(result.get('timed_out'))
    exit_code = result.get('exit_code')
    text_out = output.strip()
    meta_extra = {
        'output': text_out,
        'exitCode': 'timeout' if timed_out
                    else (exit_code if exit_code is not None else '?'),
        'timedOut': timed_out,
    }
    return _finish(text, meta_extra)


def _execute_remote_project_tool(task, fn_name, tc_id, fn_args, rn,
                                 round_entry, remote):
    """Route a project tool call to the bound agent's local root (RWA P3).

    服务器侧 FS 门(read-before-edit / freshness / abs_path_guard)刻意不
    适用 —— agent 对着自己声明的 share_roots 自守同款门(P1 约束⑤)。
    工具名不变(拍板 3A),串行写分区 + Manual 批准门原样继承。
    """
    from lib.desktop import format_desktop_result, send_desktop_command
    from lib.project_mod.tool_admission import Cancellation

    def _finish(text, badge=''):
        meta = build_project_tool_meta(fn_name, fn_args, text)
        meta['source'] = f'Remote:{remote["agent_id"][:8]}'
        meta['remoteRoot'] = remote['root']
        if badge:
            meta['badge'] = badge
        _record_producer_result_metadata(fn_name, text, round_entry)
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, text, False

    if fn_name == 'run_command':
        return _execute_remote_run_command(
            task, tc_id, fn_args, rn, round_entry, remote)

    cmd_type = _REMOTE_CMD_MAP.get(fn_name)
    if cmd_type is None:
        supported = ' / '.join(sorted(_REMOTE_CMD_MAP))
        return _finish(
            f'Error: {fn_name} is not supported on the remote worktree '
            f'({remote["root"]}) yet. Supported: {supported}.',
            badge='remote unsupported')
    if fn_name == 'read_files' and fn_args.get('reads'):
        return _finish(
            'Error: batch read_files (reads=[...]) is not supported on the '
            'remote worktree yet — read one path per call.',
            badge='remote unsupported')
    if fn_name == 'write_file' and fn_args.get('append'):
        # An agent on an older build would silently OVERWRITE — fail closed.
        return _finish(
            'Error: write_file append=true is not supported on the remote '
            f'worktree ({remote["root"]}) yet.',
            badge='remote unsupported')

    params = {k: v for k, v in fn_args.items() if k != 'content_ref'}
    params['root'] = remote['root']
    if fn_name in {'write_file', 'apply_diff', 'edit_file'}:
        params['file_receipts_version'] = 1
    bridge_timeout = None
    logger.info('[Remote] routing %s → %s @%s:%s (task=%s)', fn_name, cmd_type,
                remote['agent_id'][:8], remote['root'], task.get('id', '?')[:8])
    result, error = send_desktop_command(
        cmd_type, params, timeout=bridge_timeout,
        target_agent_id=remote['agent_id'],
        user_id=task.get('_userId', '') or '', cancellation=Cancellation(task))
    if error:
        return _finish(f'Error: remote worktree {remote["root"]}: {error}',
                       badge='remote error')
    if fn_name in {'write_file', 'apply_diff', 'edit_file'}:
        from lib.project_mod.receipts import FileReceiptError, decode_write_receipts
        from lib.tasks_pkg.commit_round._derive import note_remote_file_changes
        batch_path = fn_args.get('path')
        paths = ([(edit.get('path') or batch_path)
                  for edit in fn_args.get('edits', []) if isinstance(edit, dict)]
                 if fn_name == 'edit_file' else [fn_args.get('path')])
        try:
            entries = decode_write_receipts(result, paths)
            note_remote_file_changes(task.get('id'), task.get('_userId'), remote, entries)
        except FileReceiptError:
            logger.warning('[Remote] invalid committed-file receipts for task %s', task.get('id', '')[:8])
    return _finish(format_desktop_result(cmd_type, result))



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
    if fn_name not in ('write_file', 'edit_file', 'apply_diff', 'apply_diffs', 'insert_content', 'insert_contents'):
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
        from lib.artifacts.response_files import read_project_file
        from lib.project_mod.tool_admission import Cancellation
        # The write-path authority already validated this exact target. Reuse
        # the no-follow snapshot reader; its parent is a derived verified root.
        raw = read_project_file(target, [os.path.dirname(target)],
                                cancelled=Cancellation(task).is_set)
        content = raw.decode('utf-8', errors='replace')
        size = len(raw)
    except (OSError, ValueError, RuntimeError) as error:
        logger.warning('[Artifacts] read-back failed: %s', error)
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
@tool_registry.tool('inspect_image', category='files',
                    description='Zoom/rotate/crop view of a local image')
@tool_registry.tool_set(PROJECT_HANDLER_TOOL_NAMES, category='project',
                        description='Read/write/search project files')
def _handle_project_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None,
                         *, repository_scanner=None):

    file_source_capture = None
    if fn_name == 'read_files' and fn_args.get('bind_sources') is True:
        from lib.tasks_pkg.working_file_sources import FileReadCapture
        try:
            file_source_capture = FileReadCapture(task, tc_id)
        except (ValueError, PermissionError):
            pass  # Ordinary reads still work; the result declares missing coverage.

    # A file grant never reaches content refs, remote adapters, root rebinding,
    # the generic preimage/read gate, or custom handlers. Its descriptor-backed
    # backend revalidates durable ownership again at actual I/O/publication.
    from lib.project_dispatch.scope import tool_scope
    with tool_scope(task, fn_name, fn_args, project_path) as scope:
        if scope is not None:
            from lib.project_mod import execute_tool
            if remote_worktree_binding(cfg):
                raise ValueError('Scoped file tools cannot use a remote transport')
            tool_content = execute_tool(fn_name, fn_args, scope.root,
                conv_id=task.get('convId'), task_id=task['id'], task=task,
                file_source_capture=file_source_capture)
            meta = build_project_tool_meta(fn_name, fn_args, tool_content)
            _record_producer_result_metadata(fn_name, tool_content, round_entry)
            _finalize_tool_round(task, rn, round_entry, [meta],
                status='error' if meta.get('writeOk') is False else 'done')
            if file_source_capture is not None:
                try:
                    file_source_capture.publish(round_entry)
                except (TimeoutError, PermissionError):
                    pass  # Cancelled, expired or changed-scope reads publish no witness.
            return tc_id, tool_content, False

    from lib.tasks_pkg.workspace_delivery import (
        ensure_execution_roots, execution_root_key, route_project_tool_args)
    ensure_execution_roots(task)
    fn_args = route_project_tool_args(task, fn_name, fn_args)
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
            meta['refusal'] = {'kind': 'content_ref'}
            _finalize_tool_round(task, rn, round_entry, [meta])
            return tc_id, error_msg, False
        fn_args['content'] = resolved
        logger.info('[Tool:write_file] content_ref resolved: tool_round=%s → %d chars, '
                    'path=%s task=%s',
                    ref.get('tool_round'), len(resolved),
                    fn_args.get('path', '?'), task.get('id', '?')[:8])

    # ── RWA remote-worktree routing (P3) ──
    # 远程绑定的会话在此分流:同名工具 → 桥命令寻址入队。服务器 FS 门
    # (下方 ReadGate/FreshGate/abs_path_guard)不适用 —— agent 自守。
    _result_projection_items = [] if fn_name == 'read_files' else None
    _remote = remote_worktree_binding(cfg)
    if _remote:
        if fn_name == 'read_files' and fn_args.get('bind_sources') is True:
            tool_content = 'Error: file source binding is unavailable for remote worktrees; read without bind_sources.'
            meta = build_project_tool_meta(fn_name, fn_args, tool_content)
            _finalize_tool_round(task, rn, round_entry, [meta], status='error')
            return tc_id, tool_content, False
        if fn_name == 'run_command' and fn_args.get('background'):
            tool_content = ('Error: explicit background commands are not supported '
                            'by the remote-worktree transport; no command ran.')
            meta = build_project_tool_meta(fn_name, fn_args, tool_content)
            meta.update(notRun=True, exitCode='not-run', badge='unsupported')
            _finalize_tool_round(task, rn, round_entry, [meta], status='error')
            return tc_id, tool_content, False
        return _execute_remote_project_tool(
            task, fn_name, tc_id, fn_args, rn, round_entry, _remote)


    from lib.tasks_pkg.project_guidance import check_project_guidance, freeze_project_command_cwd
    if fn_name == 'run_command':
        fn_args = freeze_project_command_cwd(task, fn_args)
    guidance_refusal = check_project_guidance(task, fn_name, fn_args)
    if guidance_refusal:
        meta = build_project_tool_meta(fn_name, fn_args, guidance_refusal)
        meta.update(notRun=True, badge='review project guidance')
        _finalize_tool_round(task, rn, round_entry, [meta], status='error')
        return tc_id, guidance_refusal, False

    # Resumed history and user instructions can still contain canonical
    # absolute paths. Binding must also cover these structured tool inputs,
    # rather than just changing the default cwd in task config.
    from lib.tasks_pkg.workspace_delivery import rebind_project_tool_arguments
    fn_args = rebind_project_tool_arguments(task, fn_args)
    if fn_name == 'run_command':
        from lib.tasks_pkg.workspace_delivery import prepare_worker_command_runtime
        prepare_worker_command_runtime(task)

    # A model occasionally tries to export browser cookies and replay them in
    # curl/wget for a file download. Detect that narrow shape before any shell
    # process exists and route it through the canonical browser→server staging
    # service. Ordinary curl/API inspection and every non-matching command keep
    # their exact shell semantics.
    if fn_name == 'run_command':
        from lib.tasks_pkg.handlers.authenticated_download import (
            maybe_redirect_authenticated_download,
        )
        redirected = maybe_redirect_authenticated_download(
            task=task,
            cfg=cfg,
            command=str((fn_args or {}).get('command') or ''),
        )
        if redirected is not None:
            safe_args = dict(fn_args or {})
            safe_args['command'] = redirected.display_command
            round_entry['authenticatedDownloadRedirected'] = True
            meta = build_project_tool_meta(
                fn_name, safe_args, redirected.tool_content)
            meta['badge'] = redirected.badge
            meta['authenticatedDownloadRedirected'] = True
            if redirected.receipt:
                meta['serverStagingReceipt'] = redirected.receipt
            _finalize_tool_round(
                task, rn, round_entry, [meta],
                status='done' if redirected.ok else 'error')
            return tc_id, redirected.tool_content, False

    # ── repo_map: structural outline, read-only ──
    # Public scan/rank/render retains output policy. An explicitly composed
    # native runtime supplies the owned scanner; omission uses the reference.
    if fn_name == 'repo_map':
        _rm_error = None
        _rm_stats: dict = {}
        try:
            if not project_path:
                raise ValueError('repo_map requires an attached project root')
            from lib.repo_map.api import generate_repo_map
            if repository_scanner is None and task.get('_repository_runtime') is not None:
                from lib.repo_map.native_runtime import scanner_for_task
                repository_scanner = scanner_for_task(task, project_path)
            _rm_text, _rm_stats = generate_repo_map(
                project_path,
                path=str(fn_args.get('path') or ''),
                focus=str(fn_args.get('focus') or ''),
                **({'scanner': repository_scanner} if repository_scanner is not None else {}),
            )
            tool_content = _rm_text
            ok = True
        except Exception as _e:
            logger.warning('[Tool:repo_map] failed: %s', _e, exc_info=True)
            _rm_error = f'Error: repo_map failed — {_e}'
            tool_content = _rm_error
            ok = False
        meta = build_project_tool_meta(fn_name, fn_args, tool_content)
        if ok and _rm_stats:
            meta['badge'] = (f'{_rm_stats["files_shown"]}/'
                             f'{_rm_stats["files_total"]} files')
            meta['stats'] = _rm_stats
        _finalize_tool_round(
            task, rn, round_entry, [meta],
            status='done' if ok else 'error')
        return tc_id, tool_content, not ok
    # ── Read-before-edit gate ──
    # apply_diff / insert_content built from guessed content are the dominant
    # source of "Search text not found" failures. Refuse them unless the
    # target file was read (or written) earlier in the conversation. See
    # lib/tasks_pkg/handlers/_read_gate.py for the policy.
    #
    # Single-edit tools (apply_diff / insert_content) are refused wholesale.
    # Atomic batches refuse the complete request before effects. Only explicit
    # partial mode partitions execution, preserving original arguments/indexes.
    _gate_skip_note = None
    _batch_skips = {}
    _execution_args = fn_args
    _atomic_batch = ((fn_name == 'edit_file' and fn_args.get('atomic') is not False)
                     or (fn_name == 'apply_diffs' and fn_args.get('atomic') is True))
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
            meta['refusal'] = {'kind': 'read_first',
                               'paths': _collect_target_paths(fn_name, fn_args)}
            _finalize_tool_round(task, rn, round_entry, [meta])
            return tc_id, _gate_err, False
    elif fn_name in ('edit_file', 'apply_diffs', 'insert_contents') and project_path:
        try:
            _skip_idx, _unread_raw = partition_batch_edits(task, fn_name, fn_args, project_path)
        except Exception as _ge:
            logger.warning('[ReadGate] partition failed for %s (atomic=%s): %s',
                           fn_name, _atomic_batch, _ge, exc_info=True)
            if _atomic_batch:
                _gate_err = ('Error: read-before-edit safety check unavailable. '
                             'No edits were executed; retry when the check is available.')
                meta = build_project_tool_meta(fn_name, fn_args, _gate_err)
                meta['badge'] = 'read check unavailable'
                _finalize_tool_round(task, rn, round_entry, [meta], status='error')
                return tc_id, _gate_err, True
            _skip_idx, _unread_raw = [], []
        if _skip_idx:
            edits = fn_args.get('edits') or []
            _skip_set = set(_skip_idx)
            # All edits target unread files → full refusal (nothing to run).
            if _atomic_batch or len(_skip_set) >= len(edits):
                from lib.tasks_pkg.handlers._read_gate import _format_refusal
                _gate_err = _format_refusal(fn_name, _unread_raw)
                meta = build_project_tool_meta(fn_name, fn_args, _gate_err)
                meta['badge'] = 'read first'
                meta['refusal'] = {'kind': 'read_first', 'paths': list(_unread_raw)}
                _finalize_tool_round(task, rn, round_entry, [meta])
                logger.info('[ReadGate] Refused all %d edit(s) of %s for unread file(s) %s (task=%s)',
                            len(edits), fn_name, ', '.join(_unread_raw), task.get('id', '?')[:8])
                return tc_id, _gate_err, False
            # Keep the original call/round arguments. Partition only the
            # execution copy after BOTH gates have checked the original set.
            _batch_skips.update({i: 'read first' for i in _skip_set})
            _gate_skip_note = (
                f'Read-before-edit gate: skipped {len(_skip_set)} edit(s) targeting '
                f'unread file(s): {", ".join(_unread_raw)}. '
                f'See indexed execution results below. read_files those '
                f'path(s) this turn, then re-issue the skipped edit(s) NEXT turn '
                f'(a sibling read_files in the same parallel batch does not count).'
            )
            logger.info('[ReadGate] Partial %s: skipped %d/%d edit(s) for unread file(s) %s (task=%s)',
                        fn_name, len(_skip_set), len(edits), ', '.join(_unread_raw),
                        task.get('id', '?')[:8])

    # ── Write-freshness guard (shared-HEAD overwrite protection) ──
    # The read gate above proves "you read this file at SOME point in this
    # conversation"; this guard proves "it has not changed SINCE". A recorded
    # read/write fingerprint that no longer matches the disk means ANOTHER
    # conversation or process touched the file — refuse the write instead of
    # silently clobbering their change. Same single-refusal / per-path
    # partition shape as the read gate. See handlers/_write_freshness_gate.py.
    _fresh_skip_note = None
    if fn_name in ('write_file', 'apply_diff', 'insert_content') and project_path:
        try:
            _fresh_err = check_write_freshness(task, fn_name, fn_args, project_path)
        except Exception as _fe:
            logger.warning('[FreshGate] check failed for %s (allowing through): %s',
                           fn_name, _fe, exc_info=True)
            _fresh_err = None
        if _fresh_err:
            meta = build_project_tool_meta(fn_name, fn_args, _fresh_err)
            meta['badge'] = 'stale'
            meta['refusal'] = {'kind': 'stale',
                               'paths': _collect_target_paths(fn_name, fn_args)}
            _finalize_tool_round(task, rn, round_entry, [meta])
            return tc_id, _fresh_err, False
    elif fn_name in ('edit_file', 'apply_diffs', 'insert_contents') and project_path:
        try:
            _stale_idx, _stale_raw = partition_stale_edits(task, fn_args, project_path)
        except Exception as _fe:
            logger.warning('[FreshGate] partition failed for %s (atomic=%s): %s',
                           fn_name, _atomic_batch, _fe, exc_info=True)
            if _atomic_batch:
                _fresh_err = ('Error: write-freshness safety check unavailable. '
                              'No writes were executed; retry when the check is available.')
                meta = build_project_tool_meta(fn_name, fn_args, _fresh_err)
                meta['badge'] = 'freshness check unavailable'
                _finalize_tool_round(task, rn, round_entry, [meta], status='error')
                return tc_id, _fresh_err, True
            _stale_idx, _stale_raw = [], []
        if _stale_idx:
            edits = fn_args.get('edits') or []
            _stale_set = set(_stale_idx)
            # All edits target stale files → full refusal (nothing to run).
            if _atomic_batch or len(_stale_set | set(_batch_skips)) >= len(edits):
                from lib.tasks_pkg.handlers._write_freshness_gate import (
                    _format_stale_refusal,
                )
                _fresh_err = _format_stale_refusal(fn_name, _stale_raw)
                meta = build_project_tool_meta(fn_name, fn_args, _fresh_err)
                meta['badge'] = 'stale'
                meta['refusal'] = {'kind': 'stale', 'paths': list(_stale_raw)}
                _finalize_tool_round(task, rn, round_entry, [meta])
                logger.info('[FreshGate] Refused all %d edit(s) of %s for stale file(s) %s (task=%s)',
                            len(edits), fn_name, ', '.join(_stale_raw), task.get('id', '?')[:8])
                return tc_id, _fresh_err, False
            _batch_skips.update({i: 'stale; re-read first' for i in _stale_set})
            _fresh_skip_note = (
                f'Write-freshness guard: skipped {len(_stale_set)} edit(s) targeting '
                f'file(s) changed on disk since this conversation last read/wrote '
                f'them: {", ".join(_stale_raw)}. '
                f'See indexed execution results below. read_files the '
                f'skipped path(s), reconcile against the current content, then '
                f're-issue the skipped edit(s).'
            )
            logger.info('[FreshGate] Partial %s: skipped %d/%d stale-target edit(s) %s (task=%s)',
                        fn_name, len(_stale_set), len(edits), ', '.join(_stale_raw),
                        task.get('id', '?')[:8])

    _admitted_indices = []
    if _batch_skips:
        import copy
        _execution_args = copy.deepcopy(fn_args)
        _admitted_indices = [i for i in range(len(fn_args['edits'])) if i not in _batch_skips]
        _execution_args['edits'] = [_execution_args['edits'][i] for i in _admitted_indices]
    _read_observations = []

    from lib.project_mod import execute_tool
    from lib.project_mod.abs_path_guard import (
        reset_restricted, set_restricted, task_is_remote,
    )
    # Remote API callers (agents:run / chat keys, compat adapters) must not
    # use absolute / ~ paths to read or write outside a registered workspace
    # root. Cookie-auth UI and the local CLI are unaffected (task_is_remote
    # is False for them). See lib/project_mod/abs_path_guard.py.
    _abs_token = set_restricted(task_is_remote(task))
    # Workspace-root resolution conv-id. Roots are REGISTERED under
    #   ``convId or id`` (orchestrator.ensure_project_state) and read back the
    #   same way by the streaming executor. The bare ``task['convId']`` used
    #   here historically was the odd one out: a sub-task with convId=''
    #   (e.g. the autopilot virtual-user) registers its roots under its TASK
    #   id but then resolved run_command's read-only / namespaced-path checks
    #   against convId='' → the globally-shared (concurrency-clobbered)
    #   _roots registry, which could be marked read-only by an unrelated
    #   task. Result: the VU's run_command was refused as "READ-ONLY
    #   workspace root" while its read_files/grep (routed via the streaming
    #   executor's convId-or-id) worked. Use the same key everywhere.
    _root_conv_id = execution_root_key(task)
    try:
        # read_files is globally available — when no project is attached,
        # absolute paths still work (routed inside tool_read_files via
        # lib.file_reader); project-relative paths error out helpfully.
        if fn_name in ('read_files', 'inspect_image') and not project_path:
            # read_files / inspect_image need the task so an uploaded-attachment
            # ref (att_media_ / att_txt_ / /api/images/) can be resolved under
            # the owner scope (media refs require task['_userId'], legacy text
            # refs scan task['messages']).
            _no_proj_kw = ({'task': task}
                           if fn_name in ('read_files', 'inspect_image') else {})
            if fn_name == 'read_files':
                _no_proj_kw['result_projection_items'] = _result_projection_items
                _no_proj_kw['file_source_capture'] = file_source_capture

                _no_proj_kw['read_observations'] = _read_observations
            tool_content = execute_tool(fn_name, fn_args, '', conv_id=_root_conv_id,
                                        task_id=task['id'], **_no_proj_kw)
        else:
            _progress_cb = None
            _spawn_cb = None
            _extra_kw = {'task': task}
            if fn_name == 'write_file':
                from lib.tasks_pkg.tool_runtime import active_context_for_call
                _extra_kw['runtime_context'] = active_context_for_call(
                    task, round_num=rn, tool_call_id=tc_id, round_entry=round_entry)
            if fn_name == 'read_files':
                _extra_kw['result_projection_items'] = _result_projection_items
                _extra_kw['file_source_capture'] = file_source_capture

                _extra_kw['read_observations'] = _read_observations
            if fn_name in ('read_files', 'inspect_image'):
                _extra_kw['task'] = task
            if fn_name == 'run_command' and fn_args.get('background') and project_path:
                from lib.tasks_pkg.background_commands import (
                    background_command_result, start_background_command,
                )
                # Capture only execution inputs, never foreground presentation
                # callbacks or task/round graphs in a long-lived closure.
                background_args = dict(fn_args)
                background_task_id = task['id']

                from copy import deepcopy
                from lib.tasks_pkg.workspace_delivery import (
                    execution_repositories, ensure_execution_roots)
                background_policy = deepcopy(ensure_execution_roots(task))

                background_roots = {key: deepcopy((task.get('config') or {}).get(key))
                    for key in ('projectPath', 'projectPaths', 'readOnlyPaths',
                                'workspaceRuntimeRoots')}
                background_execution = {
                    'repositories': deepcopy(execution_repositories(task))}
                def _execute_explicit_background(command_task, exec_ctx):
                    exec_args = dict(background_args)
                    command_task['_workspaceExecution'] = background_execution

                    if background_policy is not None:
                        command_task['_workspaceRootPolicy'] = deepcopy(background_policy)
                    command_task['config'] = background_roots
                    wrapped = exec_ctx.get('wrapped_command')
                    from lib.project_mod.tool_admission import RecordedCommandSource
                    admission_source = (RecordedCommandSource(
                        str(background_args.get('command') or ''), wrapped) if wrapped else None)
                    if wrapped:
                        # POSIX durable record: streams go to files, the exit
                        # code to a sentinel; the worker rebuilds the
                        # authoritative result from those files afterwards.
                        exec_args['command'] = wrapped

                    spawned = False

                    def _note_spawn(*_spawn_args):
                        nonlocal spawned
                        spawned = bool(command_task.get('_subprocess_pid'))
                        exec_ctx['note_spawn'](
                            command_task.get('_subprocess_pid'),
                            command_task.get('_subprocess_pgid'))

                    result = execute_tool(
                        'run_command', exec_args, project_path,
                        conv_id=_root_conv_id, task_id=background_task_id,
                        task=command_task, stdin_callback=None, on_chunk=None,
                        on_spawn=_note_spawn, runtime_context=None,
                        recorded_command_source=admission_source,
                    )
                    if wrapped and spawned:
                        return exec_ctx['build_result'](
                            aborted=bool(command_task.get('aborted')))
                    return result

                command_id = start_background_command(
                    task=task, config=cfg,
                    command=str(background_args.get('command') or ''),
                    execute=_execute_explicit_background,
                )
                tool_content = background_command_result(
                    str(background_args.get('command') or ''), command_id,
                    task=task,
                    has_conversation=bool(task.get('convId') and task.get('_userId')),
                )
                meta = build_project_tool_meta(fn_name, fn_args, tool_content)
                meta.pop('notRun', None)
                meta.pop('reason', None)
                meta.update(exitCode='background', backgrounded=True,
                            backgroundCommandId=command_id, badge='background',
                            timedOut=False)
                _finalize_tool_round(task, rn, round_entry, [meta])
                return tc_id, tool_content, False
            if fn_name == 'run_command':
                _cmd = fn_args.get('command', '') or ''
                _stdin_cb = _make_stdin_callback(task, rn, round_entry, _cmd)
                from lib.tasks_pkg.tool_runtime import active_context_for_call
                _runtime_context = active_context_for_call(
                    task, round_num=rn, tool_call_id=tc_id,
                    round_entry=round_entry)
                _spawn_cb = _make_run_command_spawn_cb(
                    task, rn, round_entry)
                _progress_cb = _make_run_command_progress_cb(
                    task, rn, round_entry, _cmd,
                    runtime_context=_runtime_context,
                    lifecycle=_spawn_cb)
                _extra_kw = {
                    'stdin_callback': _stdin_cb,
                    'on_chunk': _progress_cb,
                    # The spawn clock (). Without this the project
                    # mode — the COMMON case — published no deadlineTs and
                    # forced no checkpoint, so the countdown silently never
                    # appeared exactly where it mattered most. The same factory
                    # _handle_code_exec uses, so both entry points share ONE
                    # implementation (the lesson of the entrance-count drift).
                    'on_spawn': _spawn_cb,
                    'on_grep_intercept': _make_grep_intercept_cb(
                        task, rn, round_entry),
                    'task': task,  # enable cooperative abort of subprocesses

                    'runtime_context': _runtime_context,
                }
            tool_content = ''
            command_failed = True
            try:
                if fn_name == 'run_command' and project_path:
                    from lib.tasks_pkg.handlers._background_command import (
                        run_with_steer_handoff,
                    )

                    def _detach_progress():
                        detach = getattr(_progress_cb, 'detach', None)
                        if callable(detach):
                            detach()

                    def _execute_run(command_task):
                        command_kw = dict(_extra_kw)
                        command_kw['task'] = command_task
                        return execute_tool(
                            fn_name, fn_args, project_path,
                            conv_id=_root_conv_id, task_id=task['id'],
                            **command_kw,
                        )

                    tool_content = run_with_steer_handoff(
                        task=task,
                        config=cfg,
                        command=_cmd,
                        execute=_execute_run,
                        on_detach=_detach_progress,

                        on_handoff=lambda command_id: round_entry.update(backgroundCommandId=command_id),
                        round_num=rn,
                        tool_call_id=tc_id,
                    )
                else:
                    tool_content = (
                        execute_tool(
                            fn_name, _execution_args, project_path,
                            conv_id=_root_conv_id, task_id=task['id'],
                            **_extra_kw,
                        ) if project_path else 'Error: No project path.'
                    )
                command_failed = False
            finally:
                if _spawn_cb is not None:
                    _spawn_cb.finish()
                # Flush any buffered run_command output tail.
                if _progress_cb is not None:
                    try:
                        _progress_cb.flush()
                    except Exception as e:
                        logger.debug('[run_command] progress flush failed: %s', e)

                    _finalize_output = getattr(
                        _progress_cb, 'finalize_output', None)
                    if callable(_finalize_output):
                        from lib.tasks_pkg.handlers._background_command import (
                            is_background_command_result,
                        )
                        _incomplete = command_failed or (
                            _runtime_context is not None
                            and _runtime_context.cancellation_requested
                        ) or '[Command timed out]' in tool_content or (
                            '[Command interrupted by' in tool_content) or (
                            is_background_command_result(tool_content))
                        _artifact = _finalize_output(complete=not _incomplete)
                        from lib.tasks_pkg.handlers.code_exec import (
                            _project_output_artifact,
                            _register_output_artifact_origin,
                        )
                        tool_content = _project_output_artifact(
                            tool_content, _artifact)
                        _register_output_artifact_origin(
                            task, round_entry, _artifact,
                            tool_name='run_command', display=_cmd,
                            tool_call_id=tc_id)
                        if _artifact.spilled:
                            round_entry['outputArtifact'] = {
                                'artifactRef': _artifact.artifact_ref,
                                'sizeBytes': _artifact.size_bytes,
                                'complete': _artifact.complete,
                                'degraded': _artifact.degraded,
                                'degradedReason': _artifact.degraded_reason,
                            }
    finally:
        reset_restricted(_abs_token)


    if _batch_skips:
        from lib.tasks_pkg.handlers._read_gate import restore_batch_result_indexes
        tool_content = restore_batch_result_indexes(
            tool_content, fn_args['edits'], _admitted_indices, _batch_skips)
    # ── Drain SVG inline-render signals (same thread as the read) ──
    # An SVG file is read as text (its markup enters the model stream), but
    # read_tools ALSO signals its source so the frontend can render it inline
    # like an image. Drain unconditionally here — even on the image early-
    # return path — so a signal never leaks into the next read on this thread.
    _svg_renders = []
    if fn_name == 'read_files':
        try:
            from lib.project_mod.read_tools import drain_svg_render_signals
            _svg_renders = drain_svg_render_signals()
        except Exception as e:
            logger.debug('[Project] drain_svg_render_signals failed (non-fatal): %s', e)

    # ── Record write-freshness tokens for successful reads ──
    # A read is authoritative content for THIS conversation: token it so a
    # later write by us is refused if someone else touches the file first.
    if fn_name in ('read_files', 'inspect_image'):
        try:
            record_read_paths(task, fn_args, project_path, tool_content,
                              observations=_read_observations)

            # Host-owned per-item evidence for the read-first history scan.
            # Raw tool output cannot distinguish file contents from headers.
            round_entry['_readObservedPaths'] = [item.abs_path for item in _read_observations]
        except Exception as _fe:
            logger.debug('[FreshGate] read-token record failed (non-fatal): %s', _fe)

    from lib.tools.rich_response import image_posthook
    _response_assets = image_posthook(task, fn_name, fn_args, project_path, tool_content)

    # read_files with absolute image paths returns a batch dict with __batch_images__
    _img_descriptors = None  # frontend-render image list (all images in a batch)
    is_batch_image = isinstance(tool_content, dict) and tool_content.get('__batch_images__')
    if is_batch_image:
        # Extract the first image for VLM upload, keep text content
        _images = tool_content['__batch_images__']
        _text = tool_content.get('_text_content', '')
        # Capture every image's data URI for inline rendering. Each image dict
        # already carries its own filename (set in read_tools).
        _img_list = [img for img in _images.values()
                     if isinstance(img, dict) and img.get('__screenshot__')]
        _img_descriptors = [_screenshot_to_descriptor(img, img.get('filename', ''))
                            for img in _img_list]
        # Use the first image as the primary screenshot result, but attach the
        # full list so EVERY image rides the wire to the VLM (one image_url
        # block each — see _append_screenshot_message).
        first_img = _img_list[0] if _img_list else next(iter(_images.values()))
        tool_content = first_img
        if len(_img_list) > 1:
            tool_content['images'] = _img_list
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
            'toolName': fn_name, 'title': filename,
            'snippet': f'{filename} ({fmt}, {size_info})',
            'source': 'Project', 'fetched': True,
            'fetchedChars': comp_size, 'url': '',
            'badge': fmt,
            # Inline-render payload — frontend (tool_rounds.js) draws an
            # <img> per descriptor. Each carries a full data: URL.
            'imageDataUris': ([d for d in _img_descriptors if d.get('uri')]
                              + [s for s in _svg_renders if s.get('uri')]),
        }
        if _response_assets:
            meta['responseAssets'] = _response_assets
            tool_content['responseAssets'] = _response_assets
        # ── inspect_image: surface the transform + source/view dimensions ──
        if fn_name == 'inspect_image':
            _ops = tool_content.get('inspectOps', '') or ''
            _view = tool_content.get('viewSize') or []
            _src = tool_content.get('sourceSize') or []
            meta['inspectOps'] = _ops
            if len(_view) == 2 and len(_src) == 2:
                meta['snippet'] = (f'{filename}: {_src[0]}×{_src[1]} → '
                                   f'{_view[0]}×{_view[1]}px ({_ops})')
            if _ops:
                meta['badge'] = _ops
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

    if fn_name == 'run_command':
        from lib.tasks_pkg.handlers._background_command import (
            is_background_command_result,
        )
        if is_background_command_result(tool_content):
            meta.pop('notRun', None)
            meta.pop('reason', None)
            meta.update({
                'exitCode': 'background',
                'timedOut': False,
                'backgrounded': True,
                'badge': 'background',
                'snippet': f'$ {str(fn_args.get("command") or "")[:120]}',
            })

    if _response_assets:
        meta['responseAssets'] = _response_assets
        if isinstance(tool_content, str):
            import json as _response_json
            tool_content += '\nResponse assets: ' + _response_json.dumps(_response_assets, ensure_ascii=False)

    if fn_name == 'run_command' and round_entry.get('backgroundCommandId'):
        meta['backgroundCommandId'] = round_entry['backgroundCommandId']

    if fn_name == 'run_command' and round_entry.get('grepSearchIntercepted'):
        meta['grepSearchIntercepted'] = True

    if fn_name == 'run_command':
        from lib.tasks_pkg.workspace_delivery import record_worker_verification
        try:
            record_worker_verification(task, fn_args, project_path, meta)
        except Exception as error:
            # Verification evidence is supplementary; a metadata outage must
            # not rewrite the exact successful command result. Terminal intent
            # retries the retained host receipt before authorizing publication.
            logger.warning('Workspace verifier receipt pending task=%s: %s', task['id'], error)

    if _gate_skip_note:
        meta['badge'] = 'partial: read first'
        meta['refusal'] = {'kind': 'partial_read_first',
                           'paths': list(_unread_raw),
                           'skipped': len(_skip_set),
                           'proceeded': len(_admitted_indices)}
    if _fresh_skip_note:
        meta['badge'] = 'partial: stale'
        meta['refusal'] = {'kind': 'partial_stale',
                           'paths': list(_stale_raw),
                           'skipped': len(_stale_set),
                           'proceeded': len(_admitted_indices)}

    # ── Attach SVG inline-render descriptors (text read path) ──
    # SVG source rides the model stream as text; these data URIs let the
    # frontend ALSO render the vector image inline (tool_rounds.js).
    if _svg_renders:
        _svg_uris = [s for s in _svg_renders if s.get('uri')]
        if _svg_uris:
            meta['imageDataUris'] = _svg_uris

    # ── Promote renderable writes to a chat artifact ──
    # Best-effort: failure here MUST NOT fail the tool round itself.
    if fn_name in ('write_file', 'edit_file', 'apply_diff', 'apply_diffs', 'insert_content', 'insert_contents') and project_path:
        try:
            _maybe_promote_write_to_artifact(task, fn_name, fn_args, project_path, meta)
        except Exception as e:
            logger.debug('[Artifacts] promotion path failed (non-fatal): %s',
                         e, exc_info=True)

    # ── Surface silent workspace-root auto-registration ──
    # An absolute-path write outside all roots auto-registers the nearest
    # existing ancestor as a NEW extra root (lib/project_mod/write_tools.py
    # _resolve_write_path §2). That expansion used to be invisible — only an
    # app.log line. The write layer signals it via a per-thread collector we
    # drain HERE (the handler owns ``task``) and emit as a visible event.
    try:
        from lib.project_mod.write_tools import drain_root_added_signals
        _new_roots = drain_root_added_signals()
        if _new_roots:
            from lib.agent_core.events import EventType, emit
            emit(task, EventType.WORKSPACE_ROOT_ADDED, roots=_new_roots)
            logger.info('[Project] workspace_root_added emitted for %d new root(s): %s',
                        len(_new_roots),
                        ', '.join(r.get('rootName', '?') for r in _new_roots))
    except Exception as e:
        logger.debug('[Project] workspace_root_added emit failed (non-fatal): %s', e)

    # For run_command: inject fileChanges from tracked modifications
    if fn_name == 'run_command' and project_path:
        try:
            from lib.project_mod.modifications import get_modifications
            task_mods = [m for m in get_modifications(project_path, conv_id=task.get('convId'), include_content=False)
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

    # A successful project write is the runtime signal that derives the one
    # immutable-conversation work item for this physical task.  The same hook
    # records changed paths/artifacts and emits only in-memory overlap advice.
    if project_path and (fn_name in (
            'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
            'insert_content', 'insert_contents')
            or (fn_name == 'run_command' and meta.get('fileChanges'))):
        try:
            from lib.conversations.project_brain import note_file_signal
            note_file_signal(
                task,
                project_path,
                fn_name=fn_name,
                fn_args=fn_args,
                tool_content=tool_content,
                meta=meta,
            )
        except Exception as e:
            logger.debug('[ProjectBrain] file signal skipped: %s', e)

    # Prepend the read-gate skip note so the model sees which path(s) were
    # dropped. Done AFTER meta is built so the per-edit summaries parse the
    # unmodified batch header ("Applied N/M edits").
    if _fresh_skip_note and isinstance(tool_content, str):
        tool_content = f'{_fresh_skip_note}\n\n{tool_content}'
    if _gate_skip_note and isinstance(tool_content, str):
        tool_content = f'{_gate_skip_note}\n\n{tool_content}'

    if fn_name == 'write_file' and meta.get('writeOk') is False:
        from lib.tasks_pkg.executor._effects import effect_inspection_action
        inspection_action = effect_inspection_action(round_entry.get('executionEffect'))
        if inspection_action:
            tool_content = f'{tool_content}\n\n{inspection_action}'

    _record_producer_result_metadata(fn_name, tool_content, round_entry)
    _finalize_tool_round(
        task, rn, round_entry, [meta],
        status='error' if meta.get('writeOk') is False else 'done')
    if file_source_capture is not None:
        try:
            file_source_capture.publish(round_entry)
        except (TimeoutError, PermissionError):
            pass  # No witness crosses a cancellation/scope fence.
    if (fn_name == 'read_files' and isinstance(tool_content, str)
            and _result_projection_items and isinstance(round_entry, dict)):
        # Ephemeral producer metadata: settlement pops this immediately after
        # projecting the V2 envelope, so raw per-file previews never become a
        # second persisted tool-result representation.
        round_entry[TOOL_RESULT_PROJECTION_ITEMS_KEY] = \
            _result_projection_items
    return tc_id, tool_content, False


def _register_live_file_change_stamping() -> None:
    """Subscribe the commit-round live stamp to journal records.

    ``handlers/__init__`` imports this module at gateway build — before any
    tool can execute — so the listener is in place ahead of the first
    task-stamped journal write.  Lazy imports keep project_mod/commit_round
    out of this HOT_PATH module's import graph.
    """
    try:
        from lib.project_mod.modifications import register_live_change_listener
        from lib.tasks_pkg.commit_round._derive import note_live_file_change
        register_live_change_listener(note_live_file_change)
    except Exception as e:
        logger.warning('[Project] live file-change stamping unavailable: %s', e)


_register_live_file_change_stamping()
