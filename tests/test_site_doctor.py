"""tests/test_site_doctor.py — selector-drift autofix (Site Knowledge Layer P3).

Covers the three layers of lib/site_doctor.py + lib/site_knowledge.py:

  * store: pin/get/clear roundtrip, version monotonicity, empty-extractor
    refusal;
  * trigger guards: env kill switch, unknown site, per-site cooldown,
    fleet-wide single-flight — the doctor is cheap and rare by construction;
  * the doctor loop itself: rides lib.agent_loop.run_agent_loop (charter
    iron rule) with a SCRIPTED dispatch — inspect → try(fail) → try(pass)
    → pin lands a verified entry; pin without a matching verification is
    rejected; give_up writes nothing; the loop NEVER raises.

All offline: the LLM is a scripted dispatch_chat fake, the browser is a
scripted _scrape fake, the store lives in tmp_path.
"""

import json

import pytest

import lib.site_doctor as sd
import lib.site_knowledge as sk

pytestmark = pytest.mark.unit

SITE = 'xiaohongshu.com'
URL = 'https://www.xiaohongshu.com/search_result?keyword=x'
EVIDENCE = {'anchors': 7, 'page_title': 'x - 小红书搜索'}

GOOD_CARDS = [{'title': '笔记一', 'url': 'https://www.xiaohongshu.com/explore/a1',
               'snippet': 's'},
              {'title': '笔记二', 'url': 'https://www.xiaohongshu.com/explore/b2',
               'snippet': ''}]

INSPECT_SAMPLE = {'url': URL, 'title': 'x - 小红书搜索', 'auth_wall': False,
                  'counts': {'note_item': 0, 'explore_anchors': 7},
                  'anchor_samples': ['<a href="/explore/a1">…</a>'],
                  'text_head': '小红书'}

WS = 'div.new-card'
JS = '(() => Array.from(document.querySelectorAll("div.new-card")).map(c => ({title: c.innerText, url: c.querySelector("a").href, snippet: ""})))()'


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated store + clean trigger state + captured audit events."""
    monkeypatch.setattr(sk, '_STORE_PATH', str(tmp_path / 'site_knowledge.json'))
    monkeypatch.delenv('TOFU_SITE_DOCTOR', raising=False)
    monkeypatch.delenv('TOFU_SITE_DOCTOR_COOLDOWN_S', raising=False)
    sd._last_attempt.clear()
    if sd._flight_lock.locked():
        sd._flight_lock.release()
    audits = []
    monkeypatch.setattr(sd, 'audit_log',
                        lambda event, **kw: audits.append((event, kw)))
    yield audits
    sd._last_attempt.clear()
    if sd._flight_lock.locked():
        sd._flight_lock.release()


# ── Store ─────────────────────────────────────────────────

def test_store_absent_returns_none(env):
    assert sk.get_knowledge(SITE) is None


def test_store_pin_roundtrip(env):
    entry = sk.pin_knowledge(SITE, extractor_js=JS, wait_selector=WS,
                             evidence={'anchors': 7}, notes='dom changed')
    assert entry['version'] == 1
    got = sk.get_knowledge(SITE)
    assert got['extractor_js'] == JS
    assert got['wait_selector'] == WS
    assert got['verified_by'] == 'site-doctor'
    assert got['evidence']['anchors'] == 7
    assert got['verified_at'] > 0


def test_store_version_increments(env):
    sk.pin_knowledge(SITE, extractor_js=JS)
    second = sk.pin_knowledge(SITE, extractor_js=JS + ' /*v2*/')
    assert second['version'] == 2


def test_store_refuses_empty_extractor(env):
    with pytest.raises(ValueError):
        sk.pin_knowledge(SITE, extractor_js='   ')
    assert sk.get_knowledge(SITE) is None


def test_store_clear_falls_back(env):
    sk.pin_knowledge(SITE, extractor_js=JS)
    assert sk.clear_knowledge(SITE) is True
    assert sk.get_knowledge(SITE) is None
    assert sk.clear_knowledge(SITE) is False, 'second clear = nothing to remove'


# ── Trigger guards ────────────────────────────────────────

def _thread_recorder(monkeypatch):
    """Fake the doctor thread target: records spawns, releases the flight
    lock exactly as the real _run_doctor_safe's finally does."""
    calls = []

    def fake(site, url, evidence):
        calls.append(site)
        sd._flight_lock.release()

    monkeypatch.setattr(sd, '_run_doctor_safe', fake)
    return calls


def test_trigger_env_off_spawns_nothing(env, monkeypatch):
    monkeypatch.setenv('TOFU_SITE_DOCTOR', '0')
    calls = _thread_recorder(monkeypatch)
    sd.on_site_drift(SITE, URL, EVIDENCE)
    assert calls == []


def test_trigger_unknown_site_spawns_nothing(env, monkeypatch):
    calls = _thread_recorder(monkeypatch)
    sd.on_site_drift('unknown.example', URL, EVIDENCE)
    assert calls == []


def test_trigger_cooldown_blocks_second(env, monkeypatch):
    calls = _thread_recorder(monkeypatch)
    sd.on_site_drift(SITE, URL, EVIDENCE)
    sd.on_site_drift(SITE, URL, EVIDENCE)
    assert calls == [SITE], 'second drift inside the 3h cooldown must be skipped'


def test_trigger_single_flight(env, monkeypatch):
    calls = _thread_recorder(monkeypatch)
    sd._flight_lock.acquire()          # simulate a doctor already running
    sd.on_site_drift(SITE, URL, EVIDENCE)
    sd._flight_lock.release()
    assert calls == [], 'a second doctor must never run concurrently'


# ── The doctor loop (scripted LLM + scripted browser) ─────

def _script_llm(monkeypatch, rounds):
    """Install a dispatch_chat fake that plays back scripted rounds.

    Each round: (tool_name, args) → a tool call; None → a bare final answer
    (no tool calls) which ends the loop.
    """
    from lib.llm_dispatch import api as dispatch_api
    script = list(rounds)
    seen = []

    def fake_dispatch(messages, max_tokens=0, temperature=0.0, tools=None,
                      prefer_model=None, log_prefix=''):
        seen.append([m.get('role') for m in messages])
        step = script.pop(0) if script else None
        if step is None:
            return 'done', {'total_tokens': 50, '_tool_calls': []}
        name, args = step
        tc = {'id': f'tc{len(script)}',
              'function': {'name': name, 'arguments': json.dumps(args)}}
        return '', {'total_tokens': 100, '_tool_calls': [tc]}

    monkeypatch.setattr(dispatch_api, 'dispatch_chat', fake_dispatch)
    return seen


def test_doctor_happy_path_pins_verified_knowledge(env, monkeypatch):
    scrapes = iter([INSPECT_SAMPLE,          # inspect
                    [],                      # try #1: extracts nothing
                    GOOD_CARDS])             # try #2: verified
    monkeypatch.setattr(sd, '_scrape', lambda *a, **kw: next(scrapes))
    _script_llm(monkeypatch, [
        ('inspect_search_page', {}),
        ('try_extractor', {'wait_selector': 'section.note-item',
                           'extractor_js': '(() => [])()'}),
        ('try_extractor', {'wait_selector': WS, 'extractor_js': JS,
                           'scrolls': 2}),
        ('pin_knowledge', {'wait_selector': WS, 'extractor_js': JS,
                           'notes': 'cards moved to div.new-card'}),
    ])

    result = sd.run_doctor(SITE, URL, EVIDENCE)

    assert result['pinned'] is True
    got = sk.get_knowledge(SITE)
    assert got is not None, 'verified selectors must be pinned to the store'
    assert got['wait_selector'] == WS
    assert got['extractor_js'] == JS
    assert got['scrolls'] == 2
    assert got['evidence']['verified_count'] == 2
    events = [e for e, _ in env]
    assert 'site_knowledge_pinned' in events


def test_pin_without_verification_is_rejected(env, monkeypatch):
    monkeypatch.setattr(sd, '_scrape', lambda *a, **kw: INSPECT_SAMPLE)
    _script_llm(monkeypatch, [
        ('inspect_search_page', {}),
        ('pin_knowledge', {'wait_selector': WS, 'extractor_js': JS}),
    ])

    result = sd.run_doctor(SITE, URL, EVIDENCE)

    assert result['pinned'] is False
    assert sk.get_knowledge(SITE) is None, (
        'an UNVERIFIED pin must never reach the store')


def test_pin_with_mismatched_args_is_rejected(env, monkeypatch):
    scrapes = iter([GOOD_CARDS])             # try passes with args A…
    monkeypatch.setattr(sd, '_scrape', lambda *a, **kw: next(scrapes))
    _script_llm(monkeypatch, [
        ('try_extractor', {'wait_selector': WS, 'extractor_js': JS}),
        ('pin_knowledge', {'wait_selector': WS,
                           'extractor_js': JS + ' // tampered'}),
    ])

    result = sd.run_doctor(SITE, URL, EVIDENCE)

    assert result['pinned'] is False
    assert sk.get_knowledge(SITE) is None


def test_give_up_writes_nothing(env, monkeypatch):
    wall = dict(INSPECT_SAMPLE, auth_wall=True)
    monkeypatch.setattr(sd, '_scrape', lambda *a, **kw: wall)
    _script_llm(monkeypatch, [
        ('inspect_search_page', {}),
        ('give_up', {'reason': 'auth-wall — login page, not drift'}),
    ])

    result = sd.run_doctor(SITE, URL, EVIDENCE)

    assert result['pinned'] is False
    assert 'gave up' in result['detail']
    assert sk.get_knowledge(SITE) is None
    assert 'site_doctor_give_up' in [e for e, _ in env]


def test_loop_exception_never_raises(env, monkeypatch):
    from lib.llm_dispatch import api as dispatch_api

    def boom(*a, **kw):
        raise RuntimeError('gateway 503')

    monkeypatch.setattr(dispatch_api, 'dispatch_chat', boom)
    result = sd.run_doctor(SITE, URL, EVIDENCE)
    assert result['pinned'] is False
    assert 'error' in result['detail']


def test_unknown_site_short_circuits(env):
    result = sd.run_doctor('unknown.example', URL, EVIDENCE)
    assert result['pinned'] is False
    assert 'no recon brief' in result['detail']


def test_shape_errors_contract(env):
    assert sd._shape_errors(GOOD_CARDS) == ''
    assert 'not a list' in sd._shape_errors({'x': 1})
    assert '0 cards' in sd._shape_errors([])
    assert 'title' in sd._shape_errors([{'title': ' ', 'url': 'https://x'}])
    assert 'http url' in sd._shape_errors([{'title': 'ok', 'url': '/explore/a'}])
