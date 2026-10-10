"""lib/tasks_pkg/handlers/paper_citations.py — Agent-facing citation tools.

Track C (manubot model): citations are searched in real literature
(OpenAlex), fetched by persistent identifier (Crossref/OpenAlex) and inserted
through the journaled project write tools, so a hallucinated reference cannot
enter the manuscript. This handler only resolves the owned document (the SAME
``_resolve_document`` the build handler uses), drives
``lib/paper/citations`` and formats Chinese verdicts — it never performs
network I/O or file mutation itself.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.paper import citations
from lib.paper.builds.errors import BuildError
from lib.storage.errors import StorageError
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.handlers.paper_build import _conv_id, _resolve_document
from lib.tasks_pkg.manager._registry import task_user_id
from lib.tools.paper_citations import PAPER_CITATION_TOOL_NAMES

logger = get_logger(__name__)

_LISTED_SEARCH_ROWS = 12
_LISTED_AUDIT_ROWS = 16

_STATUS_LABEL = {
    'verified': '✓ 已核实',
    'mismatch': '✗ 元数据不符',
    'not_found': '✗ 疑似幻觉（OpenAlex 未找到）',
    'retracted': '⚠ 已撤稿',
    'skipped': '– 跳过',
}


def _error_text(exc) -> str:
    if isinstance(exc, StorageError):
        return '引用存储暂不可用（%s）：%s' % (exc.code, exc)
    hints = {
        'citation_backend': '文献服务（OpenAlex/Crossref）暂不可用，请稍后重试。',
        'citation_query': '请检查标识符或检索词（DOI / OpenAlex ID / arXiv ID）。',
        'citation_not_found': '该标识符在公开索引中不存在；请用 '
                              'paper_cite_search 重新检索真实文献。',
        'invalid_key': '引用键仅允许字母数字与 :_./-，最长 64 字符。',
        'anchor_not_found': '锚点未命中：请提供在文件中出现的原文片段。',
        'anchor_ambiguous': '锚点不唯一：请提供更长的原文片段。',
        'bib_limit': '.bib 已达上限（4096 条 / 4 MiB）；请拆分子库。',
        'bibtex_invalid': '获取到的 BibTeX 无法解析，请重试或换用 DOI。',
        'source_path': '目标路径不可用或不在会话项目内。',
        'source_limit': '源文件超出读取预算。',
        'write_failed': '日志化写入失败；修改未生效，可重试。',
    }
    return '无法完成引用操作：%s。%s' % (
        exc, hints.get(getattr(exc, 'code', ''), ''))


def _search_verdict(fn_args) -> tuple[str, str]:
    query = str(fn_args.get('query') or '').strip()
    if not query:
        return '请提供检索关键词（query 参数），例如论文标题或作者+主题。', 'error'
    works = citations.search_works(query, limit=fn_args.get('limit') or 8)
    if not works:
        return ('未检索到匹配文献（OpenAlex）。请更换关键词重试；'
                '严禁凭空编造参考文献。'), 'done'
    lines = ['检索到 %d 篇真实文献（OpenAlex）：' % len(works)]
    for work in works[:_LISTED_SEARCH_ROWS]:
        authors = '、'.join(work['authors'][:3]) or '未知作者'
        flags = '　⚠ 已撤稿！禁止引用' if work['is_retracted'] else ''
        lines.append(
            '  - `%s` %s（%s，%s，被引 %d）%s\n'
            '    作者：%s；DOI：`%s`；OpenAlex：`%s`'
            % (work['bibtex_key'], work['title'] or '(无标题)',
               work['year'] or '?', work['venue'] or '未知出处',
               work['cited_by_count'], flags, authors,
               work['doi'] or '无', work['openalex_id'] or '无'))
    lines.append('用 paper_cite_insert 并传入所选文献的 doi 插入引用；'
                 '键名建议用列出的 bibtex_key。绝不要手写参考文献条目。')
    return '\n'.join(lines), 'done'


def _insert_verdict(base: str, main_file: str, fn_args, task,
                    conv_id: str) -> tuple[str, str]:
    identifier = (str(fn_args.get('doi') or '').strip()
                  or str(fn_args.get('openalex_id') or '').strip()
                  or str(fn_args.get('arxiv') or '').strip())
    if not identifier:
        return ('请提供文献标识符：doi、openalex_id 或 arxiv 之一'
                '（先调用 paper_cite_search 获取真实 DOI）。'), 'error'
    bibtex, proposed_key = citations.fetch_bibtex(identifier)
    key = citations.validate_key(fn_args.get('key') or proposed_key)
    if key != proposed_key:
        bibtex = citations.rewrite_bibtex_key(bibtex, key)
    bib_rel = (str(fn_args.get('cite_file') or '').strip()
               or citations.default_bib_path(base, main_file))
    task_id = task.get('id')
    appended = citations.append_bib_entries(
        base, bib_rel, [(bibtex, key)], conv_id=conv_id, task_id=task_id)
    lines = []
    if appended['added']:
        lines.append('已将真实文献条目追加到 `%s`%s（键 `%s`，可撤销）。'
                     % (bib_rel, '（新建）' if appended['created'] else '', key))
    else:
        lines.append('条目已存在于 `%s`（键 `%s`），按去重跳过。' % (bib_rel, key))
    anchor = str(fn_args.get('anchor') or '')
    if anchor:
        tex_rel = (str(fn_args.get('tex_file') or '').strip() or main_file)
        if not tex_rel:
            return ('无法确定插入目标 .tex 文件：该文档没有主文件，'
                    '请用 tex_file 参数指定。'), 'error'
        inserted = citations.insert_cite(
            base, tex_rel, anchor, key,
            position=str(fn_args.get('anchor_position') or 'after'),
            conv_id=conv_id, task_id=task_id)
        if inserted['inserted']:
            lines.append('已在 `%s` 锚点后插入 \\cite{%s}（可撤销）。'
                         % (tex_rel, key))
        else:
            lines.append('`%s` 锚点处已存在 \\cite{%s}，无需重复插入。'
                         % (tex_rel, key))
    status = citations.bibliography_status(base, bib_rel)
    if not status['referenced']:
        stem = bib_rel[:-4] if bib_rel.lower().endswith('.bib') else bib_rel
        lines.append(
            '注意：未在任何 .tex 源中发现引用该文献库的 \\bibliography{%s} 或 '
            '\\addbibresource{%s}；请添加其一（并确认已加载 '
            'natbib/biblatex），否则参考文献不会输出。'
            % (stem, bib_rel.rsplit('/', 1)[-1]))
    lines.append('完成后请调用 paper_build 复验编译（undefined citation 会在'
                 '诊断中报告）；投稿前建议运行 paper_citation_audit。')
    return '\n'.join(lines), 'done'


def _audit_verdict(base: str, main_file: str) -> tuple[str, str]:
    report = citations.audit_document(base, main_file=main_file)
    if not report['bib_files'] and not report['tex_files']:
        return '文档根目录下没有 .tex/.bib 文件可审计。', 'error'
    rows = report['verifications']
    counts = {name: 0 for name in _STATUS_LABEL}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    lines = ['引用审计：%d 个 .bib 文件 / %d 条条目，%d 个 .tex 文件。'
             % (len(report['bib_files']), report['entries_total'],
                len(report['tex_files'])),
             'OpenAlex 核验：已核实 %d，元数据不符 %d，疑似幻觉 %d，已撤稿 %d，'
             '跳过 %d。' % (counts.get('verified', 0), counts.get('mismatch', 0),
                          counts.get('not_found', 0), counts.get('retracted', 0),
                          counts.get('skipped', 0))]
    shown = 0
    for row in rows:
        if row['status'] == 'verified' and len(rows) > _LISTED_AUDIT_ROWS:
            continue
        evidence = row.get('evidence') or {}
        detail = ''
        if row['status'] in ('mismatch', 'retracted'):
            detail = '（匹配到：%s，%s，相似度 %.2f）' % (
                str(evidence.get('matched_title') or '?')[:80],
                evidence.get('matched_year') or '?',
                float(evidence.get('similarity') or 0.0))
        elif row['status'] == 'skipped' and row.get('note'):
            detail = '（%s）' % row['note'][:80]
        lines.append('  %s `%s`%s'
                     % (_STATUS_LABEL.get(row['status'], row['status']),
                        row['key'], detail))
        shown += 1
        if shown >= _LISTED_AUDIT_ROWS:
            lines.append('  …其余 %d 条从略' % (len(rows) - shown))
            break
    if report['duplicates']:
        lines.append('✗ 重复键：%s' % '、'.join(
            '`%s`' % key for key in report['duplicates'][:8]))
    if report['undefined']:
        lines.append('✗ 未定义引用（.bib 中无条目）：%s' % '、'.join(
            '`%s`' % key for key in report['undefined'][:8]))
    if report['unused']:
        lines.append('注意：%d 条条目从未被引用（如 %s）。'
                     % (len(report['unused']),
                        '、'.join('`%s`' % k for k in report['unused'][:4])))
    problems = (counts.get('mismatch', 0) + counts.get('not_found', 0)
                + counts.get('retracted', 0) + len(report['duplicates'])
                + len(report['undefined']))
    if problems:
        lines.append('请逐条处理上述问题：疑似幻觉条目必须用 paper_cite_search '
                     '+ paper_cite_insert 换成真实文献；撤稿文献禁止引用。'
                     '修复后运行 paper_build 复验。')
        return '\n'.join(lines), 'error'
    lines.append('全部已核验条目均为真实文献。投稿/提交前可用 paper_build '
                 '做最终编译验证。')
    return '\n'.join(lines), 'done'


@tool_registry.tool_set(
    PAPER_CITATION_TOOL_NAMES,
    category='project',
    description='Search, insert and verify real literature citations')
def _handle_paper_citations_tool(task, tc, fn_name, tc_id, fn_args, rn,
                                 round_entry, cfg, project_path,
                                 project_enabled, all_tools=None):
    owner_user_id = task_user_id(task)
    conv_id = _conv_id(task, cfg)
    status = 'done'
    if not conv_id:
        content = '%s 需要一个会话上下文；当前调用没有会话标识。' % fn_name
        status = 'error'
    else:
        try:
            if fn_name == 'paper_cite_search':
                content, status = _search_verdict(fn_args)
            else:
                document, problem = _resolve_document(
                    conv_id, owner_user_id,
                    str(fn_args.get('path') or '').strip())
                if problem:
                    content, status = problem, 'error'
                else:
                    base = str(document.get('project_root') or '')
                    main_file = str(document.get('main_file')
                                    or document.get('path') or '')
                    if not base:
                        content = ('该论文文档没有本地项目根目录，无法读写引用'
                                   '文件；请先挂载包含 TeX 项目的目录。')
                        status = 'error'
                    elif fn_name == 'paper_cite_insert':
                        content, status = _insert_verdict(
                            base, main_file, fn_args, task, conv_id)
                    else:
                        content, status = _audit_verdict(base, main_file)
        except (BuildError, StorageError) as exc:
            content = _error_text(exc)
            status = 'error'
        except Exception:
            logger.exception('[paper_citations] unexpected failure')
            content = ('引用工具内部错误；项目文件未被修改（已发生的写入均可'
                       '通过撤销回滚），可重试。')
            status = 'error'
    title = {'paper_cite_search': '文献检索',
             'paper_cite_insert': '插入引用',
             'paper_citation_audit': '引用审计'}.get(fn_name, fn_name)
    meta = _build_simple_meta(
        fn_name, content, source='PaperCitations', title=title,
        snippet=content.split('\n', 1)[0][:160])
    if status != 'done':
        meta['writeOk'] = False
    _finalize_tool_round(task, rn, round_entry, [meta], status=status)
    return tc_id, content, False
