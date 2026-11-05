"""Site repair catalog: supported pages, tool schemas and model instructions.

Entry points: SITE_BRIEFS, DOCTOR_TOOLS, build_prompt. Built-in selector
references load lazily through the maintained search dependency boundary.
"""

from lib.log import get_logger

logger = get_logger(__name__)

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

SITE_BRIEFS = {
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


def _builtin_reference(site: str) -> str:
    """The engine's CURRENT built-in selectors, for the repair prompt."""
    if site != 'xiaohongshu.com':
        return ''
    try:
        from lib.search.search_runtime import prepare_search_dependency_import
        prepare_search_dependency_import()
        from tofu_search.search.engines import xhs
        return (f'wait_selector: {xhs._WAIT_SELECTOR}\n'
                f'extractor_js:\n{xhs._CARD_EXTRACT_JS}')
    except Exception as e:
        logger.debug('[SiteDoctor] builtin reference unavailable: %s', e)
        return ''


def build_prompt(site: str, url: str, evidence: dict) -> str:
    brief = SITE_BRIEFS[site]
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
        'the repair ends immediately with auth-wall; re-pinning cannot fix '
        'a login wall.\n'
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
