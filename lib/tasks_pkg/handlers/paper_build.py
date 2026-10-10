"""lib/tasks_pkg/handlers/paper_build.py — Agent-facing paper build tool.

The model edits .tex/.bib sources with the project tools, then verifies them
through the SAME bounded build service the browser preview uses: frozen
snapshots, supervised latexmk, validated PDFs and immutable receipts. This
handler only resolves the owned document, admits one build and formats the
verified receipt — it never spawns a compiler subprocess itself.
"""

from __future__ import annotations

import time
import uuid

from lib.log import get_logger
from lib.paper.builds.contract import validate
from lib.paper.builds.errors import BuildError
from lib.paper.builds.runtime import get_scheduler
from lib.paper.builds.service import start_preview
from lib.storage.errors import StorageError
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.manager._registry import task_user_id
from lib.tools.paper_build import PAPER_BUILD_TOOL_NAMES

logger = get_logger(__name__)

# The declared tool timeout (registry spec) is the outer bound; the wait
# deadline stays under one viewer subscription window so a single admission
# covers queueing plus one supervised compile.
_WAIT_DEADLINE_SECONDS = 220
_RENEW_INTERVAL_SECONDS = 45
_LISTED_DIAGNOSTICS = 16
_LOG_TAIL_CHARS = 1500


def _conv_id(task, cfg):
    return str(task.get('convId') or task.get('conv_id')
               or (cfg or {}).get('conv_id') or '')


def _resolve_document(conv_id: str, owner_user_id: int, path: str):
    """Return (document, error_text); exactly one owned document or a verdict."""
    from lib.paper.builds.documents import discover_documents
    payload = {'conv_id': conv_id}
    if path:
        payload['path'] = path
    result = discover_documents(payload, owner_user_id=owner_user_id)
    documents = result['documents']
    selected = result.get('selected')
    if path:
        candidates = [doc for doc in documents
                      if doc['source'] == 'local'
                      and (doc['path'] == path or doc['main_file'] == path
                           or doc['path'].endswith('/' + path))]
        if len(candidates) == 1:
            return candidates[0], None
        if not candidates and selected and selected.get('source') == 'local':
            return selected, None
        return None, ('没有找到与 `%s` 匹配的唯一论文文档（候选 %d 个）。'
                      '请用发现列表中的相对路径重试。' % (path, len(documents)))
    if selected:
        return selected, None
    if len(documents) == 1:
        return documents[0], None
    if not documents:
        return None, ('这个会话还没有可编译的 TeX 论文项目。请先把包含 '
                      '\\documentclass 的项目目录挂载为会话项目，或让用户在'
                      '预览面板中选择主文档。')
    listing = '\n'.join('  - `%s`（%s）' % (doc.get('path') or doc.get('label'),
                                            doc.get('project_root') or doc.get('source'))
                        for doc in documents[:8])
    return None, ('发现 %d 个候选论文文档，无法自动选择。请用 path 参数指定其一：\n%s'
                  % (len(documents), listing))


def _turn_aborted(task) -> bool:
    if task.get('aborted'):
        return True
    event = task.get('abort_event')
    return bool(event is not None and getattr(event, 'is_set', lambda: False)())


def _await_build(accepted: dict, task, owner_user_id: int):
    """Wait for one admitted build; returns (row, terminal_status)."""
    scheduler = get_scheduler()
    runtime = scheduler.production.runtime
    task_id = accepted['task_id']
    subscriber_id = accepted['subscriber_id']
    deadline = time.monotonic() + _WAIT_DEADLINE_SECONDS
    renew_at = time.monotonic() + _RENEW_INTERVAL_SECONDS
    while time.monotonic() < deadline:
        if _turn_aborted(task):
            _detach(scheduler, task_id, subscriber_id, owner_user_id)
            return None, 'aborted'
        row = runtime.get_owned(task_id, user_id=owner_user_id)
        status = (row or {}).get('status')
        if status in ('done', 'error', 'aborted'):
            return row, status
        if time.monotonic() >= renew_at:
            try:
                scheduler.renew(task_id, owner_user_id=owner_user_id,
                                subscriber_id=subscriber_id)
            except BuildError:
                pass
            renew_at = time.monotonic() + _RENEW_INTERVAL_SECONDS
        time.sleep(0.4)
    _detach(scheduler, task_id, subscriber_id, owner_user_id)
    return None, 'timeout'


def _detach(scheduler, task_id, subscriber_id, owner_user_id):
    try:
        scheduler.detach(task_id, owner_user_id=owner_user_id,
                         subscriber_id=subscriber_id)
    except BuildError:
        pass


def _fmt_diagnostics(diagnostics: list[dict]) -> str:
    if not diagnostics:
        return '无诊断信息。'
    errors = [d for d in diagnostics if d['severity'] == 'error']
    warnings = [d for d in diagnostics if d['severity'] == 'warning']
    lines = ['错误 %d 条，警告 %d 条%s：' % (
        len(errors), len(warnings),
        '（仅列出前 %d 条）' % _LISTED_DIAGNOSTICS
        if len(diagnostics) > _LISTED_DIAGNOSTICS else '')]

    def fmt(row):
        where = row['file'] or '?'
        if row.get('line'):
            where += ':%d' % row['line']
        return '  [%s] %s %s（%s）' % (row['severity'], where, row['message'][:300],
                                      row['code'])

    for row in (errors + warnings)[:_LISTED_DIAGNOSTICS]:
        lines.append(fmt(row))
    return '\n'.join(lines)


def _log_tail(owner_user_id: int, document_id: str, build_id: str) -> str:
    try:
        scheduler = get_scheduler()
        with scheduler.cache.open_artifact(owner_user_id, document_id,
                                           build_id, 'build.log') as artifact:
            stream, size = artifact
            if size > _LOG_TAIL_CHARS:
                stream.seek(size - _LOG_TAIL_CHARS)
            data = stream.read(_LOG_TAIL_CHARS)
        return data.decode('utf-8', errors='replace')
    except (BuildError, OSError, ValueError):
        return ''


def _page_count(owner_user_id: int, document_id: str, build_id: str) -> int:
    """Page count from the build's own SyncTeX (never a recompile)."""
    try:
        scheduler = get_scheduler()
        with scheduler.cache.open_artifact(owner_user_id, document_id,
                                           build_id, 'document.synctex.gz') as artifact:
            stream, _size = artifact
            from lib.paper.latex_layout import parse_synctex
            from lib.paper.builds.synctex import _decode
            return int(parse_synctex(_decode(stream)).get('max_page') or 0)
    except Exception as _e:
        logger.debug('count pdf pages failed: %s', _e)
        # Page count is advisory garnish on the verdict; a cache/parse quirk
        # must never fail a compile report the agent depends on.
        return 0


def _receipt_verdict(receipt: dict, *, log_tail: bool) -> str:
    status = receipt['status']
    duration = (receipt['completed_at'] - receipt['started_at']) / 1000.0
    header = {'success': '编译成功 ✓', 'failed': '编译失败 ✗',
              'cancelled': '编译已取消'}.get(status, '编译状态：' + status)
    lines = ['%s（引擎 %s，耗时 %.1fs，主文件 %s）' % (
        header, receipt.get('engine') or '?', duration, receipt.get('main_file') or '?'),
        _fmt_diagnostics(receipt.get('diagnostics') or [])]
    if status == 'failed':
        tail = log_tail and _log_tail(receipt['owner_user_id'],
                                      receipt['document_id'], receipt['build_id'])
        if tail:
            lines.append('build.log 尾部：\n%s' % tail)
        lines.append('请按上面的 file:line 定位并修复全部错误，然后重新调用 '
                     'paper_build 验证，直到编译成功。')
    elif status == 'success':
        pages = _page_count(receipt['owner_user_id'], receipt['document_id'],
                            receipt['build_id'])
        lines.append('PDF 已发布（build_id %s%s）。引用/参考文献警告建议一并处理。'
                     % (receipt['build_id'][:12],
                        '，共 %d 页' % pages if pages else ''))
    return '\n'.join(lines)


def _latest_receipt(document: dict, owner_user_id: int):
    """Return (projection, receipt) of the latest retained build, if any."""
    from lib.paper.builds.document_repository import PaperDocumentRepository
    document_id = document.get('document_id')
    if not document_id:
        resolved = PaperDocumentRepository(owner_user_id).resolve([document])
        document_id = (resolved or [None])[0]
    if not document_id:
        return None, None
    projection = PaperDocumentRepository(owner_user_id).get(document_id)
    if not projection or not projection.get('receipt'):
        return projection, None
    return projection, projection['receipt']


def _latest_verdict(document: dict, owner_user_id: int) -> str:
    projection, receipt = _latest_receipt(document, owner_user_id)
    if not receipt:
        return ('该文档还没有已保留的构建结果（状态 %s）。'
                '请用 action=compile 触发一次编译。'
                % ((projection or {}).get('status') or 'unknown'))
    return ('最近一次已保留构建（build_id %s）：\n%s'
            % (receipt['build_id'][:12], _receipt_verdict(receipt, log_tail=False)))


def _layout_verdict(document: dict, owner_user_id: int) -> str:
    from lib.paper.builds.layout import document_layout
    projection, receipt = _latest_receipt(document, owner_user_id)
    if not receipt or receipt.get('status') != 'success':
        return '还没有成功构建可分析；请先调用 paper_build 编译一次。'
    report = document_layout(
        projection or {'document_id': document.get('document_id'), 'source': document},
        receipt['build_id'], owner_user_id=owner_user_id)
    if not report['pages']:
        return '最近一次构建没有可用的版面信息（缺 SyncTeX 产物）；请重新编译。'
    lines = ['共 %d 页（build_id %s）' % (report['pages'], report['build_id'][:12])]
    sections = report['sections']
    if sections:
        lines.append('章节页码（file:line → 页）:')
        for row in sections[:24]:
            lines.append('  p.%-4d %s %s（%s:%d）'
                         % (row['page'], row['level'], row['title'] or '(无标题)',
                            row['file'], row['line']))
        if len(sections) > 24:
            lines.append('  …共 %d 个章节' % len(sections))
    end = report.get('end')
    if end and end.get('fullness_page') is not None:
        lines.append('末页（第 %d 页）填充度：整页 %.0f%%' % (
            end['page'], end['fullness_page'] * 100)
            + ('，正文区 %.0f%%' % (end['fullness_text'] * 100)
               if end.get('fullness_text') is not None else ''))
    lines.append('需要压缩页数时：优先缩短跨页最多的章节，然后调用 paper_build 复验。')
    return '\n'.join(lines)


def _import_verdict(conv_id: str, arxiv: str, owner_user_id: int) -> str:
    from lib.paper.builds.imports import import_arxiv_source
    result = import_arxiv_source({'conv_id': conv_id, 'arxiv': arxiv},
                                 owner_user_id=owner_user_id)
    lines = ['已导入 arXiv 源码：%d 个文件（%.1f KiB）→ `%s/`'
             % (result['files'], result['bytes'] / 1024.0, result['directory'])]
    if result['skipped']:
        lines.append('跳过 %d 个非常规条目（链接/特殊文件）。' % len(result['skipped']))
    document = result.get('document')
    if document:
        lines.append('已选中主文档 `%s`；现在可以阅读/修改源码，'
                     '并用 paper_build 编译验证。' % (document.get('main_file')
                                                     or document.get('path')))
    elif result['candidates']:
        lines.append('候选主文档：%s。用 paper_build 的 path 参数指定其一。'
                     % '、'.join('`%s`' % path for path in result['candidates'][:4]))
    else:
        lines.append('压缩包内没有 \\documentclass 主文档；该论文可能是 PDF-only 投稿。')
    return '\n'.join(lines)


def _error_text(exc) -> str:
    if isinstance(exc, StorageError):
        return '论文构建存储暂不可用（%s）：%s' % (exc.code, exc)
    hints = {
        'project_root': '请先为会话挂载包含 TeX 项目的目录。',
        'source_path': '源文件路径不可用或不在会话项目内。',
        'main_ambiguous': '主文档不唯一，请用 path 参数明确指定。',
        'build_busy': '构建队列正忙，请稍后重试。',
        'build_stopped': '构建服务正在停止，请稍后重试。',
        'desktop_unavailable': '远程桌面构建当前不可用。',
        'overleaf_capability': 'Overleaf 项目缺少编译能力，请检查绑定。',
        'arxiv_unavailable': 'arXiv 源不可用；检查 ID 或稍后重试。',
        'arxiv_source_unavailable': '该论文没有可下载的 TeX 源码（PDF-only）。',
        'archive_invalid': '仅支持 .zip/.tar/.tar.gz 源码包。',
        'archive_limit': '源码包超出大小上限。',
        'archive_empty': '源码包内没有可用文件。',
    }
    return '无法编译：%s。%s' % (exc, hints.get(getattr(exc, 'code', ''), ''))


@tool_registry.tool_set(
    PAPER_BUILD_TOOL_NAMES,
    category='project',
    description='Compile the conversation LaTeX paper and read verified diagnostics')
def _handle_paper_build_tool(task, tc, fn_name, tc_id, fn_args, rn,
                             round_entry, cfg, project_path, project_enabled,
                             all_tools=None):
    owner_user_id = task_user_id(task)
    conv_id = _conv_id(task, cfg)
    status = 'done'
    if not conv_id:
        content = '%s 需要一个会话上下文；当前调用没有会话标识。' % fn_name
    else:
        try:
            if fn_name == 'paper_import_arxiv':
                arxiv = str(fn_args.get('arxiv') or '').strip()
                content = (_import_verdict(conv_id, arxiv, owner_user_id) if arxiv
                           else '请提供 arXiv ID 或链接（arxiv 参数）。')
                if not arxiv:
                    status = 'error'
                _finalize_tool_round(task, rn, round_entry, [
                    _build_simple_meta(fn_name, content, source='PaperBuild',
                                       title=arxiv or 'arXiv 导入',
                                       snippet=content.split('\n', 1)[0][:160])],
                                     status=status)
                return tc_id, content, False
            document, problem = _resolve_document(
                conv_id, owner_user_id, str(fn_args.get('path') or '').strip())
            if problem:
                content = problem
                status = 'error'
            elif fn_name == 'paper_layout':
                content = _layout_verdict(document, owner_user_id)
            elif (fn_args.get('action') or 'compile') == 'diagnostics':
                content = _latest_verdict(document, owner_user_id)
            else:
                payload = validate({
                    'source': 'local', 'conv_id': conv_id,
                    'path': document.get('path') or document.get('main_file'),
                    'project_root': document.get('project_root') or '',
                    'main_file': document.get('main_file') or '',
                    'engine': fn_args.get('engine') or 'auto',
                    'request_id': uuid.uuid4().hex,
                    'subscriber_id': ('agent:%s' % (task.get('id') or uuid.uuid4().hex))[:160],
                }, 'PaperBuildRequestV1')
                accepted = start_preview(payload, owner_user_id=owner_user_id)
                row, terminal = _await_build(accepted, task, owner_user_id)
                receipt = ((row or {}).get('result') or {}).get('receipt')
                if receipt:
                    content = _receipt_verdict(receipt, log_tail=True)
                    status = 'done' if receipt['status'] == 'success' else 'error'
                elif terminal == 'timeout':
                    content = ('构建在 %d 秒内未完成（队列或编译超时）。'
                               '可稍后用 action=diagnostics 查询，或重新调用 compile。'
                               % _WAIT_DEADLINE_SECONDS)
                    status = 'error'
                elif terminal == 'aborted':
                    content = '本次构建随会话中断而取消；如需验证请重新调用 paper_build。'
                else:
                    error = ((row or {}).get('error') or {})
                    content = '构建未产生结果：%s' % (error.get('message') or terminal)
                    status = 'error'
        except (BuildError, StorageError) as exc:
            content = _error_text(exc)
            status = 'error'
        except Exception:
            logger.exception('[paper_build] unexpected failure')
            content = 'paper_build 内部错误；构建服务状态未受影响，可重试。'
            status = 'error'
    badge = ''
    if fn_name == 'paper_build':
        headline = content.split('\n', 1)[0]
        badge = ('✓ 编译成功' if status == 'done' and '成功' in headline
                 else ('✗ 编译失败' if status == 'error' else ''))
    meta = _build_simple_meta(
        fn_name, content, source='PaperBuild',
        title=str(fn_args.get('path')
                  or ('论文版面' if fn_name == 'paper_layout' else '论文构建')),
        snippet=content.split('\n', 1)[0][:160], badge=badge)
    if status != 'done':
        meta['writeOk'] = False
    _finalize_tool_round(task, rn, round_entry, [meta], status=status)
    return tc_id, content, False
