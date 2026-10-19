"""lib/tasks_pkg/handlers/paper_edits.py — Agent-facing paper edit proposals.

The model proposes exact-anchor search/replace hunks for USER REVIEW instead
of writing files directly: the proposal is pre-validated against the live
sources (non-matching hunks are marked failed), parked in the bounded
owner-scoped proposal store, and the browser preview panel renders every hunk
as an accept/reject diff. Accepted hunks land through the same journaled
write tools as any agent edit. This handler never mutates project files, so
it is not a write tool.
"""
from __future__ import annotations

from lib.log import get_logger
from lib.paper.builds.errors import BuildError
from lib.agent_core.storage_errors import StorageError
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.manager._registry import task_user_id
from lib.tools.paper_edits import PAPER_EDITS_TOOL_NAMES

from .paper_build import _conv_id, _resolve_document

logger = get_logger(__name__)

_MAX_EDITS = 20


def _normalize_edits(raw):
    """Shape fn_args edits for the proposal store; None when nothing usable."""
    if not isinstance(raw, list):
        return None
    edits = []
    for i, entry in enumerate(raw[:_MAX_EDITS], 1):
        if not isinstance(entry, dict):
            return None
        path = str(entry.get('path') or '').strip()
        search = entry.get('search')
        if not path or not isinstance(search, str) or not search:
            return None
        edits.append({'path': path, 'search': search,
                      'replace': entry.get('replace')
                      if isinstance(entry.get('replace'), str) else '',
                      'description': str(entry.get('description') or '')[:200]})
    return edits or None


def _document_id(document: dict, owner_user_id: int) -> str:
    """The resolved logical identity, registering the binding when needed."""
    document_id = document.get('document_id')
    if document_id:
        return document_id
    from lib.paper.builds.document_repository import PaperDocumentRepository
    repository = PaperDocumentRepository(owner_user_id)
    resolved = repository.resolve([document])
    if resolved and resolved[0]:
        return resolved[0]
    return repository.register(document)['document_id']


def _verdict(proposal: dict) -> str:
    failed = [row for row in proposal['edits'] if row['status'] == 'failed']
    lines = ['proposal_id: %s' % proposal['proposal_id'],
             '已生成修改提案 %s：%d 处修改待确认（%d 处预检失败）。请在预览面板中逐条接受或拒绝。'
             % (proposal['proposal_id'][:12], len(proposal['edits']), len(failed))]
    for row in failed:
        lines.append('  [%d] %s 预检失败：%s'
                     % (row['index'], row['path'], (row.get('error') or '')[:200]))
    return '\n'.join(lines)


@tool_registry.tool_set(
    PAPER_EDITS_TOOL_NAMES,
    category='project',
    description='Propose journaled paper edits for user review')
def _handle_paper_edits_tool(task, tc, fn_name, tc_id, fn_args, rn,
                             round_entry, cfg, project_path, project_enabled,
                             all_tools=None):
    owner_user_id = task_user_id(task)
    conv_id = _conv_id(task, cfg)
    status = 'done'
    if not conv_id:
        content = '%s 需要一个会话上下文；当前调用没有会话标识。' % fn_name
    else:
        try:
            document, problem = _resolve_document(
                conv_id, owner_user_id, str(fn_args.get('path') or '').strip())
            if problem:
                content = problem
                status = 'error'
            else:
                edits = _normalize_edits(fn_args.get('edits'))
                if edits is None:
                    content = ('请提供 edits 数组（最多 %d 项），每项含 path 和 search'
                               '（replace 省略表示删除锚点）。' % _MAX_EDITS)
                    status = 'error'
                else:
                    from lib.paper.builds.proposals import create_proposal
                    proposal = create_proposal(
                        owner_user_id=owner_user_id,
                        document_id=_document_id(document, owner_user_id),
                        conv_id=conv_id, edits=edits)
                    content = _verdict(proposal)
        except (BuildError, StorageError) as exc:
            content = '无法生成修改提案：%s。' % exc
            status = 'error'
        except Exception:
            logger.exception('[paper_edits] unexpected failure')
            content = 'paper_propose_edits 内部错误；未修改任何文件，可重试。'
            status = 'error'
    meta = _build_simple_meta(fn_name, content, source='PaperEdits',
                              title='修改提案',
                              snippet=content.split('\n', 1)[0][:160])
    if status != 'done':
        meta['writeOk'] = False
    _finalize_tool_round(task, rn, round_entry, [meta], status=status)
    return tc_id, content, False
