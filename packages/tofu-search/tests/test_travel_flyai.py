"""FlyAI travel provider tests — signature, wire identity, parsing, latch.

Every test here is offline. The golden signature vector was produced by the
REFERENCE implementation (the bundled Node CLI's own crypto, run once at
authoring time), so the Python HMAC construction is checked against an
independent implementation, not against itself.
"""

import base64
import hashlib
import hmac
import json
from datetime import date

import pytest

from tofu_search.search.vertical import base, travel_flyai, travel_slots

TODAY = date(2026, 7, 28)


@pytest.fixture(autouse=True)
def _reset_flyai_latch():
    travel_flyai._reset_availability()
    yield
    travel_flyai._reset_availability()


# ── signature ──

@pytest.mark.unit
def test_sign_matches_the_node_cli_golden_vector():
    """Pinned against Node's crypto (digest('base64url'), padding stripped).

    Inputs fixed; expected value generated once by running the same payload
    construction through node:crypto — a cross-language oracle.
    """
    hdrs = travel_flyai._sign_headers(
        '{"jsonrpc":"2.0","id":1}', key='sk-test',
        secret='XSbdYnucPARDc9knhD8+X6hxdD1Nh6ZGI6Hadg25kBw=',
        pathname='/mcp', ts='1700000000000',
        nonce='0123456789abcdef0123456789abcdef')
    assert hdrs['x-flyai-sign'] == 'ENWunecdaBXD8Qkp4XkxxvfrCMJ87F-G9L_S-JFnu3E'


@pytest.mark.unit
def test_sign_headers_structure():
    hdrs = travel_flyai._sign_headers(
        '{}', key='sk-test', secret='s', pathname='/mcp',
        ts='1700000000000', nonce='ab' * 16)
    assert hdrs['Authorization'] == 'Bearer sk-test'
    assert hdrs['x-flyai-sign-ver'] == '7'
    assert hdrs['x-flyai-sign-alg'] == 'hmac-sha256'
    assert hdrs['x-flyai-ts'] == '1700000000000'
    assert hdrs['x-flyai-nonce'] == 'ab' * 16
    assert hdrs['x-ttid']
    sig = hdrs['x-flyai-sign']
    assert '=' not in sig and '+' not in sig and '/' not in sig


# ── wire identity: the signed bytes ARE the sent bytes ──

class _FakeResp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        envelope = {'jsonrpc': '2.0', 'id': 1,
                    'result': {'content': [{'type': 'text',
                                            'text': json.dumps(payload)}]}}
        self.text = json.dumps(envelope)
        self.headers = {'Content-Type': 'application/json'}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return json.loads(self.text)


@pytest.mark.unit
def test_signed_body_is_byte_identical_to_sent_body(monkeypatch):
    """The signature covers the exact body bytes; a json= re-serialisation
    (spaces, \\uXXXX escapes) would silently break it — capture what leaves
    the process and re-verify the signature against THOSE bytes."""
    captured = {}

    def fake_post(url, **kw):
        captured['data'] = kw.get('data')
        captured['headers'] = kw.get('headers')
        return _FakeResp({'data': {'itemList': []}})

    monkeypatch.setattr(base, 'http_post', fake_post)
    out = travel_flyai.call_tool('search_flight', {'origin': '北京'})
    assert out == {'data': {'itemList': []}}

    body = captured['data'].decode('utf-8')
    hdrs = captured['headers']
    payload = '\n'.join([
        'POST', '/mcp', hdrs['x-flyai-ts'], hdrs['x-flyai-nonce'],
        hashlib.sha256(body.encode('utf-8')).hexdigest(),
        hashlib.sha256(hdrs['Authorization'].encode('utf-8')).hexdigest(),
    ])
    expect = base64.urlsafe_b64encode(hmac.new(
        travel_flyai._SIGN_SECRET.encode('utf-8'), payload.encode('utf-8'),
        hashlib.sha256).digest()).decode('ascii').rstrip('=')
    assert hdrs['x-flyai-sign'] == expect
    # Compact raw-UTF-8 (the Node JSON.stringify shape the server verifies).
    assert '北京' in body
    assert '": "' not in body


@pytest.mark.unit
def test_trial_credential_used_when_unconfigured(monkeypatch):
    captured = {}

    def fake_post(url, **kw):
        captured['headers'] = kw.get('headers')
        return _FakeResp({'data': {'itemList': []}})

    monkeypatch.setattr(base, 'http_post', fake_post)
    travel_flyai.call_tool('search_flight', {'origin': '北京'})
    assert captured['headers']['Authorization'] == \
        'Bearer ' + travel_flyai._TRIAL_KEY


# ── availability latch ──

@pytest.mark.unit
def test_unauthorized_latches_provider_off(monkeypatch):
    monkeypatch.setattr(base, '_post_json',
                        lambda *a, **kw: base._FETCH_UNAUTHORIZED)
    assert travel_flyai.is_available() is True
    assert travel_flyai.call_tool('search_flight', {'origin': 'x'}) is None
    assert travel_flyai.is_available() is False


@pytest.mark.unit
def test_transport_failure_does_not_latch(monkeypatch):
    monkeypatch.setattr(base, '_post_json',
                        lambda *a, **kw: base._FETCH_FAILED)
    assert travel_flyai.call_tool('search_flight', {'origin': 'x'}) is None
    assert travel_flyai.is_available() is True


# ── flights ──

_FLYAI_FLIGHTS = {
    'data': {'itemList': [
        {'ticketPrice': '410.00',
         'jumpUrl': 'https://router.feizhu.com/x',
         'totalDuration': '115',
         'journeys': [
             {'journeyType': '直达', 'totalDuration': '115',
              'segments': [
                  {'marketingTransportNo': 'MU5231',
                   'marketingTransportName': '东航',
                   'depStationShortName': '大兴',
                   'depStationName': '大兴国际机场', 'depTerm': '',
                   'arrStationShortName': '浦东',
                   'arrStationName': '浦东国际机场', 'arrTerm': 'T1',
                   'depDateTime': '2026-08-06 23:20:00',
                   'arrDateTime': '2026-08-07 01:15:00',
                   'duration': '115', 'seatClassName': '经济舱'}]}]},
    ]},
    'message': 'success', 'status': 0, 'systemMessage': '飞猪提供数据支持'}


def _flight_slots(q="2026-08-03 北京到上海的机票"):
    s = travel_slots.parse_flight_query(q, today=TODAY)
    assert s is not None, q
    return s


@pytest.mark.unit
def test_search_flights_args_and_formatting(monkeypatch):
    seen = {}

    def fake_call(tool, args, **kw):
        seen['tool'] = tool
        seen['args'] = args
        return _FLYAI_FLIGHTS

    monkeypatch.setattr(travel_flyai, 'call_tool', fake_call)
    out = travel_flyai.search_flights(_flight_slots())
    assert out is not None
    assert seen['tool'] == 'search_flight'
    assert seen['args'] == {'origin': '北京', 'destination': '上海',
                            'depDate': '2026-08-03'}

    assert out['domain'] == 'travel' and out['type'] == 'flight'
    assert out['source'] == '飞猪机票 (FlyAI)'
    assert '## 航班 北京 → 上海  2026-08-03' in out['content']
    assert 'MU5231' in out['content']
    assert '¥410' in out['content']
    assert '[预订](https://router.feizhu.com/x)' in out['content']
    assert '飞猪提供数据支持' in out['content']
    item = out['items'][0]
    assert item['url'] == 'https://router.feizhu.com/x'
    assert item['bookable'] is True
    assert item['type'] == 'flight'


@pytest.mark.unit
def test_search_flights_round_trip_and_cabin_args(monkeypatch):
    seen = {}

    def fake_call(tool, args, **kw):
        seen['args'] = args
        return _FLYAI_FLIGHTS

    monkeypatch.setattr(travel_flyai, 'call_tool', fake_call)
    slots = _flight_slots("北京到上海 8/3-8/8 往返机票 商务舱")
    assert travel_flyai.search_flights(slots) is not None
    assert seen['args']['backDate'] == '2026-08-08'
    assert seen['args']['seatClassName'] == '商务舱'


@pytest.mark.unit
def test_search_flights_economy_adds_no_cabin_filter(monkeypatch):
    seen = {}

    def fake_call(tool, args, **kw):
        seen['args'] = args
        return _FLYAI_FLIGHTS

    monkeypatch.setattr(travel_flyai, 'call_tool', fake_call)
    assert travel_flyai.search_flights(_flight_slots()) is not None
    assert 'seatClassName' not in seen['args']


@pytest.mark.unit
def test_search_flights_empty_inventory_returns_none(monkeypatch):
    monkeypatch.setattr(travel_flyai, 'call_tool',
                        lambda *a, **kw: {'data': {'itemList': []}})
    assert travel_flyai.search_flights(_flight_slots()) is None


# ── hotels ──

_FLYAI_HOTELS = {
    'data': {'itemList': [
        {'name': '海友上海外滩延安东路酒店', 'star': '经济型', 'price': '¥2xx',
         'score': '5.0', 'scoreDesc': '超棒', 'address': '江西南路29号',
         'interestsPoi': '近南京路步行街',
         'detailUrl': 'https://router.feizhu.com/y'},
    ]},
    'message': 'success', 'status': 0}


@pytest.mark.unit
def test_search_hotels_args_and_formatting(monkeypatch):
    seen = {}

    def fake_call(tool, args, **kw):
        seen['tool'] = tool
        seen['args'] = args
        return _FLYAI_HOTELS

    monkeypatch.setattr(travel_flyai, 'call_tool', fake_call)
    slots = travel_slots.parse_hotel_query("上海外滩 8/3 住2晚 五星酒店",
                                           today=TODAY)
    out = travel_flyai.search_hotels(slots)
    assert out is not None
    assert seen['tool'] == 'search_hotels'
    assert seen['args'] == {'destName': '上海外滩',
                            'checkInDate': '2026-08-03',
                            'checkOutDate': '2026-08-05',
                            'limit': 10, 'hotelStars': '5'}

    assert out['type'] == 'hotel'
    assert out['source'] == '飞猪酒店 (FlyAI)'
    assert '海友上海外滩延安东路酒店' in out['content']
    assert '经济型' in out['content']
    assert '5.0 超棒' in out['content']
    assert '江西南路29号 · 近南京路步行街' in out['content']
    assert 'https://router.feizhu.com/y' in out['content']
    item = out['items'][0]
    assert item['bookable'] is True
    assert item['url'] == 'https://router.feizhu.com/y'


@pytest.mark.unit
def test_search_hotels_masked_price_is_disclosed(monkeypatch):
    """The trial credential gets masked prices ('¥2xx') — say so honestly."""
    monkeypatch.setattr(travel_flyai, 'call_tool',
                        lambda *a, **kw: _FLYAI_HOTELS)
    slots = travel_slots.parse_hotel_query("三亚 8/3-8/5 酒店", today=TODAY)
    out = travel_flyai.search_hotels(slots)
    assert out is not None
    assert '脱敏' in out['content']
    assert 'FLYAI_API_KEY' in out['content']


@pytest.mark.unit
def test_search_hotels_real_price_has_no_mask_note(monkeypatch):
    payload = {'data': {'itemList': [
        dict(_FLYAI_HOTELS['data']['itemList'][0], price='¥618')]},
        'message': 'success', 'status': 0}
    monkeypatch.setattr(travel_flyai, 'call_tool', lambda *a, **kw: payload)
    slots = travel_slots.parse_hotel_query("三亚 8/3-8/5 酒店", today=TODAY)
    out = travel_flyai.search_hotels(slots)
    assert out is not None
    assert '脱敏' not in out['content']


@pytest.mark.unit
def test_search_hotels_no_stars_omits_filter(monkeypatch):
    seen = {}

    def fake_call(tool, args, **kw):
        seen['args'] = args
        return _FLYAI_HOTELS

    monkeypatch.setattr(travel_flyai, 'call_tool', fake_call)
    slots = travel_slots.parse_hotel_query("三亚 8/3-8/5 酒店", today=TODAY)
    assert travel_flyai.search_hotels(slots) is not None
    assert 'hotelStars' not in seen['args']
