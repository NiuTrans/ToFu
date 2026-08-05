"""lib/site_doctor.py — selector-drift autofix (Site Knowledge Layer P3).

Trigger: tofu-search emits a site-drift signal — the page demonstrably
rendered note anchors yet the configured selectors extracted ZERO cards
(docs/SITE_KNOWLEDGE_LAYER_DESIGN.md §6). ``on_site_drift`` (registered as
the library's drift listener by lib/search_bridge at install) guards and
spawns ONE bounded agent loop that re-cons the live page through the user's
own browser, verifies candidate selectors by actually extracting with them,
and pins the winners into lib/site_knowledge — engines pick the new
knowledge up on their very next search, no release, no restart.

Hard rules (all load-bearing):

  * **Rides lib.agent_loop.run_agent_loop** — charter iron rule: new agentic
    capabilities use the shared chassis, never a private loop.
  * **Never pins unverified selectors.** pin_knowledge only accepts the
    EXACT (wait_selector, extractor_js) pair that the immediately-preceding
    try_extractor proved against the live page (>=1 shape-valid card).
  * **An auth wall is a give-up, not a re-pin.** Login/captcha pages are an
    account problem; new selectors cannot fix them.
  * **Cheap and single.** One doctor at a time fleet-wide, a per-site
    cooldown between attempts, a hard token budget — drift is rare and the
    loop costs real tokens.
  * **Never raises.** The trigger fires inside a search worker thread; every
    failure path degrades to a log + audit line and the built-in selectors
    keep serving.
"""

from __future__ import annotations

import json
import os
import threading
import time

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

__all__ = ['on_site_drift', 'run_doctor', 'DOCTOR_TOOLS']

#: Bounded rounds (tool-eligible) and the cumulative token ceiling per run.
_MAX_ROUNDS = 6
_TOKEN_BUDGET = 60000
_MAX_TOKENS_PER_ROUND = 4096
#: Bound on selector/JS arguments the model may hand us.
_MAX_EXTRACTOR_CHARS = 6000

_ENV_OFF = ('0', 'false', 'no', 'off')

_flight_lock = threading.Lock()
_cool_lock = threading.Lock()
_last_attempt: dict = {}


def _doctor_enabled() -> bool:
    return os.environ.get('TOFU_SITE_DOCTOR', '1').strip().lower() not in _ENV_OFF


def _cooldown_s() -> int:
    try:
        return int(os.environ.get('TOFU_SITE_DOCTOR_COOLDOWN_S', '10800'))
    except ValueError as e:
        logger.debug('[SiteDoctor] TOFU_SITE_DOCTOR_COOLDOWN_S parse failed, '
                     'using default: %s', e)
        return 10800


# ── Per-site recon briefs ─────────────────────────────────
#
# Everything the doctor needs to re-con a site WITHOUT hardcoding site
# logic into the loop: where the failing page is, what the DOM sample
# should measure, and what a valid extracted card looks like. Internalizing
# a second site = adding one entry here (append-only, per the registry
# principle in the design doc).

_XHS_SAMPLE_JS = r"""
(() => {
  const q = (s) => document.querySelectorAll(s).length;
  const text = (document.body && document.body.innerText || '');
  const anchors = Array.from(document.querySelectorAll(
    'a[href*="/explore/"], a[href*="/search_result/"]'))
    .slice(0, 3).map(a => (a.outerHTML || '').slice(0, 600));
  const authWall = /安全验证|滑动验证|登录后查看|请登录|captcha|verify/i
    .test(text.slice(0, 3000)) || /login/i.test(location.href);
  return {
    url: location.href, title: document.title || '', auth_wall: authWall,
    counts: {
      note_item: q('section.note-item'),
      explore_anchors: q('a[href*="/explore/"]'),
      search_result_anchors: q('a[href*="/search_result/"]'),
      sections: q('section'), inputs: q('input'),
    },
    anchor_samples: anchors,
    text_head: text.slice(0, 400),
  };
})()
"""

_SITE_BRIEFS = {
    'xiaohongshu.com': {
        'sample_js': _XHS_SAMPLE_JS,
        'card_contract': (
            'a list of {title, snippet, url} — one per note card; url must '
            'be an absolute http(s) link to the note (contains /explore/ or '
            '/search_result/), title the note caption text'),
    },
}


# ── Narrow tool schemas ───────────────────────────────────

DOCTOR_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'inspect_search_page',
            'description': (
                'Open the failing search page in a background tab of the '
                "user's real browser and return a DOM structural sample: "
                'element counts for candidate selectors, the outerHTML of '
                'the first note anchors, page title, and whether the page is '
                'an AUTH WALL (login/captcha). Always call this FIRST.'),
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'try_extractor',
            'description': (
                'Verify a candidate extractor against the LIVE page: opens it '
                'in a background tab, runs your list-form extractor JS in-page, '
                'and returns how many shape-valid cards it produced plus a '
                'sample. Only an extractor that passes here may be pinned.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'wait_selector': {'type': 'string',
                                      'description': 'CSS selector to await '
                                                     'before extracting.'},
                    'extractor_js': {'type': 'string',
                                     'description': 'JS expression returning a '
                                                    'LIST of card dicts.'},
                    'scrolls': {'type': 'integer',
                                'description': 'Bottom-scrolls before '
                                               'extracting (lazy feeds).'},
                },
                'required': ['wait_selector', 'extractor_js'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'pin_knowledge',
            'description': (
                'Pin the VERIFIED selectors as the site\'s new extraction '
                'knowledge (engines pick it up on their next search). Only '
                'accepts the exact (wait_selector, extractor_js) pair that '
                'the immediately-preceding try_extractor proved — anything '
                'else is rejected.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'wait_selector': {'type': 'string'},
                    'extractor_js': {'type': 'string'},
                    'notes': {'type': 'string',
                              'description': 'One line: what changed in the '
                                             'DOM and why these selectors fix it.'},
                },
                'required': ['wait_selector', 'extractor_js'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'give_up',
            'description': (
                'Declare this re-con unsuccessful (e.g. auth wall, or two '
                'failed verifications). Records the reason and ends the run — '
                'the built-in selectors keep serving.'),
            'parameters': {
                'type': 'object',
                'properties': {'reason': {'type': 'string'}},
                'required': ['reason'],
            },
        },
    },
]


def _shape_errors(items) -> str:
    """'' when ``items`` is a list of >=1 shape-valid cards, else the reason."""
    if not isinstance(items, list):
        return f'extractor returned {type(items).__name__}, not a list'
    if not items:
        return 'extractor produced 0 cards'
    for i, it in enumerate(items[:5]):
        if not isinstance(it, dict):
            return f'card #{i} is {type(it).__name__}, not an object'
        title = it.get('title')
        url = it.get('url')
        if not isinstance(title, str) or len(title.strip()) < 2:
            return f'card #{i} has no usable title'
        if not isinstance(url, str) or not url.startswith('http'):
            return f'card #{i} has no absolute http url'
    return ''


def _scrape(url, *, wait_selector='', extractor_js, scrolls=0):
    """Run one verification scrape through the registered browser provider."""
    from tofu_search.providers import get_browser_provider
    provider = get_browser_provider()
    if provider is None:
        return None
    return provider.scrape(url, wait_selector=wait_selector,
                           extractor_js=extractor_js, timeout=25,
                           scrolls=scrolls)


def _builtin_reference(site: str) -> str:
    """The engine's CURRENT built-in selectors, for the repair prompt."""
    if site != 'xiaohongshu.com':
        return ''
    try:
        from tofu_search.search.engines import xhs
        return (f'wait_selector: {xhs._WAIT_SELECTOR}\n'
                f'extractor_js:\n{xhs._CARD_EXTRACT_JS}')
    except Exception as e:
        logger.debug('[SiteDoctor] builtin reference unavailable: %s', e)
        return ''


def _build_prompt(site: str, url: str, evidence: dict) -> str:
    brief = _SITE_BRIEFS[site]
    builtin = _builtin_reference(site)
    return (
        f'You are the site doctor for {site}. Its search extraction BROKE: '
        f'the page rendered {evidence.get("anchors", "?")} note anchors but '
        'the configured selectors extracted ZERO cards — the site DOM '
        'drifted. Re-pin the extraction knowledge.\n\n'
        f'Failing page: {url}\n'
        f'Page title at failure: {evidence.get("page_title", "")!r}\n\n'
        '## Card contract\n'
        f'Your extractor must return {brief["card_contract"]}.\n\n'
        '## Required workflow\n'
        '1. Call inspect_search_page FIRST. If it reports auth_wall=true, '
        'the account — not the selectors — is the problem: call give_up '
        "('auth-wall') immediately; re-pinning cannot fix a login wall.\n"
        '2. Study the anchor samples, write a list-form extractor JS and a '
        'wait_selector, then call try_extractor to verify them against the '
        'LIVE page. Iterate at most twice.\n'
        '3. Only after a try_extractor passes, call pin_knowledge with the '
        'EXACT same wait_selector + extractor_js. It rejects anything else.\n'
        '4. If two verifications fail, call give_up with what you learned.\n\n'
        'Keep the extractor robust: tolerate missing optional fields, '
        'de-duplicate by URL, cap output at 30 cards.\n'
        + (f'\n## Current built-in selectors (the ones that drifted — repair '
           f'these, do not start from zero)\n{builtin}\n' if builtin else '')
    )


# ── Trigger ───────────────────────────────────────────────

def on_site_drift(site: str, url: str, evidence: dict) -> None:
    """tofu-search drift-listener entry. Fast, non-blocking, never raises.

    Runs inside a search worker thread: guards, then spawns the doctor on a
    daemon thread and returns immediately.
    """
    try:
        if not _doctor_enabled():
            logger.debug('[SiteDoctor] drift on %s ignored (TOFU_SITE_DOCTOR off)', site)
            return
        if site not in _SITE_BRIEFS:
            logger.debug('[SiteDoctor] no recon brief for %s — skipping', site)
            return
        with _cool_lock:
            last = _last_attempt.get(site, 0.0)
            if time.time() - last < _cooldown_s():
                logger.info('[SiteDoctor] drift on %s inside cooldown — skipping', site)
                return
            _last_attempt[site] = time.time()
        if not _flight_lock.acquire(blocking=False):
            logger.info('[SiteDoctor] another doctor run in flight — skipping %s', site)
            return
        logger.warning('[SiteDoctor] selector drift on %s (%s anchors rendered, '
                       '0 cards) — spawning recon doctor', site,
                       (evidence or {}).get('anchors', '?'))
        audit_log('site_doctor_triggered', domain=site,
                  anchors=(evidence or {}).get('anchors'),
                  page_title=str((evidence or {}).get('page_title') or '')[:120])
        t = threading.Thread(target=_run_doctor_safe, args=(site, url, evidence),
                             name=f'site-doctor-{site}', daemon=True)
        t.start()
    except Exception as e:
        logger.error('[SiteDoctor] on_site_drift failed for %s: %s', site, e,
                     exc_info=True)


def _run_doctor_safe(site: str, url: str, evidence: dict) -> None:
    try:
        result = run_doctor(site, url, evidence)
        audit_log('site_doctor_done', domain=site, pinned=result.get('pinned'),
                  rounds=result.get('rounds'), tokens=result.get('tokens'),
                  detail=str(result.get('detail') or '')[:200])
    except Exception as e:
        logger.error('[SiteDoctor] doctor crashed for %s: %s', site, e,
                     exc_info=True)
        audit_log('site_doctor_error', domain=site,
                  error=f'{type(e).__name__}: {e}'[:200])
    finally:
        _flight_lock.release()


# ── The doctor loop ───────────────────────────────────────

def run_doctor(site: str, url: str, evidence: dict | None = None,
               *, model: str | None = None) -> dict:
    """One bounded recon-and-repin run for ``site``. Never raises.

    Returns ``{'pinned', 'rounds', 'tokens', 'detail'}``.
    """
    from lib.agent_loop import AbortSignal, run_agent_loop

    evidence = dict(evidence or {})
    brief = _SITE_BRIEFS.get(site)
    if brief is None:
        return {'pinned': False, 'rounds': 0, 'tokens': 0,
                'detail': f'no recon brief for {site}'}

    state = {'tokens': 0, 'pinned': False, 'gave_up': False,
             'verified': None, 'budget_exhausted': False, 'detail': ''}
    messages = [{'role': 'user', 'content': _build_prompt(site, url, evidence)}]

    def _reply(tc_id, payload):
        messages.append({'role': 'tool', 'tool_call_id': tc_id,
                         'content': payload if isinstance(payload, str)
                         else json.dumps(payload, ensure_ascii=False)})

    def _dispatch(rnd, tools):
        from lib.llm_dispatch.api import dispatch_chat
        content, usage = dispatch_chat(
            messages, max_tokens=_MAX_TOKENS_PER_ROUND, temperature=0.2,
            tools=tools, prefer_model=model,
            log_prefix=f'[SiteDoctor:{site}:R{rnd}]')
        tool_calls = []
        if isinstance(usage, dict):
            state['tokens'] += int(usage.get('total_tokens') or 0)
            tool_calls = usage.get('_tool_calls') or []
        msg = {'role': 'assistant', 'content': content or None,
               'tool_calls': tool_calls}
        return msg, None, usage

    def _on_round(rnd, msg, finish, usage):
        if state['tokens'] > _TOKEN_BUDGET:
            logger.warning('[SiteDoctor] %s hit the %d-token budget (%d used)',
                           site, _TOKEN_BUDGET, state['tokens'])
            state['budget_exhausted'] = True

    def _on_tool_round(rnd, msg):
        messages.append(msg)

    def _execute(rnd, tc):
        fn = tc.get('function') or {}
        name = fn.get('name') or ''
        tc_id = tc.get('id', '')
        try:
            args = json.loads(fn.get('arguments') or '{}')
        except (json.JSONDecodeError, TypeError) as e:
            _reply(tc_id, f'Invalid JSON arguments: {e}')
            return
        if not isinstance(args, dict):
            args = {}

        if name == 'inspect_search_page':
            try:
                sample = _scrape(url, extractor_js=brief['sample_js'], scrolls=1)
            except Exception as e:
                logger.warning('[SiteDoctor] inspect scrape failed: %s', e)
                sample = None
            if sample is None:
                _reply(tc_id, 'Inspection failed — the browser path is '
                              'unavailable right now. Call give_up '
                              '("browser-unavailable") if this persists.')
                return
            if isinstance(sample, dict) and sample.get('auth_wall'):
                logger.warning('[SiteDoctor] %s shows an AUTH WALL — not a '
                               'selector problem', site)
            _reply(tc_id, sample)
        elif name == 'try_extractor':
            ws = str(args.get('wait_selector') or '')
            js = str(args.get('extractor_js') or '')
            try:
                scrolls = int(args.get('scrolls') or 0)
            except (TypeError, ValueError) as e:
                logger.debug('[SiteDoctor] bad scrolls value (%s) — using 0', e)
                scrolls = 0
            if not js.strip() or len(js) > _MAX_EXTRACTOR_CHARS:
                _reply(tc_id, f'Rejected: extractor_js empty or over '
                              f'{_MAX_EXTRACTOR_CHARS} chars.')
                return
            try:
                raw = _scrape(url, wait_selector=ws, extractor_js=js,
                              scrolls=scrolls)
            except Exception as e:
                logger.warning('[SiteDoctor] try_extractor scrape failed: %s', e)
                raw = None
            if raw is None:
                state['verified'] = None
                _reply(tc_id, {'ok': False,
                               'reason': 'browser path unavailable — '
                                         'verification impossible'})
                return
            err = _shape_errors(raw)
            if err:
                state['verified'] = None
                _reply(tc_id, {'ok': False, 'reason': err,
                               'raw_head': str(raw)[:300]})
                return
            state['verified'] = {'wait_selector': ws, 'extractor_js': js,
                                 'scrolls': scrolls, 'count': len(raw)}
            _reply(tc_id, {'ok': True, 'count': len(raw),
                           'sample': raw[:3],
                           'next': 'call pin_knowledge with THESE EXACT '
                                   'wait_selector + extractor_js to persist'})
        elif name == 'pin_knowledge':
            ws = str(args.get('wait_selector') or '')
            js = str(args.get('extractor_js') or '')
            notes = str(args.get('notes') or '')
            v = state.get('verified')
            if not v or v['wait_selector'] != ws or v['extractor_js'] != js:
                _reply(tc_id, 'Rejected: pin_knowledge only accepts the EXACT '
                              '(wait_selector, extractor_js) pair that the '
                              'immediately-preceding try_extractor proved. '
                              'Verify first.')
                return
            from lib import site_knowledge
            entry = site_knowledge.pin_knowledge(
                site, extractor_js=js, wait_selector=ws,
                scrolls=v['scrolls'], verified_by='site-doctor',
                evidence={'anchors': evidence.get('anchors'),
                          'page_title': evidence.get('page_title'),
                          'verified_count': v['count']},
                notes=notes)
            state['pinned'] = True
            logger.warning('[SiteDoctor] %s re-pinned to knowledge v%s '
                           '(%d cards verified live)', site,
                           entry.get('version'), v['count'])
            audit_log('site_knowledge_pinned', domain=site,
                      version=entry.get('version'),
                      verified_count=v['count'])
            _reply(tc_id, {'pinned': True, 'version': entry.get('version'),
                           'note': 'engines use this on their next search'})
        elif name == 'give_up':
            reason = str(args.get('reason') or '')[:200]
            state['gave_up'] = True
            state['detail'] = reason
            logger.warning('[SiteDoctor] %s gave up: %s', site, reason)
            audit_log('site_doctor_give_up', domain=site, reason=reason)
            _reply(tc_id, 'Recorded. The built-in selectors keep serving.')
        else:
            _reply(tc_id, f'Unknown tool {name!r}')

    combined = AbortSignal(lambda: bool(state.get('budget_exhausted')))
    rounds = 0
    try:
        outcome = run_agent_loop(
            abort=combined, max_tool_rounds=_MAX_ROUNDS,
            round_tools=DOCTOR_TOOLS, dispatch=_dispatch,
            execute_tool=_execute, on_round_result=_on_round,
            on_tool_round=_on_tool_round)
        rounds = outcome.rounds
    except Exception as e:
        logger.error('[SiteDoctor] %s loop raised: %s', site, e, exc_info=True)
        return {'pinned': False, 'rounds': rounds, 'tokens': state['tokens'],
                'detail': f'doctor loop error: {type(e).__name__}: {e}'}

    if state['pinned']:
        detail = 're-pinned'
    elif state['gave_up']:
        detail = f"gave up: {state['detail']}"
    elif state['budget_exhausted']:
        detail = 'token budget exhausted before a verified pin'
    else:
        detail = 'loop ended without a pin'
    return {'pinned': state['pinned'], 'rounds': rounds,
            'tokens': state['tokens'], 'detail': detail}
