"""Shared wire and domain limits for owner-bound human gates.

Stdin, write approval, chat guidance, and orchestration human nodes all
resolve an in-process waiter by an opaque request identifier.  Keeping their
identifier and response limits here prevents one HTTP surface from accepting
payloads that another surface (or the shared registry) would reject.
"""

from __future__ import annotations


MAX_HUMAN_GATE_REQUEST_ID_LENGTH = 256
MAX_HUMAN_GATE_RESPONSE_LENGTH = 8000


def require_human_gate_request_id(value: object, *, field: str) -> str:
    """Return a normalized bounded request ID or fail closed."""
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a string')
    request_id = value.strip()
    if not request_id:
        raise ValueError(f'{field} is required')
    if len(request_id) > MAX_HUMAN_GATE_REQUEST_ID_LENGTH:
        raise ValueError(
            f'{field} exceeds {MAX_HUMAN_GATE_REQUEST_ID_LENGTH} characters')
    return request_id


__all__ = [
    'MAX_HUMAN_GATE_REQUEST_ID_LENGTH',
    'MAX_HUMAN_GATE_RESPONSE_LENGTH',
    'require_human_gate_request_id',

    'require_declared_choice_response',
]


def require_declared_choice_response(question_round: object, response: str) -> str:
    """Validate a single-choice answer against the durable ask_human round.

    Existing optional human notes remain supported. Model/request-carried option
    lists are never consulted. Text and batch gates retain their existing owner.
    """
    from collections.abc import Mapping
    if not isinstance(question_round, Mapping):
        raise ValueError('Invalid human guidance round')
    if question_round.get('guidanceType') != 'choice':
        return response
    declared = question_round.get('guidanceOptions')
    options = [option.get('label') if isinstance(option, Mapping) else option
               for option in declared] if isinstance(declared, list) else None
    if (not isinstance(options, list) or not options
            or not all(isinstance(option, str) and option for option in options)):
        raise ValueError('Human guidance has no valid declared choices')
    if response not in options and not any(
            response.startswith(option + '\nuser_note: ') for option in options):
        raise ValueError('Human guidance response is not a declared choice')
    return response


def answered_guidance_round(question: dict, response: str, command_id: str) -> dict:
    """Durable receipt and replayable tool result on the existing question round."""
    from copy import deepcopy
    result = deepcopy(question)
    result.update(guidanceResponse=response, guidanceAnswerCommandId=command_id,
                  status='completed', resultSummary=response,
                  resultText=f'Human response: {response}',
                  toolContent=f'Human response: {response}')
    for key in ('results', 'toolMeta'):
        entries = result.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get('guidanceId') == question.get('guidanceId'):
                entry.update(userResponse=response, waiting=False, received=True)
    return result


def preserve_guidance_answers(previous: dict, candidate: dict) -> dict:
    """Producer state is not allowed to undo a durably accepted human answer."""
    from copy import deepcopy
    accepted = {r.get('guidanceId'): r for r in previous.get('toolRounds', [])
                if isinstance(r, dict) and r.get('guidanceAnswerCommandId')}
    if not accepted:
        return candidate
    result = deepcopy(candidate)
    rounds = result.setdefault('toolRounds', [])
    present = set()
    for index, question in enumerate(rounds):
        if isinstance(question, dict) and question.get('guidanceId') in accepted:
            receipt = accepted[question['guidanceId']]
            rounds[index] = answered_guidance_round(
                question, receipt['guidanceResponse'], receipt['guidanceAnswerCommandId'])
            present.add(question['guidanceId'])
    for guidance_id, receipt in accepted.items():
        if guidance_id not in present:
            rounds.append(deepcopy(receipt))
    return result
