"""Flight vertical — provider chain: RollingGo (道旅) keyed, FlyAI (飞猪) anonymous.

RollingGo path (``ROLLINGGO_API_KEY`` configured): two-hop lookup —
``searchAirports`` resolves free-text city names to IATA codes, then
``searchFlights`` returns the priced itinerary list. Its anonymous era ended
2026-08-04 (the endpoint now answers 401 to keyless calls), so it is only
attempted when a key is configured.

FlyAI path (travel_flyai.py): zero-config fallback — and the only path when no
key is configured — via the bundled trial credential of the public flyai-cli.

Both share one wall-clock budget so a slow upstream cannot blow the caller's
search deadline.
"""

import re
from datetime import date

from tofu_search.config import get_config
from tofu_search.search.vertical import _mcp, travel_flyai, travel_slots
from tofu_search.search.vertical.base import logger

TYPE = 'flight'
DOMAIN = 'travel'

META = {
    'purpose': 'Real bookable flight inventory — airport/city code lookup plus '
               'priced itineraries across 500+ airlines.',
    'when_to_use': 'A query naming an origin, a destination and a travel date '
                   '(e.g. "8月3日北京到上海的机票", "flights from Hangzhou to '
                   'Chengdu next Tuesday").',
    'examples': ['8月3日北京到上海的机票', '下周二杭州飞成都 2大1小 商务舱',
                 'round trip flights from Shanghai to Tokyo 8/3-8/8'],
    'requires_credential': False,
    'credential_env': 'ROLLINGGO_API_KEY',
}

_CABIN_LABEL = {'ECONOMY': '经济舱', 'PREMIUM_ECONOMY': '超级经济舱',
                'BUSINESS': '商务舱', 'FIRST': '头等舱'}


def _today():
    """The current local date, isolated so tests can pin the calendar.

    travel_slots is deliberately clock-free (``today`` is injected); the
    handler is the caller that reads the clock. Keeping that read behind a
    one-line function is what stops handler-level tests rotting as the
    calendar moves past their literal query dates.
    """
    return date.today()


def is_available(cfg=None):
    """True while at least one provider can serve a flight request.

    RollingGo only serves callers holding ``ROLLINGGO_API_KEY``; FlyAI ships a
    bundled trial credential and is the keyless path, so the type is available
    in a zero-config deployment unless FlyAI's credential has been rejected.
    """
    cfg = cfg or get_config()
    if bool(cfg.rollinggo_api_key):
        return True
    return travel_flyai.is_available(cfg)


def detect(q):
    """Detect a flight-intent query with resolvable slots."""
    if not is_available():
        return None
    if not travel_slots.looks_like_flight(q):
        return None
    if travel_slots.parse_flight_query(q, today=_today()) is None:
        return None
    return (TYPE, q, {})


def _call(tool, arguments, *, cfg, timeout):
    out = _mcp.call_tool(cfg.rollinggo_flight_endpoint, tool, arguments,
                         api_key=cfg.rollinggo_api_key, timeout=timeout,
                         label=f'flight/{tool}')
    if out is _mcp.UNAUTHORIZED:
        logger.warning('[Vertical] flight endpoint rejected ROLLINGGO_API_KEY')
        return None
    return out


def _normalize_place(text):
    """Lowercase and strip separators so 'Hong Kong' matches 'hongkong'."""
    return re.sub(r'[\s\-_.,/()]+', '', (text or '').lower())


def _candidate_matches(cand, place):
    """True when an ``airPortInformationList`` entry really is ``place``.

    ``searchAirports`` is a FUZZY supplier-side search: asking for 上海 can come
    back with a list whose first entry is a different city's transport node. The
    provider documents this explicitly and tells clients to re-filter on
    cityCode / airportCode / countryCode. Skipping that check does not merely
    mislabel the result — an unnoticed wrong endpoint still returns bookable
    priced itineraries, i.e. a real quote for a route the user never asked for.
    """
    want = _normalize_place(place)
    if not want:
        return False
    # An explicit 3-letter IATA code in the query matches either code exactly.
    if len(want) == 3:
        for key in ('cityCode', 'airportCode'):
            if _normalize_place(cand.get(key)) == want:
                return True
    for key in ('cityName', 'airportName', 'cityCode', 'airportCode'):
        got = _normalize_place(cand.get(key))
        if not got:
            continue
        if got == want or want in got or got in want:
            return True
    return False


def _resolve_code(place, *, cfg, timeout):
    """Map free text to (city_code, airport_code, display_name), or None.

    Returns None rather than guessing when no candidate corroborates ``place``:
    a wrong code produces a plausible-looking quote for the wrong route, which
    neither the model nor the user can detect downstream.
    """
    data = _call('searchAirports', {'keyword': place}, cfg=cfg, timeout=timeout)
    if not isinstance(data, dict):
        return None
    airports = [a for a in (data.get('airPortInformationList') or [])
                if isinstance(a, dict)]
    if not airports:
        return None

    match = next((a for a in airports if _candidate_matches(a, place)), None)
    if match is None:
        logger.info('[Vertical] flight: %d airport candidate(s) for %r, none '
                    'corroborating it (first was %r) — refusing to guess',
                    len(airports), place,
                    airports[0].get('cityName') or airports[0].get('airportName'))
        return None
    return (match.get('cityCode') or '', match.get('airportCode') or '',
            match.get('cityName') or match.get('airportName') or place)


def _format_segments(segments):
    lines = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        dep = (seg.get('depTime') or '').replace('T', ' ')
        arr = (seg.get('arrTime') or '').replace('T', ' ')
        dur = seg.get('duration')
        piece = (f"  {seg.get('flightNumber', '')} "
                 f"{seg.get('depAirport', '')} {dep} → "
                 f"{seg.get('arrAirport', '')} {arr}")
        if dur:
            piece += f'  ({dur} min)'
        if seg.get('stopCities'):
            piece += f"  经停 {seg['stopCities']}"
        lines.append(piece)
    return lines


def search(identifier, params):
    """Look up flights for a natural-language query.

    Provider chain: RollingGo first when ``ROLLINGGO_API_KEY`` is configured
    (paid quota, richer arguments), FlyAI as the anonymous fallback — and the
    only path when no key is configured.
    """
    cfg = get_config()
    if not is_available(cfg):
        return None
    slots = travel_slots.parse_flight_query(identifier, today=_today())
    if slots is None:
        return None
    if cfg.rollinggo_api_key:
        out = _search_rollinggo(slots, cfg)
        if out is not None:
            return out
        logger.info('[Vertical] flight: RollingGo path failed, falling back '
                    'to FlyAI')
    return travel_flyai.search_flights(slots, cfg=cfg)


def _search_rollinggo(slots, cfg):
    """RollingGo flight path: two-hop (searchAirports → searchFlights)."""
    timeout = cfg.vertical_travel_timeout
    try:
        origin = _resolve_code(slots.from_text, cfg=cfg, timeout=timeout)
        dest = _resolve_code(slots.to_text, cfg=cfg, timeout=timeout)
        if not origin or not dest:
            logger.info('[Vertical] flight: could not resolve route %r → %r',
                        slots.from_text, slots.to_text)
            return None
        # Both endpoints resolving to one code means at least one lookup latched
        # onto the wrong candidate; querying it would price a nonexistent route.
        if (origin[0] or origin[1]) == (dest[0] or dest[1]):
            logger.info('[Vertical] flight: %r and %r both resolved to %s — '
                        'ambiguous route, refusing to query',
                        slots.from_text, slots.to_text, origin[0] or origin[1])
            return None

        args = {
            'adultNumber': slots.adults,
            'childNumber': slots.children,
            'cabinGrade': slots.cabin,
            'tripType': slots.trip_type,
            'fromDate': slots.from_date,
        }
        if slots.trip_type == 'ROUND_TRIP' and slots.ret_date:
            args['retDate'] = slots.ret_date
        # City codes cover every airport in a metro area; fall back to the
        # airport code only when the provider gave no city code.
        if origin[0]:
            args['fromCity'] = origin[0]
        else:
            args['fromAirport'] = origin[1]
        if dest[0]:
            args['toCity'] = dest[0]
        else:
            args['toAirport'] = dest[1]

        data = _call('searchFlights', args, cfg=cfg, timeout=timeout)
        if not isinstance(data, dict):
            return None
        flights = data.get('flightInformationList') or []
        if not flights:
            logger.info('[Vertical] flight: no itineraries for %s→%s on %s',
                        origin[2], dest[2], slots.from_date)
            return None

        trip_label = '往返' if slots.trip_type == 'ROUND_TRIP' else '单程'
        header = (f'## 航班 {origin[2]} → {dest[2]}  {slots.from_date}'
                  f'{" ~ " + slots.ret_date if slots.ret_date else ""}')
        parts = [header,
                 f'**行程**: {trip_label}  |  **舱位**: '
                 f'{_CABIN_LABEL.get(slots.cabin, slots.cabin)}  |  '
                 f'**乘客**: {slots.adults} 成人'
                 + (f' + {slots.children} 儿童' if slots.children else ''),
                 f'**共 {len(flights)} 条结果**（价格与库存实时变动，下单前请复核）',
                 '_仅支持查询，不可直接预订；需出票请自行前往航空公司或代理渠道。_']
        for note in slots.notes:
            parts.append(f'> {note}')

        items = []
        for fl in flights[:10]:
            if not isinstance(fl, dict):
                continue
            price = fl.get('totalAdultPrice')
            currency = fl.get('currency') or 'CNY'
            carrier = fl.get('validatingCarrier') or ''
            out_segs = fl.get('fromSegments') or []
            first_seg = out_segs[0] if out_segs and isinstance(out_segs[0], dict) else {}
            title = (f"{first_seg.get('flightNumber', carrier)} "
                     f"{first_seg.get('depAirport', '')}→{first_seg.get('arrAirport', '')}")
            price_txt = f'{price:,.0f} {currency}' if isinstance(price, (int, float)) else '—'
            parts.append(f'\n### {title}  {price_txt}')
            if carrier:
                parts.append(f'**承运**: {carrier}')
            parts.extend(_format_segments(out_segs))
            ret_segs = fl.get('retSegments') or []
            if ret_segs:
                parts.append('**返程**:')
                parts.extend(_format_segments(ret_segs))
            items.append({
                'title': title,
                'snippet': f'{price_txt} · {first_seg.get("depTime", "")}',
                'url': '',
                'type': TYPE,
                # The provider returns no booking link for flights (the endpoint
                # is query-only). Say so explicitly so a renderer can style the
                # row as non-clickable instead of emitting a dead link.
                'bookable': False,
            })

        return {'domain': DOMAIN, 'type': TYPE,
                'identifier': f'{origin[2]}→{dest[2]}@{slots.from_date}',
                'content': '\n'.join(parts), 'items': items,
                'source': 'RollingGo 机票'}
    except Exception as e:
        logger.warning('[Vertical] RollingGo flight lookup failed for %r→%r: %s',
                       slots.from_text, slots.to_text, e)
        return None
