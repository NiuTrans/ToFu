"""lib/tasks_pkg/handlers/paper_quality.py — Agent-facing paper quality gate.

Track D of the paper toolchain: paper_check runs the submission-readiness
gate (source QA rules on the LIVE tree + PDF-level checks on the latest
retained build), paper_review assembles the deterministic peer-review
dossier the calling model then fills in, and paper_scaffold renders a
new-paper skeleton as text. All three are read-only: this handler resolves
the owned document through the same discovery path as paper_build and never
spawns a compiler or writes a file.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.paper.builds.errors import BuildError
from lib.paper.checks import format_report, run_pdf_checks, run_source_checks
from lib.paper.review_dossier import build_review_dossier, build_scaffold
from lib.storage.errors import StorageError
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.handlers.paper_build import (_conv_id, _latest_receipt,
                                                _resolve_document)
from lib.tasks_pkg.manager._registry import task_user_id
from lib.tools.paper_quality import PAPER_QUALITY_TOOL_NAMES

logger = get_logger(__name__)


def _live_main(document: dict):
    """(base, main_name) of the document's live tree, symlink-refusing."""
    from lib.paper.builds.documents import _root, _safe
    root = _root(document['project_root'])
    main = _safe(root, document.get('main_file') or document['path'])
    return main.parent, main.name


def _check_verdict(document: dict, scope: str, owner_user_id: int) -> str:
    base, main_name = _live_main(document)
    source_rows: list[dict] = []
    pdf_rows: list[dict] = []
    notes: list[str] = []
    if scope in ('all', 'source'):
        source_rows = run_source_checks(base, main_name)
    else:
        notes.append('已按 scope=%s 跳过源码检查。' % scope)
    if scope in ('all', 'pdf'):
        _projection, receipt = _latest_receipt(document, owner_user_id)
        if receipt and receipt.get('status') == 'success':
            pdf_rows = run_pdf_checks(document, receipt,
                                      owner_user_id=owner_user_id)
        else:
            notes.append('PDF 检查跳过：该文档还没有成功的已保留构建；'
                         '先调用 paper_build 编译一次，再复验。')
    else:
        notes.append('已按 scope=%s 跳过 PDF 检查。' % scope)
    report = format_report(source_rows, pdf_rows)
    return report + ('\n' + '\n'.join(notes) if notes else '')


def _review_verdict(document: dict, focus: str, owner_user_id: int) -> str:
    base, main_name = _live_main(document)
    dossier = build_review_dossier(base, main_name)
    if focus:
        return '用户指定的审稿重点：%s\n\n%s' % (focus, dossier)
    return dossier


def _scaffold_verdict(fn_args: dict) -> str:
    title = str(fn_args.get('title') or '').strip()
    sections = fn_args.get('sections')
    scaffold = build_scaffold(
        title, [str(s) for s in sections] if isinstance(sections, list) else [])
    target = str(fn_args.get('path') or '').strip()
    lines = ['论文骨架提案（确认后用 write_file 逐文件创建%s）：'
             % ('，目标目录 `%s`' % target if target else '')]
    lines.append('`main.tex`：\n```latex\n%s```' % scaffold['main_tex'])
    for stub in scaffold['files']:
        lines.append('`%s`：\n```latex\n%s```' % (stub['path'], stub['content']))
    lines.append('确认后可增删章节；文件创建完成后用 paper_build 编译验证。')
    return '\n\n'.join(lines)


def _error_text(exc) -> str:
    if isinstance(exc, StorageError):
        return '论文检查存储暂不可用（%s）：%s' % (exc.code, exc)
    hints = {
        'project_root': '请先为会话挂载包含 TeX 项目的目录。',
        'source_path': '源文件路径不可用或不在会话项目内。',
        'main_ambiguous': '主文档不唯一，请用 path 参数明确指定。',
        'document_not_found': '论文文档记录不可用，请重新选择主文档。',
    }
    return '无法检查：%s。%s' % (exc, hints.get(getattr(exc, 'code', ''), ''))


@tool_registry.tool_set(
    PAPER_QUALITY_TOOL_NAMES,
    category='project',
    description='Submission-readiness gate + review dossier for the paper')
def _handle_paper_quality_tool(task, tc, fn_name, tc_id, fn_args, rn,
                               round_entry, cfg, project_path, project_enabled,
                               all_tools=None):
    owner_user_id = task_user_id(task)
    conv_id = _conv_id(task, cfg)
    status = 'done'
    if fn_name == 'paper_scaffold':
        if str(fn_args.get('title') or '').strip():
            content = _scaffold_verdict(fn_args)
        else:
            content = '请提供论文标题（title 参数）。'
            status = 'error'
    elif not conv_id:
        content = '%s 需要一个会话上下文；当前调用没有会话标识。' % fn_name
        status = 'error'
    else:
        try:
            document, problem = _resolve_document(
                conv_id, owner_user_id, str(fn_args.get('path') or '').strip())
            if problem:
                content = problem
                status = 'error'
            elif fn_name == 'paper_review':
                content = _review_verdict(
                    document, str(fn_args.get('focus') or '').strip(),
                    owner_user_id)
            else:
                scope = str(fn_args.get('scope') or 'all')
                if scope not in ('all', 'source', 'pdf'):
                    scope = 'all'
                content = _check_verdict(document, scope, owner_user_id)
        except (BuildError, StorageError) as exc:
            content = _error_text(exc)
            status = 'error'
        except Exception:
            logger.exception('[paper_quality] unexpected failure')
            content = ('%s 内部错误；论文源文件与构建服务均未受影响，可重试。'
                       % fn_name)
            status = 'error'
    badge = ''
    if fn_name == 'paper_check' and status == 'done':
        headline = content.split('\n', 1)[0]
        if headline.startswith('✓'):
            badge = '✓ 就绪'
        elif headline.startswith('✗'):
            badge = '✗ 有错误'
    meta = _build_simple_meta(
        fn_name, content, source='PaperQuality',
        title={'paper_check': '投稿检查', 'paper_review': '论文审稿',
               'paper_scaffold': '论文骨架'}.get(fn_name, '论文质量'),
        snippet=content.split('\n', 1)[0][:160], badge=badge)
    if status != 'done':
        meta['writeOk'] = False
    _finalize_tool_round(task, rn, round_entry, [meta], status=status)
    return tc_id, content, False
