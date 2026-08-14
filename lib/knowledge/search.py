"""Hybrid lexical retrieval over the local knowledge corpus."""

from __future__ import annotations

import json
import math
import re

from lib.database import knowledge_repository as _repository
from lib.log import get_logger

from . import store

logger = get_logger(__name__)

_CANDIDATE_LIMIT = 80
_RESULT_LIMIT = 6
_MAX_RESULT_CHARS = 14_000

_QUESTION_FILLERS = (
    '能不能', '可不可以', '有没有', '是什么', '有哪些', '怎么样',
    '请问', '麻烦', '帮我', '告诉我', '我想知道', '关于', '一下',
    '什么', '哪些', '哪个', '怎么', '如何', '是否', '为何', '为什么',
    '需要', '应该', '可以', '能够',
)

_QUERY_EXPANSION_GROUPS = (
    (
        ('软件', '应用程序', 'software', 'application'),
        ('客户端', '应用', 'app', '工具', '系统', '程序', '软件'),
    ),
    (
        ('安装', '配置', '下载', '部署', '设置', '添加', 'install', 'setup'),
        ('安装', '配置', '下载', '部署', '设置', '添加'),
    ),
)


def _fts_query(tokens: list[str]) -> str:
    # Tokens are produced by our own conservative tokenizer. Quote every one
    # so punctuation can never become an FTS operator.
    return ' OR '.join('"' + token.replace('"', '""') + '"'
                       for token in tokens[:32])


def _normalized(text: str) -> str:
    return re.sub(r'\s+', '', (text or '').casefold())


def _candidate_rows(tokens: list[str]) -> list[dict]:
    return _repository.search_candidates(
        store._db_path(),
        fts_query=_fts_query(tokens),
        candidate_limit=_CANDIDATE_LIMIT,
    )


def _expanded_tokens(query: str, original: list[str]) -> list[str]:
    """Add a small, deterministic vocabulary for common intent wording.

    This stays local and explainable while bridging wording such as asking for
    "software to install" when a handbook calls it a client, app, tool, system,
    configuration, or download.  Original query evidence remains weighted
    much more strongly than these recall-only terms.
    """
    compact = _normalized(query)
    expanded: list[str] = []
    seen = set(original)
    for triggers, related in _QUERY_EXPANSION_GROUPS:
        if not any(_normalized(trigger) in compact for trigger in triggers):
            continue
        for term in related:
            for token in store.search_tokens(term, cap=16):
                if token not in seen:
                    seen.add(token)
                    expanded.append(token)
    return expanded[:32]


def _focus_tokens(query: str, fallback: list[str]) -> list[str]:
    """Remove question scaffolding before measuring lexical coverage.

    Candidate recall still uses every original n-gram.  Only reranking uses
    these focus terms, so wording such as "please tell me which" cannot beat
    the actual nouns, identifiers and actions in the question.
    """
    focused = query or ''
    for filler in sorted(_QUESTION_FILLERS, key=len, reverse=True):
        focused = focused.replace(filler, ' ')
    tokens = store.search_tokens(focused, cap=32)
    return tokens or fallback


def _evidence_text(text: str) -> str:
    value = (text or '').casefold()
    value = re.sub(r'!?(\[([^\]]*)\])\([^)]*\)', r'\2', value)
    value = re.sub(r'https?://\S+', '', value)
    value = re.sub(r'visual evidence:.*?ocr text:', '', value, count=1)
    value = re.sub(r'[*_`#>|\-]+', '', value)
    return re.sub(r'[^a-z0-9\u3400-\u9fff]+', '', value)


def _shingles(value: str, width: int = 5) -> set[str]:
    if len(value) <= width:
        return {value} if value else set()
    # Excerpts are bounded, but cap work further for pathological wide rows.
    value = value[:6000]
    return {value[index:index + width]
            for index in range(len(value) - width + 1)}


def _near_duplicate(left: str, right: str) -> bool:
    """Detect OCR/text and overlapping-chunk duplicates without embeddings."""
    a, b = _evidence_text(left), _evidence_text(right)
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 80 and shorter in longer:
        return True
    a_shingles, b_shingles = _shingles(a), _shingles(b)
    if not a_shingles or not b_shingles:
        return a == b
    overlap = len(a_shingles & b_shingles)
    containment = overlap / min(len(a_shingles), len(b_shingles))
    jaccard = overlap / len(a_shingles | b_shingles)
    return containment >= 0.84 or jaccard >= 0.72


def _merge_assets(target: list[dict], incoming: list[dict]) -> None:
    seen = {str(asset.get('id') or '') for asset in target}
    for asset in incoming:
        asset_id = str(asset.get('id') or '')
        if asset_id and asset_id not in seen:
            target.append(asset)
            seen.add(asset_id)


def _score(
    row: dict,
    query: str,
    tokens: list[str],
    expanded_tokens: list[str] | None = None,
) -> float:
    content = row['content'].casefold()
    metadata = f"{row['name']} {row['section']}".casefold()
    compact_query = _normalized(query)
    compact_content = _normalized(row['content'])
    hits = sum(1 for token in tokens if token.casefold() in content)
    meta_hits = sum(1 for token in tokens if token.casefold() in metadata)
    semantic_hits = sum(
        1 for token in (expanded_tokens or []) if token.casefold() in content)
    semantic_meta_hits = sum(
        1 for token in (expanded_tokens or []) if token.casefold() in metadata)
    coverage = hits / max(1, len(tokens))
    semantic_coverage = semantic_hits / max(1, len(expanded_tokens or []))
    phrase = 1.0 if len(compact_query) >= 2 and compact_query in compact_content else 0.0
    bm25 = max(0.0, -float(row.get('bm25_score') or 0.0))
    # bm25 values may span several orders of magnitude; log1p avoids one long
    # spreadsheet row overwhelming exact phrase/title evidence.
    # A PDF image proxy often repeats OCR that is already present in a parsed
    # text section. Keep it retrievable (and valuable for scanned documents),
    # but prefer the cleaner text evidence when both answer the same query.
    visual_proxy_penalty = (
        4.0 if row.get('assets') and content.startswith('visual evidence:')
        else 0.0)
    return (
        math.log1p(bm25 * 1_000_000)
        + coverage * 4.0
        + phrase * 4.0
        + meta_hits * 0.9
        + semantic_coverage * 1.8
        + semantic_meta_hits * 0.25
        - visual_proxy_penalty
    )


def _neighbor_context(row: dict) -> str:
    """Add a small adjacent chunk when the hit itself is unusually short."""
    content = str(row['content'])
    if len(content) >= 900:
        return content
    neighbor = row.get('next_content')
    if neighbor and row.get('next_section') == row.get('section'):
        return (content + '\n\n' + str(neighbor))[:2400]
    return content


def _public_asset(asset: dict) -> dict:
    asset_id = str(asset.get('id') or '')
    try:
        pages = json.loads(asset.get('pages_json') or '[]')
    except (TypeError, ValueError) as exc:
        logger.warning(
            '[Knowledge] invalid pages metadata for asset %s: %s',
            asset_id, exc)
        pages = []
    try:
        bbox = json.loads(asset.get('bbox_json') or '[]')
    except (TypeError, ValueError) as exc:
        logger.warning(
            '[Knowledge] invalid bounding-box metadata for asset %s: %s',
            asset_id, exc)
        bbox = []
    return {
        'id': asset_id,
        'kind': str(asset.get('kind') or 'image'),
        'mime_type': str(asset.get('mime_type') or 'image/jpeg'),
        'width': int(asset.get('width') or 0),
        'height': int(asset.get('height') or 0),
        'page': int(asset.get('page') or 0),
        'pages': pages if isinstance(pages, list) else [],
        'bbox': bbox if isinstance(bbox, list) else [],
        'caption': str(asset.get('caption') or ''),
        'description': str(asset.get('description') or ''),
        'enrichment_status': str(
            asset.get('enrichment_status') or 'not_requested'),
        'url': f'/api/v1/knowledge/assets/{asset_id}',
        'thumbnail_url': f'/api/v1/knowledge/assets/{asset_id}?thumbnail=1',
    }


def search(
    query: str,
    *,
    limit: int = _RESULT_LIMIT,
    require_enabled: bool = True,
) -> list[dict]:
    """Return grounded excerpts ranked across the local corpus.

    Model tool calls keep ``require_enabled=True``.  The management workbench
    can explicitly preview an index while model access is disabled, which is
    important when a user wants to validate evidence before turning it on.
    """
    query = (query or '').strip()
    if not query or (require_enabled and not store.tool_available()):
        return []
    original_tokens = store.search_tokens(query, cap=32)
    if not original_tokens:
        return []

    tokens = _focus_tokens(query, original_tokens)
    expanded_tokens = _expanded_tokens(query, original_tokens)
    # FTS has a hard token budget. Put focused nouns/actions and deterministic
    # intent bridges ahead of verbose question n-grams, and deduplicate them,
    # so "software to install" can genuinely recall "client/application".
    candidate_tokens = list(dict.fromkeys(
        tokens + expanded_tokens + original_tokens))
    rows = _candidate_rows(candidate_tokens)
    ranked = sorted(
        ((_score(row, query, tokens, expanded_tokens), row) for row in rows),
        key=lambda pair: (-pair[0], pair[1]['name'], pair[1]['ordinal']))

    results: list[dict] = []
    per_doc: dict[str, int] = {}
    per_section: dict[tuple[str, str], int] = {}
    result_limit = max(1, min(int(limit), 10))
    candidate_doc_count = max(1, len({row['document_id'] for row in rows}))
    per_doc_limit = max(2, math.ceil(result_limit / candidate_doc_count))
    total_chars = 0
    for score, row in ranked:
        if score <= 0:
            continue
        doc_id = row['document_id']
        # Preserve cross-document diversity without starving a small corpus
        # that currently contains one comprehensive handbook.
        if per_doc.get(doc_id, 0) >= per_doc_limit:
            continue
        section_key = (doc_id, _normalized(row.get('section') or ''))
        if section_key[1] and per_section.get(section_key, 0) >= 2:
            continue
        excerpt = _neighbor_context(row).strip()
        if not excerpt:
            continue
        public_assets = [_public_asset(asset)
                         for asset in (row.get('assets') or [])]
        duplicate = next(
            (result for result in results
             if _near_duplicate(excerpt, result['excerpt'])), None)
        if duplicate is not None:
            # Keep one textual answer while retaining any image provenance
            # found on its duplicate OCR proxy.
            _merge_assets(duplicate['assets'], public_assets)
            continue
        remaining = _MAX_RESULT_CHARS - total_chars
        if remaining < 300:
            break
        excerpt = excerpt[:min(2600, remaining)]
        results.append({
            'evidence_id': f'{doc_id}:{int(row["ordinal"])}',
            'source': row['name'],
            'section': row['section'],
            'location': row['location'],
            'excerpt': excerpt,
            'score': round(score, 4),
            'assets': public_assets,
        })
        total_chars += len(excerpt)
        per_doc[doc_id] = per_doc.get(doc_id, 0) + 1
        per_section[section_key] = per_section.get(section_key, 0) + 1
        if len(results) >= result_limit:
            break
    return results


__all__ = ['search']
