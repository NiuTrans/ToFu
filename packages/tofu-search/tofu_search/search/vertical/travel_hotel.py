"""Hotel vertical — provider chain: RollingGo (道旅) keyed, FlyAI (飞猪) anonymous.

RollingGo path (``ROLLINGGO_API_KEY`` configured): single-hop ``searchHotels``
lookup returning bookable inventory with live lowest-price confirmation.

FlyAI path (travel_flyai.py): zero-config fallback — and the only path when no
key is configured — so the hotel type is now available WITHOUT any credential.

``getHotelSearchTags`` / ``getHotelDetail`` are deliberately NOT called here —
they belong to the booking chain, which stays with the full MCP client. The
vertical layer only does search-time enrichment.
"""

from datetime import date

from tofu_search.config import get_config
from tofu_search.search.vertical import _mcp, travel_flyai, travel_slots
from tofu_search.search.vertical.base import logger

TYPE = 'hotel'
DOMAIN = 'travel'

META = {
    'purpose': 'Real bookable hotel inventory with live lowest-price lookup '
               '(2M+ properties, 110k+ direct-contract).',
    'when_to_use': 'A query naming a place plus a stay date (e.g. "上海外滩 8/3 '
                   '住2晚 五星酒店", "hotels in Kyoto on 2026-08-03").',
    'examples': ['上海外滩附近后天入住的五星酒店', '三亚 8/3-8/5 酒店',
                 'hotels in Kyoto on 2026-08-03 for 2 nights'],
    'requires_credential': False,
    'credential_env': 'ROLLINGGO_API_KEY',
}


def _today():
    """The current local date, isolated so tests can pin the calendar (see
    travel_flight._today)."""
    return date.today()


def is_available(cfg=None):
    """True while at least one provider can serve a hotel request.

    RollingGo needs ``ROLLINGGO_API_KEY``; FlyAI ships a bundled trial
    credential, so the type is available in a zero-config deployment unless
    FlyAI's credential has been rejected in this process.
    """
    cfg = cfg or get_config()
    if bool(cfg.rollinggo_api_key):
        return True
    return travel_flyai.is_available(cfg)


def detect(q):
    """Detect a hotel-intent query with resolvable slots."""
    if not is_available():
        return None
    if not travel_slots.looks_like_hotel(q):
        return None
    if travel_slots.parse_hotel_query(q, today=_today()) is None:
        return None
    return (TYPE, q, {})


def search(identifier, params):
    """Look up hotels for a natural-language query.

    Provider chain: RollingGo first when ``ROLLINGGO_API_KEY`` is configured
    (paid quota, live lowest-price confirmation), FlyAI as the anonymous
    fallback — and the only path when no key is configured.
    """
    cfg = get_config()
    if not is_available(cfg):
        return None
    slots = travel_slots.parse_hotel_query(identifier, today=_today())
    if slots is None:
        return None
    if cfg.rollinggo_api_key:
        out = _search_rollinggo(slots, cfg)
        if out is not None:
            return out
        logger.info('[Vertical] hotel: RollingGo path failed, falling back '
                    'to FlyAI')
    return travel_flyai.search_hotels(slots, cfg=cfg)


def _search_rollinggo(slots, cfg):
    """RollingGo hotel path (searchHotels)."""
    args = {
        'originQuery': slots.origin_query or slots.place,
        'place': slots.place,
        'placeType': slots.place_type,
        'checkInParam': {
            'checkInDate': slots.check_in_date,
            'stayNights': slots.stay_nights,
        },
        'size': 10,
    }
    if slots.star_ratings:
        args['filterOptions'] = {'starRatings': slots.star_ratings}

    try:
        data = _mcp.call_tool(cfg.rollinggo_hotel_endpoint, 'searchHotels', args,
                              api_key=cfg.rollinggo_api_key,
                              timeout=cfg.vertical_travel_timeout,
                              label='hotel/searchHotels')
        if data is _mcp.UNAUTHORIZED:
            logger.warning('[Vertical] hotel endpoint rejected ROLLINGGO_API_KEY')
            return None
        if not isinstance(data, dict):
            return None
        hotels = data.get('hotelInformationList') or []
        if not hotels:
            logger.info('[Vertical] hotel: no inventory for %s on %s',
                        slots.place, slots.check_in_date)
            return None

        parts = [f'## 酒店 {slots.place}  {slots.check_in_date} 起 '
                 f'{slots.stay_nights} 晚',
                 f'**共 {len(hotels)} 条结果**（价格为参考价，以实际下单为准）']
        for note in slots.notes:
            parts.append(f'> {note}')

        items = []
        for h in hotels[:10]:
            if not isinstance(h, dict):
                continue
            name = h.get('name') or ''
            star = h.get('starRating')
            price_obj = h.get('price') or {}
            lowest = price_obj.get('lowestPrice')
            currency = price_obj.get('currency') or 'CNY'
            url = h.get('bookingUrl') or ''
            price_txt = (f'{lowest:,.0f} {currency}'
                         if isinstance(lowest, (int, float)) else '暂无报价')
            head = f'\n### {name}'
            if isinstance(star, (int, float)) and star:
                head += f'  {"⭐" * int(star)}'
            parts.append(head)
            parts.append(f'**最低价**: {price_txt}')
            if h.get('address'):
                parts.append(f'**地址**: {h["address"]}')
            amenities = h.get('hotelAmenities') or []
            if amenities:
                parts.append(f'**设施**: {"、".join(str(a) for a in amenities[:8])}')
            if url:
                parts.append(f'**预订**: {url}')
            items.append({
                'title': name,
                'snippet': price_txt,
                'url': url,
                'type': TYPE,
                'bookable': bool(url),
            })

        return {'domain': DOMAIN, 'type': TYPE,
                'identifier': f'{slots.place}@{slots.check_in_date}',
                'content': '\n'.join(parts), 'items': items,
                'source': 'RollingGo 酒店'}
    except Exception as e:
        logger.warning('[Vertical] RollingGo hotel lookup failed for %r: %s',
                       slots.place, e)
        return None
