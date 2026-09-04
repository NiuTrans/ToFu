"""Time-aware, URL-grounded research shared by production capabilities.

The creative request is deliberately *not* used as the only search query.
That failed for product decks: words such as "宣传片和视频" biased one
unfiltered search toward old campaign material, while a later price/status
announcement never reached the content model. Research now fans out into a
fresh status lane, an official-source discovery lane, and an evergreen
background lane, preserving their temporal provenance on every card. Media
recipes may select a different current-freshness window while consuming the
same evidence bundle and deterministic current-fact gate.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from itertools import islice
from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'RESEARCH_RESUME_TTL_S', 'current_fact_errors',
    'evidence_checkpoint_version',
    'format_research_cards', 'gate_research_bundle',
    'research_topic', 'summarise_current_signals',
]

RESEARCH_RESUME_TTL_S = 6 * 60 * 60
_EVIDENCE_SCHEMA_VERSION = 'production-evidence-v1'
_QUERY_LANE_COUNT = 3
_MAX_RESULTS_PER_LANE = 12

_DATE_FIELDS = (
    'published_at', 'publishedAt', 'published', 'pub_date', 'pubDate',
    'datePublished', 'date', 'datetime', 'timestamp',
)
_CURRENT_LANES = frozenset({'current', 'official'})
_MONEY_RE = re.compile(
    r'(?<![\d.])(?:人民币\s*)?(?:[¥￥$€£]\s*)?'
    r'\d{1,7}(?:[.,]\d{1,4})?\s*(?:万元?|元|美元|港元|亿元|'
    r'USD|CNY|RMB|EUR|GBP)(?![A-Za-z])', re.IGNORECASE)
_PRICE_WORD_RE = re.compile(
    r'预售价|预售价格|起售价|指导价|售价|定价|价格|'
    r'pre[ -]?sale|preorder|price|priced|starts? at', re.IGNORECASE)
_STATUS_WORD_RE = re.compile(
    r'开启预售|启动预售|官宣|公布|正式上市|已上市|已正式发布|现已发布|'
    r'正式发布|发布会|最新进展|正式推出|'
    r'pre[ -]?sale|preorder|officially announced|has launched|was launched|'
    r'now available|released on|launch event', re.IGNORECASE)
_PRICE_ASSERTION_RE = re.compile(
    r'预售价|预售价格|起售价|指导价|定价为|价格(?:为|是|已公布)|'
    r'售价(?:为|是|已公布)|现已开启预售|'
    r'pre[ -]?sale price|priced at|price (?:is|was|has been) announced|starts? at',
    re.IGNORECASE)
_PRICE_SPECULATION_RE = re.compile(
    r'(?:预计|预测|猜测|传闻|网传|爆料|或为|可能|估算).{0,18}'
    r'(?:预售价|售价|价格)|'
    r'(?:预售价|售价|价格).{0,18}'
    r'(?:预计|预测|猜测|传闻|网传|或为|可能|估算)|'
    r'(?:rumou?r|expected|estimated|speculat|reportedly).{0,24}price|'
    r'price.{0,24}(?:rumou?r|expected|estimated|speculat|could|may be)',
    re.IGNORECASE)
_NON_PRICE_AMOUNT_RE = re.compile(
    r'订金|定金|意向金|小订|优惠|补贴|抵扣|保险|手续费|服务费|'
    r'deposit|reservation fee|discount|subsidy|rebate|insurance|service fee',
    re.IGNORECASE)
_MODEL_INSTRUCTION_TAIL_RE = re.compile(
    r'\s*[,，;；]\s*(?:都|全部)?\s*用\s*'
    r'(?:kimi|gpt|claude|gemini|qwen|deepseek)\b.*$', re.IGNORECASE)
_CREATIVE_FORMAT_TAIL_RE = re.compile(
    r'(?:的)?(?:品牌)?(?:宣传片和视频|宣传片及视频|演示文稿|幻灯片|PPTX?|deck)\s*$',
    re.IGNORECASE)
_GENERIC_SUBJECT_TOKENS = frozenset({
    'about', 'automotive', 'brand', 'car', 'cars', 'deck', 'latest',
    'official', 'product', 'story', 'video',
})


def evidence_checkpoint_version(*, freshness: str) -> str:
    """Version an evidence checkpoint by schema and freshness profile."""
    return f'{_EVIDENCE_SCHEMA_VERSION}:{freshness or "none"}'


def _is_zh(text: str) -> bool:
    return any('\u4e00' <= c <= '\u9fff' for c in text)


def _research_subject(topic: str) -> str:
    """Remove obvious creation/model instructions without rewriting the topic."""
    subject = _MODEL_INSTRUCTION_TAIL_RE.sub('', topic).strip(' ，,;；')
    candidate = _CREATIVE_FORMAT_TAIL_RE.sub('', subject).strip(' ，,;；')
    # Do not reduce a short topic such as "PPT" or "品牌宣传片" to nothing.
    if len(candidate) >= 4:
        subject = candidate
    return subject or topic


def _query_specs(topic: str, *, current_freshness: str) -> list[dict]:
    """Build complementary lanes; lane order is also card priority order."""
    subject = _research_subject(topic)
    if _is_zh(subject):
        current = f'{subject} 最新 官方 发布 进展 状态 预售 价格'
        official = f'{subject} 官网 官方公告 产品页 发布 参数'
    else:
        current = f'{subject} latest official update launch presale status price'
        official = f'{subject} official website product announcement release specs'
    return [
        {'lane': 'current', 'query': current, 'freshness': current_freshness,
         'deepen': False},
        # First-party pages are often evergreen URLs whose publication date is
        # absent, so this lane intentionally does not use a date filter.
        {'lane': 'official', 'query': official, 'freshness': '',
         'deepen': True},
        {'lane': 'background', 'query': subject, 'freshness': '',
         'deepen': False},
    ]


def _published_at(result: dict, *, url: str, point: str) -> str:
    """Preserve an engine date or conservatively recover a publication date.

    Dates anywhere in article text are often event dates ("will launch on
    July 30"), not publication dates.  Fallback extraction is therefore
    limited to URL paths and a date prefix emitted by search engines.
    """
    for key in _DATE_FIELDS:
        value = result.get(key)
        if value not in (None, ''):
            return ' '.join(str(value).split())[:80]
    url_patterns = (
        r'(?<!\d)(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})日?',
        r'/(20\d{2})/(\d{1,2})/(\d{1,2})(?:/|\b)',
        r'(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)',
    )
    for pattern in url_patterns:
        m = re.search(pattern, url)
        if m:
            year, month, day = (int(x) for x in m.groups())
            try:
                return datetime(year, month, day).date().isoformat()
            except ValueError as exc:
                logger.debug('[Production:research] invalid URL date %s: %s',
                             m.group(0), exc)
                continue
    prefix = point[:48]
    m = re.match(r'\s*(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})日?', prefix)
    if m:
        try:
            return datetime(*(int(x) for x in m.groups())).date().isoformat()
        except ValueError as exc:
            logger.debug('[Production:research] invalid result date prefix '
                         '%s: %s', m.group(0), exc)
    month_names = {name.lower(): number for number, name in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June', 'July',
         'August', 'September', 'October', 'November', 'December'), 1)}
    m = re.match(r'\s*([A-Za-z]+)\s+(\d{1,2}),\s*(20\d{2})\s*[-–—]', prefix)
    if m and m.group(1).lower() in month_names:
        try:
            return datetime(int(m.group(3)), month_names[m.group(1).lower()],
                            int(m.group(2))).date().isoformat()
        except ValueError as exc:
            logger.debug('[Production:research] invalid English date prefix '
                         '%s: %s', m.group(0), exc)
    return ''


def _normalise_result(result: dict, spec: dict) -> dict | None:
    if not isinstance(result, dict):
        return None
    url = str(result.get('url') or result.get('link') or '').strip()
    if not url.startswith(('http://', 'https://')):
        return None
    title = str(result.get('title') or '').strip()[:240]
    point = str(result.get('snippet') or result.get('content') or title).strip()
    point = ' '.join(point.split())[:700]
    if not point:
        return None
    lane = spec['lane']
    return {
        'title': title,
        'point': point,
        'url': url,
        'host': urlparse(url).netloc.lower().removeprefix('www.'),
        'published_at': _published_at(result, url=url, point=point),
        'query_lane': lane,
        'query_lanes': [lane],
        'freshness': spec['freshness'] or 'none',
        'source_hints': [],
    }


def _official_candidate_hints(card: dict, subject: str) -> list[str]:
    """Return ranking hints, never an authority verdict.

    Search often fetches a real product page but ranks it below articles. A
    distinctive Latin product token in the URL (for example ``skynomad``) is
    useful evidence for keeping that candidate in the limited card budget.
    """
    tokens = {
        token for token in re.findall(r'[a-z][a-z0-9_-]{3,}', subject.lower())
        if token not in _GENERIC_SUBJECT_TOKENS
    }
    url = str(card.get('url') or '').lower()
    hints = [f'subject-token-in-url:{token}'
             for token in sorted(tokens) if token in url]
    host = str(card.get('host') or '').lower()
    if host.endswith(('.gov', '.gov.cn', '.edu', '.edu.cn')):
        hints.append('institutional-domain')
    return hints


def _search_lane(search_fn, topic: str, spec: dict) -> tuple[list, str]:
    try:
        results = search_fn(
            spec['query'], max_results=_MAX_RESULTS_PER_LANE,
            user_question=topic,
            freshness=spec['freshness'], deepen=spec.get('deepen', False))
        # ``max_results`` is a request to an adapter, not an authority over
        # what it returns. Bound lazy and misbehaving providers here before a
        # three-lane fan-out can retain an arbitrary result stream in memory.
        return list(islice(results or (), _MAX_RESULTS_PER_LANE)), ''
    except Exception as exc:
        logger.warning('[Production:research] lane=%s query=%r failed: %s',
                       spec['lane'], spec['query'], exc)
        return [], f'{type(exc).__name__}: {exc}'


def _merge_card(cards: list[dict], by_url: dict[str, dict], card: dict) -> bool:
    """Add a card, merging provenance when two lanes find the same URL."""
    existing = by_url.get(card['url'])
    if existing is not None:
        lane = card['query_lane']
        if lane not in existing['query_lanes']:
            existing['query_lanes'].append(lane)
        if not existing.get('published_at') and card.get('published_at'):
            existing['published_at'] = card['published_at']
        for hint in card.get('source_hints') or []:
            if hint not in existing['source_hints']:
                existing['source_hints'].append(hint)
        return False
    cards.append(card)
    by_url[card['url']] = card
    return True


def summarise_current_signals(cards: list[dict]) -> dict:
    """Extract deterministic current-state signals for the outline gate.

    This does not decide whether a page's prose is true.  It only records that
    fresh/status-oriented evidence contains an announced price or release
    update, making it possible to reject an outline that ignores every current
    card or still claims no price exists.
    """
    status_ids: list[str] = []
    price_ids: list[str] = []
    prices: list[str] = []
    price_sources: dict[str, dict[str, set[str]]] = {}
    presale_ids: list[str] = []
    launched_ids: list[str] = []
    for card in cards or []:
        lanes = set(card.get('query_lanes') or [card.get('query_lane')])
        if not lanes.intersection(_CURRENT_LANES):
            continue
        text = ' '.join((str(card.get('title') or ''),
                         str(card.get('point') or '')))
        sid = str(card.get('id') or '')
        found_prices = []
        money_matches = list(_MONEY_RE.finditer(text))
        auxiliary_matches = list(_NON_PRICE_AMOUNT_RE.finditer(text))

        def _distance(a, b):
            if a.end() <= b.start():
                return b.start() - a.end()
            if b.end() <= a.start():
                return a.start() - b.end()
            return 0

        auxiliary_money = {
            min(money_matches, key=lambda money: _distance(money, marker))
            for marker in auxiliary_matches
        } if money_matches else set()
        for match in money_matches:
            context = text[max(0, match.start() - 56):match.end() + 56]
            asserted = bool(_PRICE_ASSERTION_RE.search(context))
            speculative = bool(_PRICE_SPECULATION_RE.search(context))
            if not asserted or speculative or match in auxiliary_money:
                continue
            value = re.sub(r'\s+', '', match.group(0)).replace('万元', '万')
            if value not in found_prices:
                found_prices.append(value)
        has_price = bool(found_prices and _PRICE_WORD_RE.search(text))
        has_status = bool(_STATUS_WORD_RE.search(text) or has_price)
        if has_status and sid and sid not in status_ids:
            status_ids.append(sid)
        if has_price and sid:
            price_ids.append(sid)
            for value in found_prices:
                if value not in prices:
                    prices.append(value)
                evidence = price_sources.setdefault(
                    value, {'source_ids': set(), 'hosts': set()})
                evidence['source_ids'].add(sid)
                host = str(card.get('host') or '')
                if host:
                    evidence['hosts'].add(host)
            if re.search(r'预售价|预售价格|pre[ -]?sale', text, re.IGNORECASE):
                presale_ids.append(sid)
        if sid and re.search(
                r'已正式发布|已经正式发布|现已发布|已上市|现已上市|'
                r'has launched|was launched|now available|released on',
                text, re.IGNORECASE):
            launched_ids.append(sid)
    price_evidence = []
    for value in prices:
        evidence = price_sources[value]
        hosts = sorted(evidence['hosts'])
        price_evidence.append({
            'value': value,
            'source_ids': sorted(evidence['source_ids']),
            'hosts': hosts,
            'corroborated': len(hosts) >= 2,
        })
    return {
        'status_source_ids': status_ids,
        'price_source_ids': price_ids,
        'presale_source_ids': presale_ids,
        'launched_source_ids': launched_ids,
        'price_values': prices,
        'corroborated_price_values': [
            item['value'] for item in price_evidence if item['corroborated']],
        'single_source_price_values': [
            item['value'] for item in price_evidence if not item['corroborated']],
        'price_evidence': price_evidence,
    }


def research_topic(topic: str, *, max_cards: int = 12,
                   current_freshness: str = 'month',
                   fallback_unfiltered_current: bool = False,
                   search_fn=None) -> dict:
    """Return one multi-lane evidence bundle for any production recipe.

    Args:
        topic: User request or subject. Obvious output/model instructions are
            removed before building search queries.
        max_cards: Maximum URL-grounded cards kept in the bundle.
        current_freshness: Search-engine freshness value for the current-state
            lane. Decks normally use ``month``; news video uses ``week``.
        fallback_unfiltered_current: Retry a starved current lane without a
            date filter. This preserves evergreen explainer behavior.
        search_fn: Optional injected search callable for capability seams and
            hermetic tests. It receives ``max_results``, ``user_question``,
            ``freshness`` and ``deepen`` keyword arguments.

    The function never raises for a search outage. Failure provenance stays in
    the returned query log and ``degraded`` / ``partial`` fields.
    """
    as_of = datetime.now().astimezone().isoformat(timespec='seconds')
    subject = _research_subject(topic)
    specs = _query_specs(topic, current_freshness=current_freshness)
    try:
        if search_fn is None:
            from lib.search_runtime import ensure_search_runtime
            search_fn = ensure_search_runtime().perform_web_search
        # One search can return a partial, engine-dependent set.  Independent
        # lanes run concurrently so freshness and authority do not add serial
        # latency or share a single point of retrieval failure.
        with ThreadPoolExecutor(
                max_workers=min(len(specs), _QUERY_LANE_COUNT)) as pool:
            futures = {spec['lane']: pool.submit(_search_lane, search_fn, topic, spec)
                       for spec in specs}
            lane_results = {
                spec['lane']: futures[spec['lane']].result()
                for spec in specs
            }
    except Exception as exc:
        logger.warning('[Production:research] fan-out failed for %r: %s',
                       topic, exc)
        return {'topic': topic, 'subject': subject, 'as_of': as_of,
                'cards': [], 'queries': specs,
                'current_signals': summarise_current_signals([]),
                'freshness_used': current_freshness or 'none',
                'degraded': True, 'reason': str(exc)}

    normalised: dict[str, list[dict]] = {}
    query_log: list[dict] = []
    for spec in specs:
        raw, error = lane_results.get(spec['lane'], ([], 'missing result'))
        rows = []
        for result in raw:
            card = _normalise_result(result, spec)
            if card is not None:
                if spec['lane'] == 'official':
                    card['source_hints'] = _official_candidate_hints(card, subject)
                rows.append(card)
        fallback = None
        if (spec['lane'] == 'current' and fallback_unfiltered_current
                and spec['freshness'] and len(rows) < 3):
            fresh_rows = list(rows)
            logger.info('[Production:research] %s-fresh current lane gave %d '
                        'card(s); retrying without freshness',
                        spec['freshness'], len(rows))
            fallback_spec = {**spec, 'freshness': ''}
            fallback_raw, fallback_error = _search_lane(
                search_fn, topic, fallback_spec)
            fallback_rows = []
            for result in fallback_raw:
                card = _normalise_result(result, fallback_spec)
                if card is not None:
                    fallback_rows.append(card)
            seen_urls = {card['url'] for card in fresh_rows}
            rows = fresh_rows + [card for card in fallback_rows
                                 if card['url'] not in seen_urls]
            fallback = {
                'freshness': 'none', 'result_count': len(fallback_rows),
                'error': fallback_error, 'adopted': bool(fallback_rows),
            }
            error = fallback_error
        if spec['lane'] == 'official':
            rows.sort(key=lambda card: len(card['source_hints']), reverse=True)
        normalised[spec['lane']] = rows
        query_log.append({**spec, 'result_count': len(rows), 'error': error,
                          'fallback': fallback})

    cards: list[dict] = []
    by_url: dict[str, dict] = {}
    # First pass guarantees representation from every successful lane while
    # reserving half the evidence budget for official-source candidates. This
    # avoids a common failure where the real product page ranked fifth or
    # sixth in that lane and four secondary articles crowded it out.
    current_quota = max(1, max_cards // 3)
    official_quota = max(1, max_cards // 2)
    background_quota = max(1, max_cards - current_quota - official_quota)
    quotas = {
        'current': current_quota,
        'official': official_quota,
        'background': background_quota,
    }
    for spec in specs:
        quota = quotas[spec['lane']]
        for card in normalised[spec['lane']][:quota]:
            if len(cards) >= max_cards:
                break
            _merge_card(cards, by_url, card)
    for spec in specs:
        quota = quotas[spec['lane']]
        for card in normalised[spec['lane']][quota:]:
            if len(cards) >= max_cards:
                break
            _merge_card(cards, by_url, card)

    for index, card in enumerate(cards, 1):
        card['id'] = f'S{index}'
    signals = summarise_current_signals(cards)
    failures = [q for q in query_log if q['error']]
    reason = ''
    if not cards:
        reason = 'all search lanes returned no URL-grounded cards'
    elif failures:
        reason = f'{len(failures)}/{len(query_log)} search lane(s) failed'
    current_log = next((q for q in query_log if q['lane'] == 'current'), {})
    fallback = current_log.get('fallback')
    if fallback is not None:
        current_cards = normalised.get('current') or []
        has_fresh = any(card.get('freshness') == current_freshness
                        for card in current_cards)
        has_unfiltered = any(card.get('freshness') == 'none'
                             for card in current_cards)
        if has_fresh and has_unfiltered:
            freshness_used = f'{current_freshness}+none'
        elif has_fresh:
            freshness_used = current_freshness or 'none'
        else:
            freshness_used = 'none'
    else:
        freshness_used = current_freshness or 'none'
    logger.info('[Production:research] %r → %d card(s), current=%d, '
                'freshness=%s, lanes=%s',
                topic, len(cards), len(signals['status_source_ids']),
                freshness_used,
                ','.join(f'{q["lane"]}:{q["result_count"]}' for q in query_log))
    return {
        'topic': topic,
        'subject': subject,
        'as_of': as_of,
        'cards': cards,
        'queries': query_log,
        'current_signals': signals,
        'freshness_used': freshness_used,
        'degraded': not bool(cards),
        'partial': bool(failures),
        'reason': reason,
    }


def gate_research_bundle(artifact: dict) -> list[str]:
    """Validate the common URL-grounded evidence floor."""
    cards = artifact.get('cards') or []
    if not cards:
        return ['research produced zero URL-grounded fact cards '
                '(every factual claim must carry a real source)']
    if not any(str(card.get('url') or '').lower().startswith(
            ('http://', 'https://')) for card in cards):
        return ['no fact card carries a real http(s) URL']
    return []


def format_research_cards(research: dict) -> str:
    """Format the shared evidence bundle for a content-model prompt."""
    cards = research.get('cards') or []
    return '\n'.join(
        f'[{card.get("id") or "S?"}] {card.get("point") or ""} '
        f'(published: {card.get("published_at") or "unknown"}; '
        f'lanes: {",".join(card.get("query_lanes") or [card.get("query_lane") or "unknown"])}; '
        f'candidate hints: {",".join(card.get("source_hints") or []) or "none"}; '
        f'source: {card.get("url") or ""})'
        for card in cards)


def current_fact_errors(research: dict, text: str, *,
                        cited_ids=None) -> list[str]:
    """Return deterministic temporal/citation defects in authored content.

    This is intentionally media-neutral. ``text`` may be a deck outline or a
    spoken-video script; ``cited_ids`` are the source ids its narrative units
    claim to use. The gate does not decide source truth, but it prevents a
    producer from ignoring all current evidence or contradicting detected
    price/release status.
    """
    cards = research.get('cards') or []
    if not cards:
        return []
    signals = (research.get('current_signals')
               or summarise_current_signals(cards))
    cited = set(cited_ids if cited_ids is not None
                else re.findall(r'\bS\d+\b', text or ''))
    errors: list[str] = []
    current_ids = set(signals.get('status_source_ids') or [])
    if current_ids and not cited.intersection(current_ids):
        errors.append(
            'content ignores every current-state source '
            f'({", ".join(sorted(current_ids))}); cite at least one recent '
            'release/availability/price card')

    prices = [re.sub(r'\s+', '', str(value))
              for value in signals.get('price_values') or []]
    compact_text = re.sub(r'\s+', '', text or '')
    exact_price_covered = bool(
        prices and any(value in compact_text for value in prices))
    negative_presale_claim = bool(re.search(
        r'(?:预售价|预售价格).{0,10}(?:尚未|未|没有|待).{0,8}'
        r'(?:公布|发布|确定)|'
        r'pre[ -]?sale price.{0,20}(?:not announced|unknown)',
        text or '', re.IGNORECASE))
    price_status_covered = bool(
        exact_price_covered or (not negative_presale_claim and re.search(
            r'(?:预售价|预售价格).{0,10}(?:已|为|是|明确|公布|发布|开启)|'
            r'已(?:经)?(?:公布|发布).{0,10}(?:价格|售价)|'
            r'pre[ -]?sale price|price (?:was |has been )?announced',
            text or '', re.IGNORECASE)))
    if prices and not price_status_covered:
        errors.append(
            'current research contains an announced price status '
            f'(candidate values: {", ".join(prices[:4])}) but the content '
            'does not acknowledge a presale/price announcement')
    if (signals.get('presale_source_ids') and not price_status_covered and
            (negative_presale_claim or re.search(
                r'价格.{0,12}(?:尚未|未|没有|待).{0,8}(?:公布|发布|确定)|'
                r'(?:售价|价格)仍是?未知|price.{0,20}'
                r'(?:not announced|unknown)', text or '', re.IGNORECASE))):
        errors.append(
            'content says price is unavailable although current evidence '
            'contains an announced presale price')
    if signals.get('launched_source_ids') and re.search(
            r'尚未(?:正式)?发布|等待(?:正式)?发布|即将发布|'
            r'not yet launched|awaiting launch', text or '', re.IGNORECASE):
        errors.append(
            'content describes the subject as awaiting launch although a '
            'current source says it has launched')
    return errors
