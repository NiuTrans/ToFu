"""Live extraction verification and terminal decisions for one site repair.

RepairSession.execute returns model-visible tool results. Browser reads and
knowledge writes enter through explicit callables; this owner never dispatches
a model, starts a worker, or owns conversation messages.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

# Existing site-doctor argument contract; not a runtime tuning parameter.
from lib.site_knowledge.repository import knowledge_contract
MAX_EXTRACTOR_CHARS = knowledge_contract()['$defs']['entry']['properties']['extractor_js']['maxLength']


@dataclass(frozen=True)
class VerifiedExtractor:
    wait_selector: str
    extractor_js: str
    scrolls: int
    count: int


def shape_errors(items) -> str:
    """Return the first violation; every extracted card must satisfy the shape."""
    if not isinstance(items, list):
        return f'extractor returned {type(items).__name__}, not a list'
    if not items:
        return 'extractor produced 0 cards'
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return f'card #{index} is {type(item).__name__}, not an object'
        title = item.get('title')
        if not isinstance(title, str) or len(title.strip()) < 2:
            return f'card #{index} has no usable title'
        url = item.get('url')
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid_url = parsed and parsed.scheme in ('http', 'https') and parsed.hostname
        except ValueError:
            valid_url = False
        if not valid_url:
            return f'card #{index} has no absolute http url'
    return ''


class RepairSession:
    """One live page's evidence; no verification survives another page read."""

    def __init__(self, site: str, url: str, evidence: dict, *,
                 sample_js: str, scrape: Callable, pin: Callable):
        self.site = site
        self.url = url
        self.evidence = evidence
        self.sample_js = sample_js
        self.scrape = scrape
        self.pin = pin
        self.verified: VerifiedExtractor | None = None
        self.pinned = False
        self.gave_up = False
        self.detail = ''

    @property
    def terminal(self) -> bool:
        return self.pinned or self.gave_up

    def execute(self, tool_call: dict) -> dict | str:
        if self.terminal:
            return {'skipped': True, 'reason': 'site repair already finished'}
        function = tool_call.get('function') or {}
        name = function.get('name') or ''
        # Revoke evidence BEFORE parsing: even a malformed new candidate or
        # inspection invalidates the proof belonging to the previous attempt.
        if name in ('try_extractor', 'inspect_search_page'):
            self.verified = None
        try:
            args = json.loads(function.get('arguments') or '{}')
        except (json.JSONDecodeError, TypeError) as error:
            return f'Invalid JSON arguments: {error}'
        if not isinstance(args, dict):
            return 'Invalid JSON arguments: expected an object'

        if name == 'inspect_search_page':
            return self._inspect_page()
        if name == 'try_extractor':
            return self._try_extractor(args)
        if name == 'pin_knowledge':
            return self._pin_knowledge(args)
        if name == 'give_up':
            self._give_up(str(args.get('reason') or '')[:200])
            return 'Recorded. The built-in selectors keep serving.'
        return f'Unknown tool {name!r}'

    def _inspect_page(self) -> dict | str:
        try:
            sample = self.scrape(self.url, extractor_js=self.sample_js, scrolls=1)
        except Exception as error:
            logger.warning('[SiteDoctor] inspect scrape failed: %s', error)
            sample = None
        if sample is None:
            return ('Inspection failed — the browser path is unavailable right '
                    'now. Call give_up ("browser-unavailable") if this persists.')
        if isinstance(sample, dict) and sample.get('auth_wall'):
            self._give_up('auth-wall')
        return sample

    def _try_extractor(self, args: dict) -> dict | str:
        wait_selector = str(args.get('wait_selector') or '')
        extractor_js = str(args.get('extractor_js') or '')
        try:
            scrolls = int(args.get('scrolls') or 0)
        except (TypeError, ValueError) as error:
            logger.debug('[SiteDoctor] bad scrolls value (%s) — using 0', error)
            scrolls = 0
        if not extractor_js.strip() or len(extractor_js) > MAX_EXTRACTOR_CHARS:
            return f'Rejected: extractor_js empty or over {MAX_EXTRACTOR_CHARS} chars.'
        try:
            raw = self.scrape(self.url, wait_selector=wait_selector,
                              extractor_js=extractor_js, scrolls=scrolls)
        except Exception as error:
            logger.warning('[SiteDoctor] try_extractor scrape failed: %s', error)
            raw = None
        if raw is None:
            return {'ok': False, 'reason': 'browser path unavailable — verification impossible'}
        error = shape_errors(raw)
        if error:
            return {'ok': False, 'reason': error, 'raw_head': str(raw)[:300]}
        self.verified = VerifiedExtractor(wait_selector, extractor_js, scrolls, len(raw))
        return {'ok': True, 'count': len(raw), 'sample': raw[:3],
                'next': 'call pin_knowledge with THESE EXACT '
                        'wait_selector + extractor_js to persist'}

    def _pin_knowledge(self, args: dict) -> dict | str:
        wait_selector = str(args.get('wait_selector') or '')
        extractor_js = str(args.get('extractor_js') or '')
        verified = self.verified
        if (verified is None or verified.wait_selector != wait_selector
                or verified.extractor_js != extractor_js):
            return ('Rejected: pin_knowledge only accepts the EXACT '
                    '(wait_selector, extractor_js) pair that the '
                    'immediately-preceding try_extractor proved. Verify first.')
        entry = self.pin(
            self.site, extractor_js=extractor_js, wait_selector=wait_selector,
            scrolls=verified.scrolls, verified_by='site-doctor',
            evidence={'anchors': self.evidence.get('anchors'),
                      'page_title': self.evidence.get('page_title'),
                      'verified_count': verified.count},
            notes=str(args.get('notes') or ''))
        self.pinned = True
        self.verified = None
        logger.warning('[SiteDoctor] %s re-pinned to knowledge v%s '
                       '(%d cards verified live)', self.site,
                       entry.get('version'), verified.count)
        audit_log('site_knowledge_pinned', domain=self.site,
                  version=entry.get('version'), verified_count=verified.count)
        return {'pinned': True, 'version': entry.get('version'),
                'note': 'engines use this on their next search'}

    def _give_up(self, reason: str) -> None:
        self.verified = None
        self.gave_up = True
        self.detail = reason
        logger.warning('[SiteDoctor] %s gave up: %s', self.site, reason)
        audit_log('site_doctor_give_up', domain=self.site, reason=reason)
