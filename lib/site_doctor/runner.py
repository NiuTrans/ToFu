"""Compose one site repair on the shared agent loop, browser and knowledge store.

Entry point: run_doctor. Owns messages, model usage and loop settlement;
RepairSession owns live verification and tool effects. The admitted search
owns execution lifetime; trigger only deduplicates its deferred observations.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from lib.log import get_logger
from lib.site_knowledge.repository import SiteKnowledgeRepository

from .catalog import DOCTOR_TOOLS, SITE_BRIEFS, build_prompt
from .verification import RepairSession

logger = get_logger(__name__)

# Existing repair-run budgets, preserved independently of module layout.
_TOKEN_BUDGET = 60000
_MAX_TOKENS_PER_ROUND = 4096


def scrape(url, *, browser, wait_selector='', extractor_js, scrolls=0):
    """One read through the exact provider captured by the search occurrence."""
    return browser.scrape(url, wait_selector=wait_selector,
                           extractor_js=extractor_js, timeout=25, scrolls=scrolls,
                           require_script_grant=True)


@dataclass(frozen=True)
class RepairExecution:
    """Ports borrowed from one admitted search; no ambient owner or account."""
    repository: SiteKnowledgeRepository
    browser: object
    model_admission: Callable
    abort_check: Callable[[], bool]
    emit: Callable[[dict], None]
    authorize: Callable[[], None]

    def ensure_live(self):
        from lib.llm.llm_errors import AbortedError
        if not all(callable(port) for port in (self.model_admission, self.abort_check, self.authorize)):
            raise ValueError('Site repair requires model admission and cancellation ports')
        if self.abort_check():
            raise AbortedError('Site repair owner cancelled')
        self.authorize()


def run_doctor(site: str, url: str, evidence: dict | None = None,
               *, execution: RepairExecution | None = None,
               model: str | None = None) -> dict:
    """One recon-and-repin run; return pinned/rounds/tokens/detail on failure too."""
    from lib import site_knowledge
    from lib.agent_loop.agent_loop import AbortSignal, LoopDirective, run_agent_loop

    evidence = dict(evidence or {})
    brief = SITE_BRIEFS.get(site)
    if brief is None:
        return {'pinned': False, 'rounds': 0, 'tokens': 0,
                'detail': f'no recon brief for {site}'}
    if execution is None:
        return {'pinned': False, 'rounds': 0, 'tokens': 0,
                'detail': 'site repair requires an admitted search execution'}
    host = urlsplit(url).hostname or ''
    if not (host == site or host.endswith('.' + site)):
        raise ValueError('Repair URL is outside the supported site')
    execution.ensure_live()
    revision = execution.repository.read(site)

    def read(*args, **kwargs):
        execution.ensure_live()
        result = scrape(*args, browser=execution.browser, **kwargs)
        execution.ensure_live()
        return result

    def pin(*args, **kwargs):
        execution.ensure_live()
        return site_knowledge.pin_knowledge(*args, repository=execution.repository,
            expected_version=revision.version, **kwargs)

    repair = RepairSession(site, url, evidence, sample_js=brief['sample_js'],
                           scrape=read, pin=pin)
    messages = [{'role': 'user', 'content': build_prompt(site, url, evidence)}]
    tokens = 0
    budget_exhausted = False
    rounds = 0

    def dispatch(rnd, tools):
        nonlocal tokens
        execution.ensure_live()
        from lib.llm_dispatch.api import dispatch_chat
        content, usage = dispatch_chat(
            messages, max_tokens=_MAX_TOKENS_PER_ROUND, temperature=0.2,
            tools=tools, prefer_model=model,
            owner_user_id=execution.repository.owner_id,
            model_budget_admit=execution.model_admission,
            abort_check=execution.abort_check,
            log_prefix=f'[SiteDoctor:{site}:R{rnd}]')
        tool_calls = []
        if isinstance(usage, dict):
            tokens += int(usage.get('total_tokens') or 0)
            tool_calls = usage.get('_tool_calls') or []
        return {'role': 'assistant', 'content': content or None,
                'tool_calls': tool_calls}, None, usage

    def on_round(rnd, msg, finish, usage):
        nonlocal budget_exhausted, rounds
        rounds = rnd + 1
        if tokens > _TOKEN_BUDGET:
            logger.warning('[SiteDoctor] %s hit the %d-token budget (%d used)',
                           site, _TOKEN_BUDGET, tokens)
            budget_exhausted = True

    def execute(rnd, tool_call):
        execution.ensure_live()
        payload = repair.execute(tool_call)
        messages.append({'role': 'tool', 'tool_call_id': tool_call.get('id', ''),
                         'content': payload if isinstance(payload, str)
                         else json.dumps(payload, ensure_ascii=False)})
        execution.emit({'stage': 'site_repair', 'site': site,
                        'tool': (tool_call.get('function') or {}).get('name'),
                        'detail': 'Site repair: ' + str((tool_call.get('function') or {}).get('name')),
                        'result': payload})
        return payload

    def after_tools(rnd, msg, note):
        if repair.pinned:
            return LoopDirective.complete('re-pinned')
        if repair.gave_up:
            return LoopDirective.halt(f'gave up: {repair.detail}')
        return None

    try:
        outcome = run_agent_loop(
            abort=AbortSignal(lambda: budget_exhausted or execution.abort_check()),
            round_tools=DOCTOR_TOOLS, dispatch=dispatch,
            execute_tool=execute, on_round_result=on_round,
            on_tool_round=lambda rnd, msg: messages.append(msg),
            after_tools=after_tools)
        rounds = outcome.rounds
    except Exception as error:
        logger.error('[SiteDoctor] %s loop raised: %s', site, error, exc_info=True)
        return {'pinned': repair.pinned, 'rounds': rounds, 'tokens': tokens,
                'detail': f'doctor loop error: {type(error).__name__}: {error}'}

    if repair.pinned:
        detail = 're-pinned'
    elif repair.gave_up:
        detail = f'gave up: {repair.detail}'
    elif budget_exhausted:
        detail = 'token budget exhausted before a verified pin'
    elif execution.abort_check():
        detail = 'search owner cancelled before a verified pin'
    else:
        detail = 'loop ended without a pin'
    return {'pinned': repair.pinned, 'rounds': rounds, 'tokens': tokens, 'detail': detail}
