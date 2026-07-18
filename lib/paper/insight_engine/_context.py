"""Reader context (the transfer moat).

Assembles the "reader context" block — the reader's paper library + relevant
stored memories — injected into the insight prompt so the pass can build
concrete "this connects to «a paper you already read»" bridges. Every helper is
best-effort: any failure degrades to an empty block, never an exception.
"""

from lib.log import get_logger

from ._config import _CTX_LIBRARY_MAX, _CTX_MEMORY_MAX

logger = get_logger(__name__)


def _context_query(report_md: str, paper_text: str) -> str:
    """Derive a short relevance query from the report title + TL;DR / paper head."""
    head = (report_md or '')[:1200]
    if not head:
        head = (paper_text or '')[:800]
    return head


def _library_context(phash: str, query: str):
    """Recent OTHER papers the reader has in their library, ranked by relevance.

    Returns a list of ``{'title', 'arxiv_id'}`` dicts (best-effort; empty on any
    failure — the pass still runs without a library).
    """
    try:
        from lib.database import get_db, get_thread_db
        try:
            db = get_db()
        except RuntimeError as e:
            logger.debug('[Paper:Insight] no request-context DB, using thread DB: %s', e)
            db = get_thread_db()
        rows = db.execute(
            "SELECT title, arxiv_id, paper_hash FROM paper_library "
            "WHERE paper_hash != ? AND title != '' "
            "ORDER BY updated_at DESC LIMIT 40",
            (phash or '',)).fetchall()
    except Exception as e:
        logger.debug('[Paper:Insight] Library context unavailable: %s', e)
        return []

    items = []
    for r in rows or []:
        try:
            items.append({'title': r['title'] or '', 'arxiv_id': r['arxiv_id'] or ''})
        except Exception as e:
            logger.debug('[Paper:Insight] skipping malformed library row: %s', e)
            continue
    items = [it for it in items if it['title']]
    if not items:
        return []

    # Relevance-rank titles against the current paper so the bridge candidates
    # are topical, not just recent.
    try:
        from lib.memory.relevance import score_items
        scored = score_items(query, [it['title'] for it in items])
        if scored:
            ranked = [items[i] for i, _ in scored]
            # Keep any positive-scoring hits; if none scored, fall back to recency.
            items = ranked or items
    except Exception as e:
        logger.debug('[Paper:Insight] Library relevance rank failed: %s', e)
    return items[:_CTX_LIBRARY_MAX]


def _memory_context(query: str, project_path=None):
    """Relevant stored memories (the reader's problems/notes), ranked by BM25.

    Returns a list of ``{'name', 'description'}`` dicts (best-effort; empty on
    any failure).
    """
    try:
        from lib.memory.relevance import filter_relevant_memories
        from lib.memory.storage import get_eligible_memories
        mems = get_eligible_memories(project_path)
        if not mems:
            return []
        top = filter_relevant_memories(mems, query, top_k=_CTX_MEMORY_MAX)
    except Exception as e:
        logger.debug('[Paper:Insight] Memory context unavailable: %s', e)
        return []
    out = []
    for m in top[:_CTX_MEMORY_MAX]:
        name = (m.get('name') or '').strip()
        desc = (m.get('description') or '').strip()
        if name or desc:
            out.append({'name': name, 'description': desc})
    return out


def _build_reader_context(phash, report_md, paper_text, ui_lang, project_path=None):
    """Assemble the "reader context" block injected into the insight prompt.

    Empty string when the reader has no library / memories — the prompt tells
    the model to be honest about that rather than manufacture a link.
    """
    query = _context_query(report_md, paper_text)
    library = _library_context(phash, query)
    memories = _memory_context(query, project_path)
    if not library and not memories:
        return ''

    zh = ui_lang == 'zh'
    lines = ['## READER CONTEXT (for transfer — do NOT restate; use to build bridges)'
             if not zh else
             '## 读者背景（用于迁移——不要复述；用来搭桥）']
    if library:
        lines.append('\n### Papers the reader has already read (their library):'
                     if not zh else '\n### 读者已经读过的论文（他的文库）：')
        for it in library:
            aid = f" (arXiv:{it['arxiv_id']})" if it.get('arxiv_id') else ''
            lines.append(f"- {it['title']}{aid}")
    if memories:
        lines.append('\n### Problems / notes the reader cares about (their memory store):'
                     if not zh else '\n### 读者关心的问题/笔记（他的记忆库）：')
        for m in memories:
            desc = f" — {m['description']}" if m.get('description') else ''
            lines.append(f"- {m['name']}{desc}")
    return '\n'.join(lines)
