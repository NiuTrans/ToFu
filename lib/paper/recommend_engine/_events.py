"""Stream the recommendation pipeline and expose its blocking projection.

``iter_recommend_events`` is the single source of truth for the recommend flow:
the agentic interpretation call (research → structured JSON) and the
*per-candidate* arXiv grounding are surfaced as discrete events so the frontend
can reveal each grounded card the instant it resolves. ``recommend_papers`` is a
thin blocking consumer of this generator.

"""

from lib.llm_errors import AbortedError
from lib.log import get_logger

from ._ground import (
    _GROUND_ATTEMPT_MULTIPLIER,
    _ground_candidate,
    _ground_correction,
    _norm_id,
)
from ._research import _research_and_interpret

logger = get_logger(__name__)


def iter_recommend_events(description, max_results=6, *, abort=None,
                          on_tool_event=None, user_id=None):
    """Stream the describe-to-recommend pipeline as an ordered event sequence.

    This is the single source of truth for the recommend flow: the agentic
    interpretation call (research → structured JSON) and the *per-candidate*
    arXiv grounding are surfaced as discrete events so the frontend can reveal
    each grounded card the instant it resolves. The blocking ``recommend_papers``
    below is a thin consumer of this generator, so both the streaming task and
    the legacy one-shot route share identical ordering + gating.

    Args:
        description: Free-text description of the paper(s) the user recalls.
        max_results: Max grounded cards to yield (capped 1..12).
        abort: Optional zero-arg predicate; when it returns True the generator
            stops (interpretation loop + grounding) and finishes early.
        on_tool_event: Optional ``(event_dict) -> None`` callback fired with the
            interpretation agent's ``tool_start`` / ``tool_done`` research
            events (BEFORE ``interpret_done``), so the caller can stream the
            "researching…" activity. The blocking wrapper leaves this ``None``.

    Yields (each a dict with a ``type`` key):
        {'type': 'interpret_done', 'query': str, 'candidateCount': int,
         'correctionPending': bool, 'agentUsageV1': dict}
            — research + interpretation complete: the model's proposals are
              parsed. ``candidateCount`` is how many grounding *attempts* will
              run (so the UI can paint that many skeleton cards).
        {'type': 'candidate', 'index': int, 'card': <card>}
            — one grounded, real arXiv card (search_arxiv shape + why/venue).
        {'type': 'correction', 'correction': {'note': str, 'paper': <card>|None}}
            — the optional false-premise correction (only when present).
        {'type': 'done', 'query': str, 'resultCount': int,
         'correctionPresent': bool, 'agentUsageV1': dict}
            — terminal success.
        {'type': 'error', 'llmError': bool, 'query': str}
            — terminal failure of the interpretation call (distinct from
              "grounded nothing", which is a normal ``done`` with resultCount 0).
    """
    description = (description or '').strip()
    if not description:
        logger.warning('[Paper:Recommend] Empty description')
        yield {'type': 'done', 'query': description, 'resultCount': 0,
               'correctionPresent': False}
        return

    max_results = max(1, min(int(max_results or 6), 12))

    try:
        research_kwargs = {'abort': abort, 'on_tool_event': on_tool_event}
        if user_id is not None:
            research_kwargs['user_id'] = user_id
        parsed = _research_and_interpret(
            description, max_results, **research_kwargs)
    except AbortedError:
        logger.info('[Paper:Recommend] Interpretation aborted for %.80s', description)
        yield {'type': 'done', 'query': description, 'resultCount': 0,
               'correctionPresent': False}
        return
    except Exception as e:
        logger.error('[Paper:Recommend] Interpretation research failed for %.120s: %s',
                     description, e, exc_info=True)
        yield {'type': 'error', 'llmError': True, 'query': description}
        return

    if parsed is None:
        logger.warning('[Paper:Recommend] No usable interpretation for %.120s', description)
        yield {'type': 'done', 'query': description, 'resultCount': 0,
               'correctionPresent': False}
        return

    agent_usage = (parsed.pop('_agentUsageV1', None)
                   if isinstance(parsed, dict) else None)
    candidates = parsed.get('candidates') if isinstance(parsed, dict) else None
    if not isinstance(candidates, list):
        candidates = []
    attempts = [c for c in candidates[:max_results * _GROUND_ATTEMPT_MULTIPLIER]
                if isinstance(c, dict)]
    raw_correction = parsed.get('correction') if isinstance(parsed, dict) else None
    correction_pending = bool(isinstance(raw_correction, dict)
                              and (raw_correction.get('note') or '').strip())

    interpret_event = {
        'type': 'interpret_done', 'query': description,
        'candidateCount': len(attempts), 'correctionPending': correction_pending,
    }
    if isinstance(agent_usage, dict):
        interpret_event['agentUsageV1'] = agent_usage
    yield interpret_event

    grounded_count = 0
    seen_ids = set()
    for cand in attempts:
        if abort is not None and abort():
            logger.info('[Paper:Recommend] Grounding aborted after %d cards for %.80s',
                        grounded_count, description)
            break
        card = _ground_candidate(cand)
        if not card:
            continue
        base = _norm_id(card['arxiv_id'])
        if base in seen_ids:
            continue
        seen_ids.add(base)
        yield {'type': 'candidate', 'index': grounded_count, 'card': card}
        grounded_count += 1
        if grounded_count >= max_results:
            break

    correction = _ground_correction(raw_correction, seen_ids)
    if correction and correction.get('note'):
        yield {'type': 'correction', 'correction': correction}

    if not grounded_count and not (correction and correction.get('note')):
        logger.info('[Paper:Recommend] Nothing grounded for %.120s (%d proposed)',
                    description, len(candidates))
    else:
        logger.info('[Paper:Recommend] %d grounded / %d proposed%s for %.120s',
                    grounded_count, len(candidates),
                    ' (+correction)' if correction else '', description)

    done_event = {
        'type': 'done', 'query': description, 'resultCount': grounded_count,
        'correctionPresent': bool(correction and correction.get('note')),
    }
    if isinstance(agent_usage, dict):
        done_event['agentUsageV1'] = agent_usage
    yield done_event


def recommend_papers(description, max_results=6, *, user_id=None):
    """Interpret a fuzzy description into grounded, addable arXiv paper cards.

    Blocking convenience wrapper over :func:`iter_recommend_events` — it drains
    the generator and assembles the same aggregate shape the legacy one-shot
    ``/api/v1/paper/recommend`` route has always returned. Prefer the generator
    (via the streaming task) for the interactive UI.

    Args:
        description: Free-text description of the paper(s) the user recalls.
        max_results: Max grounded cards to return (capped 1..12).

    Returns:
        {
          'query': str,
          'correction': {'note': str, 'paper': <card>|None} | None,
          'results': [ <card>, ... ],   # each card = search_arxiv shape + why/venue
          'llmError': bool,             # true only when the interpretation call failed
          'agentUsageV1': dict | None,  # bounded interpretation-loop ledger
        }
        A card is guaranteed to carry a real ``arxiv_id`` (grounded), so the
        frontend can click straight into the existing ``_fetchArxivPaper``.
    """
    query = (description or '').strip()
    out = {'query': query, 'correction': None, 'results': [], 'llmError': False,
           'agentUsageV1': None}
    event_kwargs = {'user_id': user_id} if user_id is not None else {}
    for ev in iter_recommend_events(
            description, max_results, **event_kwargs):
        etype = ev.get('type')
        if etype == 'candidate':
            out['results'].append(ev['card'])
        elif etype == 'interpret_done':
            out['agentUsageV1'] = ev.get('agentUsageV1')
        elif etype == 'correction':
            out['correction'] = ev['correction']
        elif etype == 'error':
            out['llmError'] = bool(ev.get('llmError'))
    return out
