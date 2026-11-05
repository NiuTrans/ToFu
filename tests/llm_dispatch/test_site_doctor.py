"""tests/llm_dispatch/test_site_doctor.py — selector-drift autofix (Site Knowledge Layer P3).

Covers lib/site_doctor/ and the owned lib/site_knowledge/ store:

  * store: pin/get/clear roundtrip, version monotonicity, empty-extractor
    refusal;
  * owned lifecycle/trigger isolation lives in test_site_repair_ownership.py;
  * the doctor loop itself: rides lib.agent_loop.agent_loop.run_agent_loop (charter
    iron rule) with a SCRIPTED dispatch — inspect → try(fail) → try(pass)
    → pin lands a verified entry; pin without a matching verification is
    rejected; give_up writes nothing; the loop NEVER raises.

All offline: the LLM is a scripted dispatch_chat fake, the browser is a
scripted browser read, the store lives in tmp_path.
"""

import json

import pytest

import lib.site_doctor as sd
import lib.site_knowledge as sk
from lib.site_doctor import runner, trigger, verification

pytest_plugins = ('tests.support.semantic_authorities',)
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
def env(tmp_path, monkeypatch, semantic_authority):
    """Isolated store + clean trigger state + captured audit events."""
    monkeypatch.setattr(sk, '_STORE_PATH', str(tmp_path / 'site_knowledge.json'))
    monkeypatch.delenv('TOFU_SITE_DOCTOR', raising=False)
    monkeypatch.delenv('TOFU_SITE_DOCTOR_COOLDOWN_S', raising=False)
    trigger._last_attempt.clear()
    trigger._in_flight.clear()
    from functools import partial
    from lib.identity import PrincipalContext
    from lib.site_knowledge.repository import SiteKnowledgeRepository
    factory, owner, _engine = semantic_authority
    repo = SiteKnowledgeRepository(PrincipalContext.user(subject_id='fixture', owner_user_id=owner),
        client_id='fixture-browser', profile='', client_factory=factory)
    execution = runner.RepairExecution(repo, object(), lambda **kw: None,
                                       lambda: False, lambda event: None, lambda: None)
    monkeypatch.setattr(sd, 'run_doctor', partial(runner.run_doctor, execution=execution))
    monkeypatch.setattr(sk, 'get_knowledge', partial(sk.get_knowledge, repository=repo))
    original_pin = sk.pin_knowledge
    def pin(domain, **kwargs):
        kwargs.setdefault('repository', repo)
        kwargs.setdefault('expected_version', repo.read(domain).version)
        return original_pin(domain, **kwargs)
    monkeypatch.setattr(sk, 'pin_knowledge', pin)
    original_clear = sk.clear_knowledge
    def clear(domain):
        revision = repo.read(domain)
        original_clear(domain, repository=repo, expected_version=revision.version)
        return revision.entry is not None
    monkeypatch.setattr(sk, 'clear_knowledge', clear)
    audits = []
    for owner in (trigger, verification):
        monkeypatch.setattr(owner, 'audit_log',
                            lambda event, **kw: audits.append((event, kw)))
    yield audits
    trigger._last_attempt.clear()
    trigger._in_flight.clear()


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


def _script_llm(monkeypatch, rounds):
    """Install a dispatch_chat fake that plays back scripted rounds.

    Each round is one (name, args) pair or a list of them; a string argument
    is raw JSON. None is a bare final answer which ends the loop.
    """
    from lib.llm_dispatch import api as dispatch_api
    script = list(rounds)
    seen = []

    def fake_dispatch(messages, max_tokens=0, temperature=0.0, tools=None,
                      prefer_model=None, log_prefix='', **kwargs):
        seen.append([m.get('role') for m in messages])
        step = script.pop(0) if script else None
        if step is None:
            return 'done', {'total_tokens': 50, '_tool_calls': []}
        calls = step if isinstance(step, list) else [step]
        tool_calls = [
            {'id': f'tc{len(script)}-{index}',
             'function': {'name': name, 'arguments':
                          args if isinstance(args, str) else json.dumps(args)}}
            for index, (name, args) in enumerate(calls)
        ]
        return '', {'total_tokens': 100, '_tool_calls': tool_calls}

    monkeypatch.setattr(dispatch_api, 'dispatch_chat', fake_dispatch)
    return seen


def test_doctor_happy_path_pins_verified_knowledge(env, monkeypatch):
    scrapes = iter([INSPECT_SAMPLE,          # inspect
                    [],                      # try #1: extracts nothing
                    GOOD_CARDS])             # try #2: verified
    monkeypatch.setattr(runner, 'scrape', lambda *a, **kw: next(scrapes))
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
    monkeypatch.setattr(runner, 'scrape', lambda *a, **kw: INSPECT_SAMPLE)
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
    monkeypatch.setattr(runner, 'scrape', lambda *a, **kw: next(scrapes))
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
    monkeypatch.setattr(runner, 'scrape', lambda *a, **kw: wall)
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
    assert verification.shape_errors(GOOD_CARDS) == ''
    assert 'not a list' in verification.shape_errors({'x': 1})
    assert '0 cards' in verification.shape_errors([])
    assert 'title' in verification.shape_errors([{'title': ' ', 'url': 'https://x'}])
    assert 'http url' in verification.shape_errors([{'title': 'ok', 'url': '/explore/a'}])


@pytest.mark.parametrize('invalid_args', [
    {'wait_selector': WS, 'extractor_js': ''},
    {'wait_selector': WS, 'extractor_js': 'x' * (verification.MAX_EXTRACTOR_CHARS + 1)},
    '{broken json',
    ['not', 'an', 'object'],
])
def test_failed_candidate_invalidates_previous_verification(env, monkeypatch, invalid_args):
    monkeypatch.setattr(runner, 'scrape', lambda *a, **kw: GOOD_CARDS)
    _script_llm(monkeypatch, [
        ('try_extractor', {'wait_selector': WS, 'extractor_js': JS}),
        ('try_extractor', invalid_args),
        ('pin_knowledge', {'wait_selector': WS, 'extractor_js': JS}),
    ])

    result = sd.run_doctor(SITE, URL, EVIDENCE)

    assert result['pinned'] is False
    assert sk.get_knowledge(SITE) is None


@pytest.mark.parametrize('sample', [INSPECT_SAMPLE, None])
def test_new_inspection_invalidates_previous_verification(env, monkeypatch, sample):
    scrapes = iter([GOOD_CARDS, sample])
    monkeypatch.setattr(runner, 'scrape', lambda *a, **kw: next(scrapes))
    _script_llm(monkeypatch, [
        ('try_extractor', {'wait_selector': WS, 'extractor_js': JS}),
        ('inspect_search_page', {}),
        ('pin_knowledge', {'wait_selector': WS, 'extractor_js': JS}),
    ])

    assert sd.run_doctor(SITE, URL, EVIDENCE)['pinned'] is False
    assert sk.get_knowledge(SITE) is None


@pytest.mark.parametrize('terminal', ['pin_knowledge', 'give_up', 'auth_wall'])
@pytest.mark.parametrize('same_round', [True, False])
def test_terminal_outcome_prevents_later_work(env, monkeypatch, terminal, same_round):
    scrapes = []

    def scrape(*a, **kw):
        scrapes.append(kw['extractor_js'])
        if kw['extractor_js'] != JS:
            return dict(INSPECT_SAMPLE, auth_wall=True)
        return GOOD_CARDS

    monkeypatch.setattr(runner, 'scrape', scrape)
    candidate = {'wait_selector': WS, 'extractor_js': JS}
    terminal_call = {
        'pin_knowledge': ('pin_knowledge', candidate),
        'give_up': ('give_up', {'reason': 'no safe repair'}),
        'auth_wall': ('inspect_search_page', {}),
    }[terminal]
    remaining = [('try_extractor', candidate), ('pin_knowledge', candidate)]
    script = [('try_extractor', candidate)]
    script += [[terminal_call, *remaining]] if same_round else [terminal_call, *remaining]
    seen = _script_llm(monkeypatch, script)

    result = sd.run_doctor(SITE, URL, EVIDENCE)

    assert result['pinned'] is (terminal == 'pin_knowledge')
    assert len(seen) == 2, 'terminal repair must not call the model again'
    assert len(scrapes) == (2 if terminal == 'auth_wall' else 1)
    if terminal == 'pin_knowledge':
        assert sk.get_knowledge(SITE)['version'] == 1
    else:
        assert sk.get_knowledge(SITE) is None
        assert 'gave up' in result['detail']
    if terminal == 'auth_wall':
        assert 'auth-wall' in result['detail']
        assert ('site_doctor_give_up', {'domain': SITE, 'reason': 'auth-wall'}) in env


@pytest.mark.parametrize('invalid_card', [
    None,
    {'title': ' ', 'url': 'https://example.com/note'},
    {'title': 'ok', 'url': 'http-is-not-a-url'},
    {'title': 'ok', 'url': 'https://'},
])
def test_every_extracted_card_must_pass_before_pin(env, monkeypatch, invalid_card):
    monkeypatch.setattr(runner, 'scrape', lambda *a, **kw: GOOD_CARDS * 3 + [invalid_card])
    _script_llm(monkeypatch, [
        ('try_extractor', {'wait_selector': WS, 'extractor_js': JS}),
        ('pin_knowledge', {'wait_selector': WS, 'extractor_js': JS}),
    ])

    assert sd.run_doctor(SITE, URL, EVIDENCE)['pinned'] is False
    assert sk.get_knowledge(SITE) is None


def test_fresh_verification_can_recover_after_rejection(env, monkeypatch):
    monkeypatch.setattr(runner, 'scrape', lambda *a, **kw: GOOD_CARDS)
    candidate = {'wait_selector': WS, 'extractor_js': JS}
    seen = _script_llm(monkeypatch, [
        ('try_extractor', candidate),
        ('try_extractor', '{bad'),
        ('pin_knowledge', candidate),
        ('try_extractor', dict(candidate, scrolls=2)),
        ('pin_knowledge', candidate),
    ])

    result = sd.run_doctor(SITE, URL, EVIDENCE)

    assert result == {'pinned': True, 'rounds': 5, 'tokens': 500, 'detail': 're-pinned'}
    assert len(seen) == 5
    assert sk.get_knowledge(SITE)['scrolls'] == 2


@pytest.mark.parametrize('terminal', ['pin_knowledge', 'give_up'])
def test_terminal_uses_shared_loop_settlement_and_keeps_skipped_results(env, monkeypatch, terminal):
    from lib.agent_loop import agent_loop

    real_loop = agent_loop.run_agent_loop
    outcomes, tool_results = [], []

    def observed_loop(**kwargs):
        execute = kwargs['execute_tool']

        def observed_execute(*args):
            result = execute(*args)
            tool_results.append(result)
            return result

        kwargs['execute_tool'] = observed_execute
        outcome = real_loop(**kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(agent_loop, 'run_agent_loop', observed_loop)
    monkeypatch.setattr(runner, 'scrape', lambda *a, **kw: GOOD_CARDS)
    candidate = {'wait_selector': WS, 'extractor_js': JS}
    _script_llm(monkeypatch, [[
        ('try_extractor', candidate),
        (terminal, candidate if terminal == 'pin_knowledge' else {'reason': 'no repair'}),
        ('try_extractor', candidate),
        ('pin_knowledge', candidate),
    ]])

    result = sd.run_doctor(SITE, URL, EVIDENCE)

    assert result['rounds'] == 1
    assert result['tokens'] == 100
    assert len(tool_results) == 4
    assert all(item['skipped'] is True for item in tool_results[2:])
    assert outcomes[0].aborted is False
    assert outcomes[0].completed is (terminal == 'pin_knowledge')
    assert outcomes[0].halted is (terminal == 'give_up')


@pytest.mark.parametrize('failure', ['write', 'audit'])
def test_pin_failure_reports_durable_result_without_retry(env, monkeypatch, failure):
    monkeypatch.setattr(runner, 'scrape', lambda *a, **kw: GOOD_CARDS)
    candidate = {'wait_selector': WS, 'extractor_js': JS}
    seen = _script_llm(monkeypatch, [
        ('try_extractor', candidate), ('pin_knowledge', candidate),
    ])

    def fail(*args, **kwargs):
        raise RuntimeError('injected failure')

    if failure == 'write':
        monkeypatch.setattr(sk, 'pin_knowledge', fail)
    else:
        monkeypatch.setattr(verification, 'audit_log', fail)

    result = sd.run_doctor(SITE, URL, EVIDENCE)

    assert 'error' in result['detail']
    assert result['pinned'] is (failure == 'audit')
    assert (sk.get_knowledge(SITE) is not None) is (failure == 'audit')
    assert result['rounds'] == 2
    assert len(seen) == 2


def test_token_budget_still_stops_before_tools(env, monkeypatch):
    from lib.llm_dispatch import api as dispatch_api

    def dispatch(*args, **kwargs):
        return '', {'total_tokens': runner._TOKEN_BUDGET + 1, '_tool_calls': [
            {'id': 'tc-budget', 'function': {'name': 'try_extractor',
             'arguments': json.dumps({'wait_selector': WS, 'extractor_js': JS})}},
        ]}

    monkeypatch.setattr(dispatch_api, 'dispatch_chat', dispatch)
    monkeypatch.setattr(runner, 'scrape', lambda *a, **kw: pytest.fail('budget must stop tools'))

    result = sd.run_doctor(SITE, URL, EVIDENCE)

    assert result['pinned'] is False
    assert result['rounds'] == 1
    assert result['tokens'] == runner._TOKEN_BUDGET + 1
    assert 'token budget exhausted' in result['detail']
    assert sk.get_knowledge(SITE) is None
