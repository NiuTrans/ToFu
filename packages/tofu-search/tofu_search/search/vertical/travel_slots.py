"""Deterministic slot extraction for travel queries.

The travel verticals need five to eight parameters (route, dates, passengers,
cabin, trip type) where every other vertical needs one identifier. Extracting
them with an LLM is not an option: the vertical layer has always been zero-LLM
and ``llm_function`` may not be configured at all.

So this module is a pure, side-effect-free parser: text in, dataclass out, no
network and no clock read — ``today`` is always injected by the caller so the
tests cannot rot as the calendar moves.

Both parsers return None when a query lacks the minimum viable slots (no route,
no resolvable date, or a date in the past). Returning None means the handler
never issues a request it knows the upstream will reject.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

__all__ = [
    'FlightSlots', 'HotelSlots',
    'parse_flight_query', 'parse_hotel_query',
    'looks_like_flight', 'looks_like_hotel',
]

_FLIGHT_CUES = ('机票', '航班', '飞机', '飞', '航线', 'flight', 'flights',
                'airfare', 'fly', 'flying', 'airline')
_HOTEL_CUES = ('酒店', '住宿', '宾馆', '民宿', '客栈', '住', 'hotel', 'hotels',
               'stay', 'accommodation', 'lodging', 'resort')

# A flight cue that is only a substring of an unrelated word would misfire; the
# short CJK cues ('飞', '住') are checked last and only in the CJK branch.
_FLIGHT_STRONG = ('机票', '航班', 'flight', 'flights', 'airfare', 'airline')
_HOTEL_STRONG = ('酒店', '住宿', '宾馆', '民宿', '客栈', 'hotel', 'hotels',
                 'accommodation', 'lodging')

_CABINS = {
    'ECONOMY': ('经济舱', '经济', 'economy', 'coach'),
    'PREMIUM_ECONOMY': ('超级经济舱', '豪华经济舱', 'premium economy',
                        'premium-economy'),
    'BUSINESS': ('商务舱', '商务', 'business'),
    'FIRST': ('头等舱', '头等', 'first class', 'first-class'),
}

_ROUND_TRIP_CUES = ('往返', '来回', '双程', 'round trip', 'round-trip',
                    'roundtrip', 'return')

_CN_NUM = {'一': 1, '两': 2, '二': 2, '三': 3, '四': 4, '五': 5,
           '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}

_WEEKDAYS = {
    '周一': 0, '星期一': 0, 'monday': 0, '周二': 1, '星期二': 1, 'tuesday': 1,
    '周三': 2, '星期三': 2, 'wednesday': 2, '周四': 3, '星期四': 3, 'thursday': 3,
    '周五': 4, '星期五': 4, 'friday': 4, '周六': 5, '星期六': 5, 'saturday': 5,
    '周日': 6, '周天': 6, '星期日': 6, '星期天': 6, 'sunday': 6,
}

_PLACE_TYPE_CUES = (
    ('景点', ('外滩', '西湖', '故宫', '天安门', '迪士尼', '兵马俑', '景点',
              '公园', '广场', '塔', '寺', '湖', '山', '海滩', '古城')),
    ('机场', ('机场', 'airport')),
    ('火车站', ('火车站', '高铁站', 'station')),
)

_STOPWORDS_PLACE = ('的', '附近', '周边', '一带', '区域', 'near', 'nearby',
                    'around', 'in', 'at')


@dataclass
class FlightSlots:
    """Resolved ``searchFlights`` parameters plus the raw route text."""

    from_text: str
    to_text: str
    from_date: str
    trip_type: str = 'ONE_WAY'
    ret_date: str = ''
    adults: int = 1
    children: int = 0
    cabin: str = 'ECONOMY'
    notes: list = field(default_factory=list)


@dataclass
class HotelSlots:
    """Resolved ``searchHotels`` parameters."""

    place: str
    check_in_date: str
    place_type: str = '城市'
    stay_nights: int = 1
    star_ratings: list = field(default_factory=list)
    origin_query: str = ''
    notes: list = field(default_factory=list)


def _has_any(text, cues):
    low = text.lower()
    return any(c in low for c in cues)


def looks_like_flight(q):
    """True when the query is asking about flights."""
    return _has_any(q, _FLIGHT_STRONG) or bool(
        re.search(r'[\u4e00-\u9fff]{2,}\s*飞\s*[\u4e00-\u9fff]{2,}', q))


def looks_like_hotel(q):
    """True when the query is asking about hotels / accommodation."""
    if _has_any(q, _HOTEL_STRONG):
        return True
    return bool(re.search(r'住\s*\d+\s*晚', q)) or bool(
        re.search(r'\bstay\b.*\bnights?\b', q, re.IGNORECASE))


def _parse_explicit_date(text, today):
    """Find the first absolute date. Returns (date, matched_span) or (None, None)."""
    m = re.search(r'(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})', text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.span()
        except ValueError:
            return None, None
    m = re.search(r'(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?', text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = today.year
        try:
            cand = date(year, month, day)
        except ValueError:
            return None, None
        if cand < today:
            try:
                cand = date(year + 1, month, day)
            except ValueError:
                return None, None
        return cand, m.span()
    # Bare M/D (no year) — only when it is not part of a longer number run.
    m = re.search(r'(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])', text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            cand = date(today.year, month, day)
        except ValueError:
            return None, None
        if cand < today:
            try:
                cand = date(today.year + 1, month, day)
            except ValueError:
                return None, None
        return cand, m.span()
    return None, None


def _parse_relative_date(text, today):
    """Resolve relative day phrases. Returns (date, matched_span) or (None, None)."""
    low = text.lower()
    for phrase, delta in (('后天', 2), ('明天', 1), ('今天', 0),
                          ('大后天', 3), ('tomorrow', 1), ('today', 0)):
        idx = low.find(phrase)
        if idx >= 0:
            return today + timedelta(days=delta), (idx, idx + len(phrase))
    m = re.search(r'(\d+)\s*天后', text)
    if m:
        return today + timedelta(days=int(m.group(1))), m.span()
    m = re.search(r'in\s+(\d+)\s+days?', low)
    if m:
        return today + timedelta(days=int(m.group(1))), m.span()

    for phrase, target in _WEEKDAYS.items():
        idx = low.find(phrase)
        if idx < 0:
            continue
        # "下周二" / "next tuesday" push into the following week.
        prefix = low[max(0, idx - 4):idx]
        next_week = ('下' in prefix) or ('next' in prefix)
        ahead = (target - today.weekday()) % 7
        if ahead == 0:
            ahead = 7
        if next_week and ahead < 7:
            ahead += 7
        span_start = idx
        if next_week:
            cut = max(low.rfind('下', 0, idx), low.rfind('next', 0, idx))
            if cut >= 0:
                span_start = cut
        return today + timedelta(days=ahead), (span_start, idx + len(phrase))
    return None, None


_TEMPORAL_PATTERNS = (
    r'20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}',
    r'\d{1,2}\s*月\s*\d{1,2}\s*[日号]?',
    r'(?<![\d/])\d{1,2}/\d{1,2}(?![\d/])',
    r'大?后天', r'明天', r'今天',
    r'\d+\s*天后',
    r'\bin\s+\d+\s+days?\b',
    r'\btomorrow\b', r'\btoday\b',
    r'(?:下{1,2}|本|这)?(?:周|星期)[一二三四五六日天]',
    r'\b(?:next\s+|this\s+)?(?:monday|tuesday|wednesday|thursday|friday|'
    r'saturday|sunday)\b',
)


def _scrub_temporal(text):
    """Strip date/time phrases so they cannot be mistaken for a place name.

    ``后天广州到深圳`` must yield origin ``广州``, not ``后天广州`` — the route
    regexes match greedy CJK runs and would otherwise swallow the date word.
    """
    out = text
    for pat in _TEMPORAL_PATTERNS:
        out = re.sub(pat, ' ', out, flags=re.IGNORECASE)
    return re.sub(r'\s{2,}', ' ', out).strip()


def _resolve_date(text, today):
    d, span = _parse_explicit_date(text, today)
    if d is None:
        d, span = _parse_relative_date(text, today)
    return d, span


def _parse_date_range(text, today):
    """Detect an explicit range like ``8/3-8/5`` or ``8月3日到8月5日``.

    Returns (start, end) dates, or (None, None).
    """
    m = re.search(r'(\d{1,2})[/月](\d{1,2})\s*[日号]?\s*[-~—到至]\s*'
                  r'(\d{1,2})[/月](\d{1,2})\s*[日号]?', text)
    if not m:
        m = re.search(r'(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\s*[-~—到至]\s*'
                      r'(20\d{2})[-/](\d{1,2})[-/](\d{1,2})', text)
        if not m:
            return None, None
        try:
            start = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            end = date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        except ValueError:
            return None, None
        return start, end
    try:
        start = date(today.year, int(m.group(1)), int(m.group(2)))
        end = date(today.year, int(m.group(3)), int(m.group(4)))
    except ValueError:
        return None, None
    if start < today:
        try:
            start = date(today.year + 1, start.month, start.day)
            end = date(today.year + 1, end.month, end.day)
        except ValueError:
            return None, None
    if end < start:
        try:
            end = date(end.year + 1, end.month, end.day)
        except ValueError:
            return None, None
    return start, end


def _parse_route(text):
    """Extract (origin, destination) place text, or (None, None)."""
    patterns = (
        r'([\u4e00-\u9fff]{2,10})\s*(?:到|至|飞往|飞|去)\s*([\u4e00-\u9fff]{2,10})',
        r'([\u4e00-\u9fff]{2,10})\s*[-→~]\s*([\u4e00-\u9fff]{2,10})',
        r'\bfrom\s+([A-Za-z][A-Za-z\s]{1,20}?)\s+to\s+([A-Za-z][A-Za-z\s]{1,20}?)'
        r'(?=\s*(?:on|at|,|$))',
        r'\b([A-Z]{3})\s*(?:to|-|→)\s*([A-Z]{3})\b',
        r'\b([A-Za-z][A-Za-z\s]{1,20}?)\s+to\s+([A-Za-z][A-Za-z\s]{1,20}?)'
        r'(?=\s*(?:on|at|,|$))',
    )
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        origin = _clean_place(m.group(1))
        dest = _clean_place(m.group(2))
        if origin and dest and origin != dest:
            return origin, dest
    return None, None


def _clean_place(raw):
    out = raw.strip()
    for cue in _FLIGHT_CUES + _HOTEL_CUES:
        if out.lower().endswith(cue):
            out = out[:len(out) - len(cue)].strip()
    for word in _STOPWORDS_PLACE:
        if out.lower().startswith(word + ' '):
            out = out[len(word) + 1:].strip()
    return out.strip('的 ,，')


def _parse_passengers(text):
    adults, children = 1, 0
    m = re.search(r'(\d+)\s*(?:个)?\s*(?:成人|大人)', text)
    if m:
        adults = int(m.group(1))
    else:
        m = re.search(r'([一两二三四五六七八九十])\s*(?:个)?\s*(?:成人|大人)', text)
        if m:
            adults = _CN_NUM[m.group(1)]
        else:
            m = re.search(r'(\d+)\s*adults?', text, re.IGNORECASE)
            if m:
                adults = int(m.group(1))
    m = re.search(r'(\d+)\s*(?:个)?\s*(?:儿童|小孩|孩子)', text)
    if m:
        children = int(m.group(1))
    else:
        m = re.search(r'([一两二三四五六七八九十])\s*(?:个)?\s*(?:儿童|小孩|孩子)', text)
        if m:
            children = _CN_NUM[m.group(1)]
        else:
            m = re.search(r'(\d+)\s*(?:children|kids?|child)', text, re.IGNORECASE)
            if m:
                children = int(m.group(1))
    # "2大1小" shorthand.
    m = re.search(r'(\d+)\s*大\s*(\d+)\s*小', text)
    if m:
        adults, children = int(m.group(1)), int(m.group(2))
    return max(1, adults), max(0, children)


def _parse_cabin(text):
    low = text.lower()
    # Longest cue first so '超级经济舱' is not shadowed by '经济'.
    best, best_len = 'ECONOMY', 0
    for grade, cues in _CABINS.items():
        for cue in cues:
            if cue in low and len(cue) > best_len:
                best, best_len = grade, len(cue)
    return best


def _parse_nights(text):
    m = re.search(r'(?:住|连住|stay)\s*(\d+)\s*(?:晚|夜|nights?)', text, re.IGNORECASE)
    if m:
        return max(1, int(m.group(1)))
    m = re.search(r'(\d+)\s*(?:晚|夜)', text)
    if m:
        return max(1, int(m.group(1)))
    m = re.search(r'([一两二三四五六七八九十])\s*(?:晚|夜)', text)
    if m:
        return max(1, _CN_NUM[m.group(1)])
    m = re.search(r'(\d+)\s*nights?', text, re.IGNORECASE)
    if m:
        return max(1, int(m.group(1)))
    return 1


def _parse_stars(text):
    stars = []
    for m in re.finditer(r'([一二三四五12345])\s*星', text):
        token = m.group(1)
        stars.append(float(_CN_NUM.get(token, token if token.isdigit() else 0)
                           if not token.isdigit() else int(token)))
    for m in re.finditer(r'(\d)\s*-?\s*star', text, re.IGNORECASE):
        stars.append(float(m.group(1)))
    out = sorted({s for s in stars if 1.0 <= s <= 5.0})
    # "4 星以上" / "4+ stars" widens upward.
    if out and re.search(r'(?:以上|\+|or above|and up)', text):
        top = out[-1]
        out = [float(v) for v in range(int(top), 6)]
    return out


# Rating / comparator / occupancy phrases. These sit right next to the place
# name in a hotel query ('上海外滩 8/3 住2晚 五星酒店') and the place regexes
# match greedy CJK runs, so an unscrubbed '五星' is picked up as the city.
_QUALIFIER_PATTERNS = (
    r'[一二三四五六七八九十\d]\s*星级?',
    r'\d\s*-?\s*star s?',
    r'以上|以下|左右|起',
    r'\d+\s*大\s*\d+\s*小',
    r'[一二三四五六七八九十\d]+\s*(?:个)?\s*(?:成人|大人|儿童|小孩|孩子)',
    r'[一二三四五六七八九十\d]+\s*(?:晚|夜)',
    r'\b\d+\s*(?:nights?|adults?|children|kids?|child)\b',
    r'含早|可取消|高性价比|性价比',
)

# Residue that survives scrubbing but can never be a place.
_NON_PLACE = frozenset({
    '以上', '以下', '左右', '附近', '周边', '一带', '区域', '星级',
    '入住', '预订', '推荐', '帮我', '找个', '搜一下', '价格', '便宜',
})


def _scrub_qualifiers(text):
    """Remove rating / occupancy / comparator phrases from a hotel query."""
    out = text
    for pat in _QUALIFIER_PATTERNS:
        out = re.sub(pat, ' ', out, flags=re.IGNORECASE)
    return re.sub(r'\s{2,}', ' ', out).strip()


def _is_place_like(token):
    """Reject qualifier residue that the place regexes would otherwise return."""
    if not token or len(token) < 2:
        return False
    if token in _NON_PLACE:
        return False
    return not _has_any(token, _HOTEL_CUES + _FLIGHT_CUES)


def _parse_hotel_place(text):
    """Extract (place, place_type) for a hotel query, or (None, None)."""
    place_type = '城市'
    for ptype, cues in _PLACE_TYPE_CUES:
        if _has_any(text, cues):
            place_type = ptype
            break

    # Strip date / rating / occupancy noise before looking for the place.
    scrubbed = _scrub_qualifiers(_scrub_temporal(text))
    scrubbed = re.sub(r'[-~—到至]', ' ', scrubbed)

    ordered = (
        r'([\u4e00-\u9fff]{2,12}?)(?:附近|周边|一带)',
        r'([\u4e00-\u9fff]{2,12})\s*(?:的)?\s*(?:酒店|住宿|宾馆|民宿|客栈)',
    )
    for pat in ordered:
        m = re.search(pat, scrubbed)
        if m:
            cand = _clean_place(m.group(1))
            if _is_place_like(cand):
                return cand, place_type
    m = re.search(r'(?:hotels?|stay|accommodation)\s+(?:in|near|around)\s+'
                  r'([A-Za-z][A-Za-z\s]{1,25}?)(?=\s*(?:on|for|,|$))',
                  scrubbed, re.IGNORECASE)
    if m:
        return _clean_place(m.group(1)), place_type
    m = re.search(r'\bin\s+([A-Za-z][A-Za-z\s]{1,25}?)(?=\s*(?:on|for|,|$))',
                  scrubbed, re.IGNORECASE)
    if m:
        return _clean_place(m.group(1)), place_type
    for tok in re.findall(r'[\u4e00-\u9fff]{2,12}', scrubbed):
        cleaned = _clean_place(tok)
        if _is_place_like(cleaned):
            return cleaned, place_type
    return None, place_type


def parse_flight_query(q, *, today):
    """Parse a flight query into :class:`FlightSlots`, or None.

    None means "do not call upstream": either the route/date could not be
    resolved, or the date is in the past (which the provider rejects outright).
    """
    if not q or not q.strip():
        return None
    text = q.strip()
    origin, dest = _parse_route(_scrub_temporal(text))
    if not origin or not dest:
        return None

    notes = []
    start, end = _parse_date_range(text, today)
    if start is None:
        start, _ = _resolve_date(text, today)
        end = None
    if start is None:
        return None
    if start < today:
        return None

    round_trip = _has_any(text, _ROUND_TRIP_CUES) or end is not None
    trip_type = 'ROUND_TRIP' if round_trip else 'ONE_WAY'
    ret = ''
    if round_trip:
        if end is not None and end > start:
            ret = end.isoformat()
        else:
            # Never invent a return date: downgrade and say so, so the model can
            # re-ask instead of being handed a fabricated itinerary.
            trip_type = 'ONE_WAY'
            notes.append('往返日期缺失，已按单程查询（需要往返请提供返程日期）')

    adults, children = _parse_passengers(text)
    return FlightSlots(
        from_text=origin, to_text=dest, from_date=start.isoformat(),
        trip_type=trip_type, ret_date=ret, adults=adults, children=children,
        cabin=_parse_cabin(text), notes=notes,
    )


def parse_hotel_query(q, *, today):
    """Parse a hotel query into :class:`HotelSlots`, or None."""
    if not q or not q.strip():
        return None
    text = q.strip()
    place, place_type = _parse_hotel_place(text)
    if not place:
        return None

    notes = []
    start, end = _parse_date_range(text, today)
    if start is None:
        start, _ = _resolve_date(text, today)
    if start is None:
        return None
    if start < today:
        return None

    if end is not None and end > start:
        nights = (end - start).days
    else:
        nights = _parse_nights(text)

    return HotelSlots(
        place=place, check_in_date=start.isoformat(), place_type=place_type,
        stay_nights=nights, star_ratings=_parse_stars(text),
        origin_query=text[:120], notes=notes,
    )
