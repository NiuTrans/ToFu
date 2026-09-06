"""FlyAI (飞猪) travel provider — the travel vertical's anonymous backend.

The public flyai-cli npm package (``@fly-ai/flyai-cli``) talks to a
streamable-http MCP endpoint at ``flyai.open.fliggy.com`` and ships a built-in
trial credential — that bundled key is what its advertised "trial without any
API keys" mode authenticates with. This module re-implements that exact wire
protocol on tofu-search's proxy-aware HTTP layer (no Node runtime, no
shell-out), giving the travel vertical a zero-config flight + hotel backend.
Hosts with a paid key set ``FLYAI_API_KEY`` (and ``FLYAI_SIGN_SECRET`` when the
signing secret rotates) to step off the shared trial quota.

Wire protocol (recovered from the bundled CLI and verified against the live
endpoint on 2026-08-04):

* ``POST {endpoint}`` with a JSON-RPC ``tools/call`` body, serialised COMPACT
  with raw UTF-8 (exactly what Node's ``JSON.stringify`` emits). The signature
  covers those precise bytes, so the body is serialised once and sent raw —
  a ``requests`` ``json=`` re-serialisation would produce different bytes and
  break the signature.
* ``Authorization: Bearer <key>`` plus signature headers::

    x-flyai-sign = base64url-NO-PADDING( HMAC-SHA256(secret,
        "POST\n" + pathname + "\n" + timestampMs + "\n" + nonce + "\n"
        + sha256hex(body) + "\n" + sha256hex(authorization)) )

  (Node's ``digest('base64url')`` strips the ``=`` padding — matching that is
  the difference between 200 and "Authorization verification failed".)
* Tool names are snake_case (``search_flight``, ``search_hotels``), NOT the
  CLI's dashed subcommand names — the dashed forms answer 401 "Tool not
  allowed".
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import date, timedelta
from urllib.parse import urlparse

from tofu_search.config import get_config
from tofu_search.search.vertical import _mcp, base
from tofu_search.search.vertical.base import logger

# The bundled trial credential, as shipped in the PUBLIC npm package
# @fly-ai/flyai-cli (dist/flyai-bundle.cjs). Empty config fields mean "use
# these" — see config.py.
_TRIAL_KEY = ''
_SIGN_SECRET = ''
_ENDPOINT = 'https://flyai.open.fliggy.com/mcp'

_SIGN_VER = '7'
_SIGN_ALG = 'hmac-sha256'
_TTID = 'ai2c(sk.clawhub)'

_CABIN_LABEL = {'ECONOMY': '经济舱', 'PREMIUM_ECONOMY': '超级经济舱',
                'BUSINESS': '商务舱', 'FIRST': '头等舱'}

# Flipped when the server rejects the credential in use (HTTP 401), so a
# deployment whose trial key has been rotated out stops re-hitting a wall it
# has already hit once. Mirrors the RollingGo latch pattern, one per provider.
_credential_rejected = False


def _reset_availability():
    """Test hook — clear the learned 'credential rejected' latch."""
    global _credential_rejected
    _credential_rejected = False


def is_available(cfg=None):
    """True while this provider can serve a request right now.

    The bundled trial credential makes FlyAI keyless-available; the only
    disqualifier is the server having already rejected that credential in
    this process.
    """
    return not _credential_rejected


def _credential(cfg):
    """(api_key, sign_secret) — configured value, else the bundled trial."""
    key = getattr(cfg, 'flyai_api_key', '') or _TRIAL_KEY
    secret = getattr(cfg, 'flyai_sign_secret', '') or _SIGN_SECRET
    return key, secret


def _sign_headers(body_text, *, key, secret, pathname, method='POST',
                  ts=None, nonce=None):
    """Build the FlyAI auth + signature headers for one request body.

    ``ts`` / ``nonce`` are injectable purely so tests can pin a golden
    signature vector; production callers leave them to the clock/RNG.
    """
    auth = 'Bearer ' + key
    ts = ts or str(int(time.time() * 1000))
    nonce = nonce or secrets.token_hex(16)
    payload = '\n'.join([
        method, pathname, ts, nonce,
        hashlib.sha256(body_text.encode('utf-8')).hexdigest(),
        hashlib.sha256(auth.encode('utf-8')).hexdigest(),
    ])
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode('utf-8'), payload.encode('utf-8'),
                 hashlib.sha256).digest()).decode('ascii').rstrip('=')
    return {'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
            'Authorization': auth,
            'x-flyai-sign-ver': _SIGN_VER,
            'x-flyai-sign-alg': _SIGN_ALG,
            'x-flyai-ts': ts,
            'x-flyai-nonce': nonce,
            'x-flyai-sign': sig,
            'x-ttid': _TTID}


def call_tool(tool, arguments, *, cfg=None, timeout=None, label=''):
    """Invoke ``tool`` on the FlyAI MCP endpoint; return the unwrapped payload.

    Returns the business dict, or None on any failure (including a rejected
    credential, which also latches :data:`_credential_rejected`).
    """
    global _credential_rejected
    cfg = cfg or get_config()
    timeout = timeout or cfg.vertical_travel_timeout
    endpoint = getattr(cfg, 'flyai_mcp_endpoint', '') or _ENDPOINT
    label = label or f'flyai/{tool}'

    rpc = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
           'params': {'name': tool, 'arguments': arguments}}
    body_text = json.dumps(rpc, separators=(',', ':'), ensure_ascii=False)
    key, secret = _credential(cfg)
    headers = _sign_headers(body_text, key=key, secret=secret,
                            pathname=urlparse(endpoint).path or '/')

    out = base._post_json(endpoint, raw_body=body_text.encode('utf-8'),
                          headers=headers, timeout=timeout, label=label)
    if out is base._FETCH_UNAUTHORIZED:
        _credential_rejected = True
        logger.warning('[Vertical] %s rejected the %s credential (HTTP 401) — '
                       'latching the FlyAI provider off for this process',
                       label, 'configured' if getattr(cfg, 'flyai_api_key', '')
                       else 'bundled trial')
        return None
    if out is base._FETCH_FAILED:
        return None
    return _mcp.unwrap_tool_result(out)


# ── flights ──

def _price_text(raw):
    """Normalise a price ('410.00', 410.0, …) to '¥410'; '' stays '—'."""
    if raw in (None, ''):
        return '—'
    try:
        return f'¥{float(raw):,.0f}'
    except (TypeError, ValueError):
        return str(raw)


def _station(seg, side):
    name = seg.get(f'{side}StationShortName') or seg.get(f'{side}StationName') or ''
    term = seg.get(f'{side}Term') or ''
    return name + term


def _fmt_dt(raw):
    """'2026-08-06 23:20:00' → '08-06 23:20' (year is in the header)."""
    parts = (raw or '').split(' ')
    if not parts[0]:
        return ''
    ymd = parts[0].split('-')
    hm = parts[1][:5] if len(parts) > 1 else ''
    return f'{ymd[1]}-{ymd[2]} {hm}'.strip() if len(ymd) == 3 else raw


def search_flights(slots, *, cfg=None):
    """Look up flights via FlyAI for parsed :class:`FlightSlots`."""
    cfg = cfg or get_config()
    if not is_available(cfg):
        return None

    args = {'origin': slots.from_text, 'destination': slots.to_text,
            'depDate': slots.from_date}
    if slots.trip_type == 'ROUND_TRIP' and slots.ret_date:
        args['backDate'] = slots.ret_date
    # ECONOMY is the upstream default; filtering on it would only narrow results.
    if slots.cabin and slots.cabin != 'ECONOMY':
        label = _CABIN_LABEL.get(slots.cabin)
        if label:
            args['seatClassName'] = label

    try:
        data = call_tool('search_flight', args, cfg=cfg)
        if not isinstance(data, dict):
            return None
        flights = (data.get('data') or {}).get('itemList') or []
        flights = [f for f in flights if isinstance(f, dict)]
        if not flights:
            logger.info('[Vertical] flyai flight: no itineraries for %r → %r '
                        'on %s', slots.from_text, slots.to_text, slots.from_date)
            return None

        trip_label = '往返' if slots.trip_type == 'ROUND_TRIP' else '单程'
        header = (f'## 航班 {slots.from_text} → {slots.to_text}  {slots.from_date}'
                  f'{" ~ " + slots.ret_date if slots.ret_date else ""}')
        parts = [header,
                 f'**行程**: {trip_label}  |  **舱位**: '
                 f'{_CABIN_LABEL.get(slots.cabin, slots.cabin)}',
                 f'**共 {len(flights)} 条结果**（价格为单人参考价，实时变动，下单前请复核）']
        for note in slots.notes:
            parts.append(f'> {note}')
        if data.get('systemMessage'):
            parts.append(f'> {data["systemMessage"]}')

        items = []
        for fl in flights[:10]:
            journeys = [j for j in (fl.get('journeys') or []) if isinstance(j, dict)]
            first_seg = {}
            for j in journeys:
                segs = [s for s in (j.get('segments') or []) if isinstance(s, dict)]
                if segs:
                    first_seg = segs[0]
                    break
            price_txt = _price_text(fl.get('ticketPrice'))
            book_url = fl.get('jumpUrl') or ''
            no = first_seg.get('marketingTransportNo', '')
            title = (f"{no} {_station(first_seg, 'dep')}→{_station(first_seg, 'arr')}"
                     ).strip()
            parts.append(f'\n### {title}  {price_txt}')
            for idx, journey in enumerate(journeys):
                segs = [s for s in (journey.get('segments') or [])
                        if isinstance(s, dict)]
                if len(journeys) > 1:
                    legs = ('去程', '返程')
                    parts.append(f'**{legs[idx] if idx < 2 else f"行程{idx + 1}"}**:')
                for seg in segs:
                    piece = (f"  {seg.get('marketingTransportNo', '')} "
                             f"{_station(seg, 'dep')} {_fmt_dt(seg.get('depDateTime'))} → "
                             f"{_station(seg, 'arr')} {_fmt_dt(seg.get('arrDateTime'))}")
                    dur = seg.get('duration')
                    if dur:
                        piece += f'  ({dur} 分钟)'
                    extras = [x for x in (seg.get('marketingTransportName'),
                                          seg.get('seatClassName'),
                                          journey.get('journeyType')) if x]
                    if extras:
                        piece += f'  · {" · ".join(extras)}'
                    parts.append(piece)
            if book_url:
                parts.append(f'  [预订]({book_url})')
            items.append({'title': title,
                          'snippet': f"{price_txt} · {_fmt_dt(first_seg.get('depDateTime'))}",
                          'url': book_url,
                          'type': 'flight',
                          'bookable': bool(book_url)})

        return {'domain': 'travel', 'type': 'flight',
                'identifier': f'{slots.from_text}→{slots.to_text}@{slots.from_date}',
                'content': '\n'.join(parts), 'items': items,
                'source': '飞猪机票 (FlyAI)'}
    except Exception as e:
        logger.warning('[Vertical] flyai flight lookup failed for %r→%r: %s',
                       slots.from_text, slots.to_text, e)
        return None


# ── hotels ──

def search_hotels(slots, *, cfg=None):
    """Look up hotels via FlyAI for parsed :class:`HotelSlots`."""
    cfg = cfg or get_config()
    if not is_available(cfg):
        return None

    check_in = date.fromisoformat(slots.check_in_date)
    check_out = (check_in + timedelta(days=slots.stay_nights)).isoformat()
    args = {'destName': slots.place,
            'checkInDate': slots.check_in_date,
            'checkOutDate': check_out,
            'limit': 10}
    if slots.star_ratings:
        args['hotelStars'] = ','.join(str(int(s)) for s in slots.star_ratings)

    try:
        data = call_tool('search_hotels', args, cfg=cfg)
        if not isinstance(data, dict):
            return None
        hotels = (data.get('data') or {}).get('itemList') or []
        hotels = [h for h in hotels if isinstance(h, dict)]
        if not hotels:
            logger.info('[Vertical] flyai hotel: no inventory for %r on %s',
                        slots.place, slots.check_in_date)
            return None

        parts = [f'## 酒店 {slots.place}  {slots.check_in_date} 起 '
                 f'{slots.stay_nights} 晚',
                 f'**共 {len(hotels)} 条结果**（价格实时变动，以实际下单为准）']
        for note in slots.notes:
            parts.append(f'> {note}')
        # The trial credential gets masked prices ('¥2xx') on some inventory —
        # say so honestly instead of presenting a partial number as a quote.
        if any('x' in str(h.get('price') or '') for h in hotels):
            parts.append('> 部分价格为脱敏参考价；配置 FLYAI_API_KEY 可获取实价')
        if data.get('systemMessage'):
            parts.append(f'> {data["systemMessage"]}')

        items = []
        for h in hotels[:10]:
            name = h.get('name') or ''
            price = str(h.get('price') or '') or '暂无报价'
            url = h.get('detailUrl') or ''
            head = f'\n### {name}'
            if h.get('star'):
                head += f'  {h["star"]}'
            parts.append(head)
            line = f'**价格**: {price}'
            if h.get('score'):
                line += f'  |  **评分**: {h["score"]} {h.get("scoreDesc") or ""}'.rstrip()
            parts.append(line)
            addr = h.get('address') or ''
            if h.get('interestsPoi'):
                addr += f' · {h["interestsPoi"]}' if addr else h['interestsPoi']
            if addr:
                parts.append(f'**地址**: {addr}')
            if url:
                parts.append(f'**预订**: {url}')
            items.append({'title': name,
                          'snippet': price,
                          'url': url,
                          'type': 'hotel',
                          'bookable': bool(url)})

        return {'domain': 'travel', 'type': 'hotel',
                'identifier': f'{slots.place}@{slots.check_in_date}',
                'content': '\n'.join(parts), 'items': items,
                'source': '飞猪酒店 (FlyAI)'}
    except Exception as e:
        logger.warning('[Vertical] flyai hotel lookup failed for %r: %s',
                       slots.place, e)
        return None
