"""Travel vertical tests — slot parsing, MCP transport, and availability gating.

Every test here is offline. The transport seam (``base._post_json``) is patched
in each test that exercises a handler, and the availability tests assert that
NO request is attempted at all when a type is gated off.
"""

import json
from datetime import date

import pytest

from tofu_search import configure
from tofu_search.search.vertical import (
    _mcp,
    available_types,
    base,
    describe_domains,
    detect_vertical_intent,
    list_domains,
    registry,
    travel_flight,
    travel_flyai,
    travel_hotel,
)
from tofu_search.search.vertical import travel_slots as slots

TODAY = date(2026, 7, 28)  # a Tuesday — pinned so relative-date tests can't rot


@pytest.fixture(autouse=True)
def _reset_flyai_latch():
    travel_flyai._reset_availability()
    yield
    travel_flyai._reset_availability()


@pytest.fixture(autouse=True)
def _pin_handler_clock(monkeypatch):
    """Pin the handlers' clock to TODAY so literal query dates in handler
    tests never rot into the past (search/detect inject ``today`` into the
    deliberately clock-free slot parsers)."""
    monkeypatch.setattr(travel_flight, '_today', lambda: TODAY)
    monkeypatch.setattr(travel_hotel, '_today', lambda: TODAY)


# ── slot parsing: flights ──

@pytest.mark.unit
@pytest.mark.parametrize("q,frm,to,fdate", [
    ("8月3日北京到上海的机票", "北京", "上海", "2026-08-03"),
    ("2026-08-03 北京到上海 机票", "北京", "上海", "2026-08-03"),
    ("明天杭州飞成都的航班", "杭州", "成都", "2026-07-29"),
    ("后天广州到深圳机票", "广州", "深圳", "2026-07-30"),
    ("下周二杭州飞成都机票", "杭州", "成都", "2026-08-04"),
])
def test_parse_flight_basic(q, frm, to, fdate):
    s = slots.parse_flight_query(q, today=TODAY)
    assert s is not None, q
    assert (s.from_text, s.to_text, s.from_date) == (frm, to, fdate)


@pytest.mark.unit
def test_parse_flight_english_route():
    s = slots.parse_flight_query("flights from Hangzhou to Chengdu on 2026-08-03",
                                 today=TODAY)
    assert s is not None
    assert s.from_text == "Hangzhou"
    assert s.to_text == "Chengdu"
    assert s.from_date == "2026-08-03"


@pytest.mark.unit
def test_parse_flight_passengers_and_cabin():
    s = slots.parse_flight_query("8月3日北京到上海 2大1小 商务舱", today=TODAY)
    assert (s.adults, s.children, s.cabin) == (2, 1, "BUSINESS")


@pytest.mark.unit
def test_premium_economy_not_shadowed_by_economy():
    s = slots.parse_flight_query("8月3日北京到上海 豪华经济舱机票", today=TODAY)
    assert s.cabin == "PREMIUM_ECONOMY"


@pytest.mark.unit
def test_round_trip_with_explicit_range():
    s = slots.parse_flight_query("北京到上海 8/3-8/8 往返机票", today=TODAY)
    assert s.trip_type == "ROUND_TRIP"
    assert s.from_date == "2026-08-03"
    assert s.ret_date == "2026-08-08"


@pytest.mark.unit
def test_round_trip_without_return_date_downgrades_and_notes():
    """A missing return date must NOT be invented — downgrade and say so."""
    s = slots.parse_flight_query("8月3日北京到上海往返机票", today=TODAY)
    assert s.trip_type == "ONE_WAY"
    assert s.ret_date == ""
    assert s.notes and "往返" in s.notes[0]


@pytest.mark.unit
def test_past_date_rejected_outright():
    assert slots.parse_flight_query("2026-07-01 北京到上海机票", today=TODAY) is None


@pytest.mark.unit
def test_bare_month_day_rolls_to_next_year_when_past():
    s = slots.parse_flight_query("1月5日北京到上海机票", today=TODAY)
    assert s.from_date == "2027-01-05"


@pytest.mark.unit
@pytest.mark.parametrize("q", [
    "北京到上海的机票",          # no date
    "8月3日的机票",              # no route
    "how do I cook pasta",       # not travel at all
    "",
])
def test_flight_parse_returns_none_without_minimum_slots(q):
    assert slots.parse_flight_query(q, today=TODAY) is None


# ── slot parsing: hotels ──

@pytest.mark.unit
def test_parse_hotel_range_gives_nights():
    s = slots.parse_hotel_query("三亚 8/3-8/5 酒店", today=TODAY)
    assert s is not None
    assert s.place == "三亚"
    assert s.check_in_date == "2026-08-03"
    assert s.stay_nights == 2


@pytest.mark.unit
def test_parse_hotel_relative_date_and_stars():
    s = slots.parse_hotel_query("上海外滩附近后天入住的五星酒店", today=TODAY)
    assert s is not None
    assert s.place == "上海外滩"
    assert s.place_type == "景点"
    assert s.check_in_date == "2026-07-30"
    assert s.star_ratings == [5.0]


@pytest.mark.unit
def test_parse_hotel_nights_phrase():
    s = slots.parse_hotel_query("杭州西湖 8月3日 住3晚 酒店", today=TODAY)
    assert s.stay_nights == 3


@pytest.mark.unit
def test_hotel_star_above_widens_upward():
    s = slots.parse_hotel_query("杭州西湖 8月3日 4星以上酒店", today=TODAY)
    assert s.star_ratings == [4.0, 5.0]


@pytest.mark.unit
@pytest.mark.parametrize("q,place", [
    ("上海外滩 8/3 住2晚 五星酒店", "上海外滩"),
    ("上海外滩附近后天入住的五星酒店", "上海外滩"),
    ("三亚 8/3-8/5 酒店", "三亚"),
    ("杭州西湖 8月3日 住3晚 酒店", "杭州西湖"),
    ("杭州西湖 8月3日 4星以上酒店", "杭州西湖"),
    ("北京 8月3日 2大1小 住2晚 四星酒店", "北京"),
])
def test_qualifiers_never_leak_into_the_place_slot(q, place):
    """Star/occupancy/comparator words sit next to the place and were being
    picked up as the city ('五星', '以上'), which searches the wrong location."""
    s = slots.parse_hotel_query(q, today=TODAY)
    assert s is not None, q
    assert s.place == place


@pytest.mark.unit
def test_hotel_past_date_rejected():
    assert slots.parse_hotel_query("三亚 2026-07-01 酒店", today=TODAY) is None


@pytest.mark.unit
@pytest.mark.parametrize("q,is_flight,is_hotel", [
    ("8月3日北京到上海的机票", True, False),
    ("三亚 8/3-8/5 酒店", False, True),
    ("北京到上海机票 顺便订酒店", True, True),
    ("python asyncio tutorial", False, False),
])
def test_intent_cues(q, is_flight, is_hotel):
    assert slots.looks_like_flight(q) is is_flight
    assert slots.looks_like_hotel(q) is is_hotel


# ── MCP transport: SSE framing + double unwrap ──

class FakeResp:
    def __init__(self, status_code=200, text='', ctype='application/json',
                 headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = {'Content-Type': ctype}
        if headers:
            self.headers.update(headers)

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return json.loads(self.text)


def _envelope(payload):
    return {'jsonrpc': '2.0', 'id': 1,
            'result': {'content': [{'type': 'text', 'text': json.dumps(payload)}]}}


@pytest.mark.unit
def test_parse_sse_frames_takes_last_data_frame():
    body = ('event: message\ndata: {"a": 1}\n\n'
            'event: message\ndata: {"b": 2}\n\n')
    assert base._parse_sse_frames(body) == {"b": 2}


@pytest.mark.unit
def test_parse_sse_frames_joins_multiline_data():
    body = 'event: message\ndata: {"a":\ndata:  1}\n\n'
    assert base._parse_sse_frames(body) == {"a": 1}


@pytest.mark.unit
def test_post_json_decodes_sse_body(monkeypatch):
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
    monkeypatch.setattr(base, 'http_post',
                        lambda url, **kw: FakeResp(200, body, 'text/event-stream'))
    out = base._post_json('https://x/mcp', payload={})
    assert out['result'] == {'ok': True}


@pytest.mark.unit
def test_post_json_returns_unauthorized_sentinel(monkeypatch):
    monkeypatch.setattr(base, 'http_post', lambda url, **kw: FakeResp(401, ''))
    assert base._post_json('https://x/mcp', payload={}) is base._FETCH_UNAUTHORIZED


@pytest.mark.unit
def test_unwrap_tool_result_double_decodes():
    out = _mcp.unwrap_tool_result(_envelope({'message': 'ok', 'rows': [1, 2]}))
    assert out == {'message': 'ok', 'rows': [1, 2]}


@pytest.mark.unit
def test_unwrap_tool_result_handles_error_envelope():
    assert _mcp.unwrap_tool_result({'jsonrpc': '2.0', 'error': {'code': -32001}}) is None


@pytest.mark.unit
def test_call_tool_sends_accept_and_bearer(monkeypatch):
    seen = {}

    def fake_post(url, *, payload, headers=None, timeout=None, label='', **kw):
        seen['url'] = url
        seen['headers'] = headers
        seen['payload'] = payload
        return _envelope({'ok': True})

    monkeypatch.setattr(base, '_post_json', fake_post)
    out = _mcp.call_tool('https://x/mcp/flight', 'searchAirports',
                         {'keyword': '杭州'}, api_key='mcp_test')
    assert out == {'ok': True}
    assert seen['headers']['Accept'] == 'application/json, text/event-stream'
    assert seen['headers']['Authorization'] == 'Bearer mcp_test'
    assert seen['payload']['method'] == 'tools/call'
    assert seen['payload']['params']['name'] == 'searchAirports'
    assert seen['headers']['MCP-Protocol-Version'] == '2026-07-28'
    assert seen['headers']['Mcp-Method'] == 'tools/call'
    assert seen['headers']['Mcp-Name'] == 'searchAirports'
    meta = seen['payload']['params']['_meta']
    assert meta['io.modelcontextprotocol/protocolVersion'] == '2026-07-28'
    assert meta['io.modelcontextprotocol/clientCapabilities'] == {}


@pytest.mark.unit
def test_call_tool_omits_bearer_when_no_key(monkeypatch):
    seen = {}

    def fake_post(url, *, payload, headers=None, **kw):
        seen['headers'] = headers
        return _envelope({'ok': True})

    monkeypatch.setattr(base, '_post_json', fake_post)
    _mcp.call_tool('https://x/mcp/flight', 'searchAirports', {'keyword': 'x'})
    assert 'Authorization' not in seen['headers']


@pytest.mark.unit
def test_call_tool_falls_back_to_legacy_session(monkeypatch):
    calls = []

    def fake_post(url, *, payload, headers=None, return_response=False, **kw):
        calls.append((payload, headers, return_response))
        if payload['method'] == 'tools/call' and len(calls) == 1:
            return {'jsonrpc': '2.0', 'id': 1,
                    'error': {'code': -32000, 'message': 'not initialized'}}
        if payload['method'] == 'initialize':
            class Resp:
                headers = {'Mcp-Session-Id': 'legacy-session'}
            return Resp()
        if payload['method'] == 'notifications/initialized':
            return object()
        return _envelope({'ok': True})

    monkeypatch.setattr(base, '_post_json', fake_post)
    out = _mcp.call_tool('https://x/mcp/flight', 'searchAirports', {'keyword': 'x'})

    assert out == {'ok': True}
    assert [c[0]['method'] for c in calls] == [
        'tools/call', 'initialize', 'notifications/initialized', 'tools/call']
    assert calls[1][0]['params']['protocolVersion'] == '2025-11-25'
    assert calls[2][1]['Mcp-Session-Id'] == 'legacy-session'
    assert calls[3][1]['Mcp-Session-Id'] == 'legacy-session'
    assert 'Mcp-Method' not in calls[3][1]
    assert '_meta' not in calls[3][0]['params']


@pytest.mark.unit
def test_call_tool_falls_back_after_legacy_http_rejection(monkeypatch):
    calls = []

    def fake_post(url, *, payload, headers=None, return_response=False,
                  return_error_response=False, **kw):
        calls.append(payload['method'])
        if len(calls) == 1:
            assert return_error_response is True
            return FakeResp(400, 'missing session')
        if payload['method'] == 'initialize':
            return FakeResp(200, '', headers={'Mcp-Session-Id': 's1'})
        if payload['method'] == 'notifications/initialized':
            return object()
        return _envelope({'ok': True})

    monkeypatch.setattr(base, '_post_json', fake_post)
    out = _mcp.call_tool('https://x/mcp', 'searchAirports', {'keyword': 'x'})
    assert out == {'ok': True}
    assert calls == [
        'tools/call', 'initialize', 'notifications/initialized', 'tools/call']


# ── handlers ──

_AIRPORTS = {'airPortInformationList': [
    {'airportCode': 'HGH', 'cityCode': 'HGH', 'cityName': '杭州'},
]}
_AIRPORTS_CTU = {'airPortInformationList': [
    {'airportCode': 'TFU', 'cityCode': 'CTU', 'cityName': '成都'},
]}
_AIRPORTS_NOISY = {'airPortInformationList': [
    # A fuzzy supplier match whose FIRST entry is a different city entirely.
    {'airportCode': 'PVG', 'cityCode': 'SHA', 'cityName': '上海'},
    {'airportCode': 'PEK', 'cityCode': 'BJS', 'cityName': '北京'},
]}
_FLIGHTS = {'flightInformationList': [
    {'routingId': 'r1', 'totalAdultPrice': 1813.0, 'currency': 'CNY',
     'validatingCarrier': '3U',
     'fromSegments': [{'flightNumber': '3U8916', 'depTime': '2026-08-03T17:50:00',
                       'arrTime': '2026-08-03T21:00:00', 'depAirport': 'HGH',
                       'arrAirport': 'CTU', 'duration': '190', 'stopCities': ''}],
     'retSegments': []},
]}


def _flight_transport(calls):
    def fake_post(url, *, payload, headers=None, **kw):
        name = payload['params']['name']
        args = payload['params']['arguments']
        calls.append((name, args))
        if name == 'searchAirports':
            return _envelope(_AIRPORTS if args['keyword'] == '杭州' else _AIRPORTS_CTU)
        return _envelope(_FLIGHTS)
    return fake_post


@pytest.mark.unit
def test_flight_search_two_hop_builds_correct_args(monkeypatch):
    configure(rollinggo_api_key='mcp_test')
    calls = []
    monkeypatch.setattr(base, '_post_json', _flight_transport(calls))
    out = travel_flight.search("2026-08-03 杭州飞成都机票", {})
    assert out is not None
    assert out['type'] == 'flight'
    assert out['source'] == 'RollingGo 机票'
    assert '3U8916' in out['content']
    assert out['items'][0]['title'].startswith('3U8916')

    names = [c[0] for c in calls]
    assert names == ['searchAirports', 'searchAirports', 'searchFlights']
    flight_args = calls[-1][1]
    assert flight_args['fromCity'] == 'HGH'
    assert flight_args['toCity'] == 'CTU'
    assert flight_args['fromDate'] == '2026-08-03'
    assert flight_args['tripType'] == 'ONE_WAY'
    assert flight_args['cabinGrade'] == 'ECONOMY'
    assert flight_args['adultNumber'] == 1
    assert 'retDate' not in flight_args


@pytest.mark.unit
def test_flight_search_past_date_makes_no_request(monkeypatch):
    calls = []

    def boom(*a, **kw):
        calls.append(1)
        raise AssertionError('must not hit the network for a past date')

    monkeypatch.setattr(base, '_post_json', boom)
    assert travel_flight.search("2020-01-01 杭州飞成都机票", {}) is None
    assert calls == []


@pytest.mark.unit
def test_flyai_401_latches_credential_rejection(monkeypatch):
    """A 401 from FlyAI latches the anonymous provider off for the process,
    and with no RollingGo key the whole travel domain goes dark with it."""
    configure(rollinggo_api_key='')
    monkeypatch.setattr(base, '_post_json',
                        lambda *a, **kw: base._FETCH_UNAUTHORIZED)
    assert travel_flight.is_available() is True
    assert travel_flight.search("2026-08-03 杭州飞成都机票", {}) is None
    assert travel_flyai.is_available() is False
    assert travel_flight.is_available() is False
    assert 'travel' not in list_domains()


@pytest.mark.unit
def test_flight_items_declare_non_bookable_instead_of_dead_link(monkeypatch):
    """The provider returns no booking link for flights — say so explicitly."""
    configure(rollinggo_api_key='mcp_test')
    monkeypatch.setattr(base, '_post_json', _flight_transport([]))
    out = travel_flight.search("2026-08-03 杭州飞成都机票", {})
    item = out['items'][0]
    assert item['url'] == ''
    assert item['bookable'] is False
    assert '仅支持查询' in out['content']


# ── airport resolution must not blindly take the first candidate ──

@pytest.mark.unit
def test_resolve_picks_the_candidate_matching_the_query(monkeypatch):
    """searchAirports is fuzzy: entry[0] may be a DIFFERENT city.

    Taking it blindly yields a real priced quote for a route the user never
    asked for — undetectable downstream, hence the corroboration check.
    """
    configure(rollinggo_api_key='mcp_test')
    calls = []

    def fake_post(url, *, payload, headers=None, **kw):
        name = payload['params']['name']
        args = payload['params']['arguments']
        calls.append((name, args))
        if name == 'searchAirports':
            return _envelope(_AIRPORTS_NOISY)
        return _envelope(_FLIGHTS)

    monkeypatch.setattr(base, '_post_json', fake_post)
    out = travel_flight.search("2026-08-03 北京到上海的机票", {})
    assert out is not None
    flight_args = calls[-1][1]
    # 北京 is entry[1], 上海 is entry[0] — each must resolve to its OWN city.
    assert flight_args['fromCity'] == 'BJS'
    assert flight_args['toCity'] == 'SHA'
    assert out['identifier'].startswith('北京→上海')


@pytest.mark.unit
def test_unmatched_place_refuses_to_guess(monkeypatch):
    """No candidate corroborates the query → fail, do NOT price a guess."""
    configure(rollinggo_api_key='mcp_test')
    calls = []

    def fake_post(url, *, payload, headers=None, **kw):
        calls.append(payload['params']['name'])
        return _envelope({'airPortInformationList': [
            {'airportCode': 'LHR', 'cityCode': 'LON', 'cityName': 'London'},
        ]})

    monkeypatch.setattr(base, '_post_json', fake_post)
    assert travel_flight.search("2026-08-03 杭州飞成都机票", {}) is None
    assert 'searchFlights' not in calls


@pytest.mark.unit
def test_iata_code_in_query_matches_exactly(monkeypatch):
    configure(rollinggo_api_key='mcp_test')

    def fake_post(url, *, payload, headers=None, **kw):
        if payload['params']['name'] == 'searchAirports':
            return _envelope(_AIRPORTS_NOISY)
        return _envelope(_FLIGHTS)

    monkeypatch.setattr(base, '_post_json', fake_post)
    out = travel_flight.search("BJS to SHA on 2026-08-03 flight", {})
    assert out is not None


@pytest.mark.unit
def test_both_endpoints_same_code_is_rejected(monkeypatch):
    """A degenerate route means a lookup latched onto the wrong candidate."""
    configure(rollinggo_api_key='mcp_test')
    calls = []

    def fake_post(url, *, payload, headers=None, **kw):
        name = payload['params']['name']
        calls.append(name)
        if name == 'searchAirports':
            # Matches BOTH '上海' and '浦东' (substring), collapsing the route.
            return _envelope({'airPortInformationList': [
                {'airportCode': 'PVG', 'cityCode': 'SHA',
                 'cityName': '上海', 'airportName': '上海浦东'},
            ]})
        return _envelope(_FLIGHTS)

    monkeypatch.setattr(base, '_post_json', fake_post)
    assert travel_flight.search("2026-08-03 上海到浦东机票", {}) is None
    assert 'searchFlights' not in calls


# ── full stack: raw SSE bytes → handler output ──

@pytest.mark.unit
def test_full_stack_sse_transport_to_handler_content(monkeypatch):
    """Patch at the http_post layer so EVERY hop runs for real.

    The SSE decode and the JSON-RPC double-unwrap are unit-tested separately;
    this covers the seam between them, which is what actually breaks against a
    live ``streamable-http`` server.
    """
    configure(rollinggo_api_key='mcp_test')

    def sse_body(payload):
        envelope = json.dumps(_envelope(payload), ensure_ascii=False)
        return f'event: message\ndata: {envelope}\n\n'

    def fake_http_post(url, *, json=None, headers=None, timeout=None, **kw):
        assert headers['Accept'] == 'application/json, text/event-stream'
        name = json['params']['name']
        if name == 'searchAirports':
            kw_word = json['params']['arguments']['keyword']
            body = sse_body(_AIRPORTS if kw_word == '杭州' else _AIRPORTS_CTU)
        else:
            body = sse_body(_FLIGHTS)
        return FakeResp(200, body, 'text/event-stream')

    monkeypatch.setattr(base, 'http_post', fake_http_post)
    out = travel_flight.search("2026-08-03 杭州飞成都机票", {})
    assert out is not None
    assert '3U8916' in out['content']
    assert out['items'][0]['title'].startswith('3U8916')
    assert out['identifier'] == '杭州→成都@2026-08-03'


@pytest.mark.unit
def test_hotel_search_builds_checkin_param(monkeypatch):
    configure(rollinggo_api_key='mcp_test')
    seen = {}

    def fake_post(url, *, payload, headers=None, **kw):
        seen['args'] = payload['params']['arguments']
        seen['name'] = payload['params']['name']
        return _envelope({'hotelInformationList': [
            {'hotelId': 1, 'name': '上海和平饭店', 'starRating': 5.0,
             'address': '南京东路20号', 'bookingUrl': 'https://rollinggo.cn/x',
             'price': {'lowestPrice': 4665.0, 'currency': 'CNY', 'hasPrice': True},
             'hotelAmenities': ['泳池', 'SPA']},
        ]})

    monkeypatch.setattr(base, '_post_json', fake_post)
    out = travel_hotel.search("上海外滩 8/3 住2晚 五星酒店", {})
    assert out is not None
    assert seen['name'] == 'searchHotels'
    assert seen['args']['checkInParam'] == {'checkInDate': '2026-08-03',
                                            'stayNights': 2}
    assert seen['args']['filterOptions'] == {'starRatings': [5.0]}
    assert seen['args']['placeType'] == '景点'
    assert '上海和平饭店' in out['content']
    assert out['items'][0]['url'] == 'https://rollinggo.cn/x'


# ── availability gating ──

@pytest.mark.unit
def test_hotel_available_without_key_via_flyai():
    """Keyless: the hotel type now rides the FlyAI anonymous provider."""
    configure(rollinggo_api_key='')
    assert travel_hotel.is_available() is True
    assert travel_hotel.detect("三亚 8/3-8/5 酒店") is not None


@pytest.mark.unit
def test_travel_domain_keyless_has_both_types():
    """Keyless: FlyAI covers both types, so the whole domain is advertised."""
    configure(rollinggo_api_key='')
    assert 'travel' in list_domains()
    assert available_types('travel') == ['flight', 'hotel']


@pytest.mark.unit
def test_travel_domain_full_with_key():
    configure(rollinggo_api_key='mcp_test')
    assert available_types('travel') == ['flight', 'hotel']


@pytest.mark.unit
def test_describe_domains_reports_full_keyless_availability():
    configure(rollinggo_api_key='')
    entry = next(d for d in describe_domains() if d['domain'] == 'travel')
    assert entry['types'] == ['flight', 'hotel']
    assert entry['available_types'] == ['flight', 'hotel']
    assert entry['requires_credential'] is False
    assert entry['unavailable_types'] == []
    assert entry['examples']


@pytest.mark.unit
def test_describe_domains_covers_every_listed_domain():
    described = {d['domain'] for d in describe_domains()}
    assert described == set(list_domains())


@pytest.mark.unit
def test_domain_fanout_routes_hotel_to_flyai_keyless(monkeypatch):
    """vertical='travel' on a hotel query with no key goes straight to FlyAI;
    the RollingGo transport must never be touched."""
    configure(rollinggo_api_key='')
    seen = {}

    def fake_call(tool, args, **kw):
        seen['tool'] = tool
        return {'data': {'itemList': []}}

    def boom(*a, **kw):
        raise AssertionError('RollingGo must not be called without a key')

    monkeypatch.setattr(travel_flyai, 'call_tool', fake_call)
    monkeypatch.setattr(base, '_post_json', boom)
    # Empty inventory → no record, but the FlyAI tool WAS the one invoked.
    assert registry.search_vertical_domain('travel', "三亚 8/3-8/5 酒店") is None
    assert seen['tool'] == 'search_hotels'


@pytest.mark.unit
# ── provider chain ──

@pytest.mark.unit
def test_flight_keyless_goes_straight_to_flyai(monkeypatch):
    """No RollingGo key: the RollingGo transport must not be touched."""
    configure(rollinggo_api_key='')
    sentinel = {'domain': 'travel', 'type': 'flight', 'content': 'x'}

    def boom(*a, **kw):
        raise AssertionError('RollingGo must not be called without a key')

    monkeypatch.setattr(base, '_post_json', boom)
    monkeypatch.setattr(travel_flyai, 'search_flights',
                        lambda slots, **kw: sentinel)
    assert travel_flight.search("2026-08-03 杭州飞成都机票", {}) is sentinel


@pytest.mark.unit
def test_flight_with_key_prefers_rollinggo(monkeypatch):
    configure(rollinggo_api_key='mcp_test')
    monkeypatch.setattr(base, '_post_json', _flight_transport([]))

    def boom(*a, **kw):
        raise AssertionError('FlyAI must not be called when RollingGo answers')

    monkeypatch.setattr(travel_flyai, 'search_flights', boom)
    out = travel_flight.search("2026-08-03 杭州飞成都机票", {})
    assert out is not None
    assert out['source'] == 'RollingGo 机票'


@pytest.mark.unit
def test_flight_falls_back_to_flyai_when_rollinggo_fails(monkeypatch):
    configure(rollinggo_api_key='mcp_test')
    monkeypatch.setattr(base, '_post_json', lambda *a, **kw: base._FETCH_FAILED)
    sentinel = {'domain': 'travel', 'type': 'flight', 'content': 'x'}
    monkeypatch.setattr(travel_flyai, 'search_flights',
                        lambda slots, **kw: sentinel)
    assert travel_flight.search("2026-08-03 杭州飞成都机票", {}) is sentinel


@pytest.mark.unit
def test_hotel_falls_back_to_flyai_when_rollinggo_fails(monkeypatch):
    configure(rollinggo_api_key='mcp_test')
    monkeypatch.setattr(base, '_post_json', lambda *a, **kw: base._FETCH_FAILED)
    sentinel = {'domain': 'travel', 'type': 'hotel', 'content': 'x'}
    monkeypatch.setattr(travel_flyai, 'search_hotels',
                        lambda slots, **kw: sentinel)
    assert travel_hotel.search("三亚 8/3-8/5 酒店", {}) is sentinel


def test_travel_planner_routes_by_intent():
    plans = registry._travel_subtypes_for('travel', "8月3日北京到上海的机票")
    assert [p[0] for p in plans] == ['flight']
    plans = registry._travel_subtypes_for('travel', "三亚 8/3-8/5 酒店")
    assert [p[0] for p in plans] == ['hotel']


# ── detection chain integration ──

@pytest.mark.unit
def test_detect_routes_natural_language_flight_query():
    configure(rollinggo_api_key='')
    out = detect_vertical_intent("2026-08-03 北京到上海的机票")
    assert out is not None
    assert out[0] == 'flight'


@pytest.mark.unit
def test_detect_does_not_shadow_identifier_verticals():
    """Travel sits late in the chain; strong identifiers still win."""
    configure(rollinggo_api_key='mcp_test')
    assert detect_vertical_intent("CVE-2021-44228")[0] == 'cve'
    assert detect_vertical_intent("2301.07041")[0] == 'arxiv'
    assert detect_vertical_intent("github:torvalds/linux")[0] == 'github'


@pytest.mark.unit
def test_detect_hotel_keyless():
    """Hotel intent detects with zero configuration (FlyAI anonymous path)."""
    configure(rollinggo_api_key='')
    out = detect_vertical_intent("三亚 8/3-8/5 酒店")
    assert out is not None and out[0] == 'hotel'
