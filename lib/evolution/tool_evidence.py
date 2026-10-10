"""Bounded projection of tool messages retained at terminal capture.

Stage outcomes own persistence, ownership and sealing. This module serializes
the retained tool messages without clipping their IDs or content; it never
claims to recover messages removed by earlier compaction or tool-output policy.
Entry points: chunks, describe, preview. No stores, models or retained buffers.
"""
from __future__ import annotations

from lib.research.observation_audit import decode_observation_json

import hashlib
import json

# Project and standalone command producers publish different display identities.
COMMAND_PRODUCER_TOOL_NAMES = frozenset({'run_command', 'code_exec'})

SCIENTIFIC_MARKERS = ('"scientific_observations"','"scientific_outcome"')

PART_CHARS = 30000
SCOPE = 'terminal_retained_tool_messages'
BACKGROUND_SCOPE = 'terminal_retained_tool_messages_and_background_completions'
SCIENTIFIC_SCOPE='terminal_retained_tool_messages_and_scientific_command_originals'
COMBINED_SCOPE='terminal_retained_tool_messages_and_background_completions_and_scientific_command_originals'


def _json(value):
    if isinstance(value, str):
        yield '"'
        for offset in range(0, len(value), PART_CHARS):
            yield json.dumps(value[offset:offset + PART_CHARS], ensure_ascii=False)[1:-1]
        yield '"'
    elif isinstance(value, dict):
        yield '{'
        for index, key in enumerate(sorted(value)):
            if not isinstance(key, str):
                raise ValueError('tool evidence JSON keys must be strings')
            if index:
                yield ','
            yield from _json(key)
            yield ':'
            yield from _json(value[key])
        yield '}'
    elif isinstance(value, (list, tuple)):
        yield '['
        for index, item in enumerate(value):
            if index:
                yield ','
            yield from _json(item)
        yield ']'
    else:
        yield json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _messages(task):
    for index, message in enumerate(task.get('messages') or []):
        if message.get('role') == 'tool':
            yield {'message_index': index, 'tool_call_id': str(message.get('tool_call_id') or ''),
                   'content': message.get('content'),
                   **({'name': message['name']} if 'name' in message else {})}

    reader=task.get('_evolutionScientificCommandRead')
    if callable(reader):yield from reader()
    yield from task.get('_evolutionBackgroundEvidence') or []


def _reverse_messages(task):
    yield from reversed(task.get('_evolutionBackgroundEvidence') or [])
    for message in reversed(task.get('messages') or []):
        if message.get('role') == 'tool':
            yield message


def chunks(task):
    """Stream canonical JSONL in fixed Unicode-character parts."""
    pending = ''
    for message in _messages(task):
        for fragment in _json(message):
            while fragment:
                count = PART_CHARS - len(pending)
                pending += fragment[:count]
                fragment = fragment[count:]
                if len(pending) == PART_CHARS:
                    yield pending
                    pending = ''
        pending += '\n'
        if len(pending) == PART_CHARS:
            yield pending
            pending = ''
    if pending:
        yield pending


def preview(task):
    """The bounded review preview, with explicit clipping/omission status."""
    evidence, total, truncated = [], 0, False
    for message in _reverse_messages(task):
        content = message.get('content')
        if isinstance(content, str):
            text = content[:2000]
            clipped = len(content) > 2000
        else:
            fragments, remaining = [], 2000
            clipped = False
            for fragment in _json(content):
                fragments.append(fragment[:remaining])
                remaining -= min(remaining, len(fragment))
                if remaining == 0:
                    clipped = True
                    break
            text = ''.join(fragments)
        identifier = str(message.get('tool_call_id') or '')
        item = {'tool_call_id': identifier[:160], 'result': text}
        size = len(json.dumps(item))
        if total + size > 16000:
            truncated = True
            break
        evidence.append(item)
        total += size
        truncated |= clipped or len(identifier) > 160
    return list(reversed(evidence)), truncated


def describe(task):
    digest, count, parts = hashlib.sha256(), 0, 0
    for text in chunks(task):
        digest.update(text.encode('utf-8'))
        count += len(text)
        parts += 1
    scientific=callable(task.get('_evolutionScientificCommandRead'))
    background=bool(task.get('_evolutionBackgroundEvidence'))
    scope=(COMBINED_SCOPE if background else SCIENTIFIC_SCOPE) if scientific else BACKGROUND_SCOPE if background else SCOPE
    return {'content_chars': count, 'content_parts': parts,
            'content_sha256': digest.hexdigest(),
            'message_count': sum(1 for _ in _messages(task)),
            'scope':scope,'preview_truncated':preview(task)[1] or scientific}


def command_execution_observed(task):
    """Prove command completion from producer metadata or a terminal reply.

    Structured execution metadata survives model-context compaction. Background
    handles and failed/interrupted commands are not completed successful work.
    This does not prove benchmark authenticity or experimental correctness.
    """
    from lib.background_command_projection import command_result_status
    witnessed=task.get('_evolutionScientificCommandExecuted')
    if callable(witnessed) and witnessed():return True
    calls = set()
    for row in task.get('toolRounds') or []:
        if not isinstance(row, dict):
            continue
        if (row.get('toolName') not in COMMAND_PRODUCER_TOOL_NAMES or row.get('status') != 'done'
                or not isinstance(row.get('toolCallId'), str) or not row['toolCallId']):
            continue
        metadata = row.get('results')
        if isinstance(metadata, list) and metadata:
            for item in metadata:
                if (isinstance(item, dict) and type(item.get('exitCode')) in (str, int)
                        and item['exitCode'] in (0, '0')
                        and not any(item.get(key) for key in
                            ('notRun', 'backgrounded', 'timedOut', 'interrupted'))):
                    return True
            # Producer refusal/background/failure wins over a display string.
            continue
        calls.add(row['toolCallId'])
    for message in task.get('messages') or []:
        if (not isinstance(message, dict) or not isinstance(message.get('tool_call_id'), str)
                or message.get('role') != 'tool' or message['tool_call_id'] not in calls):
            continue
        content = message.get('content')
        if not isinstance(content, str) or not content.startswith('$ '):
            continue
        outcome = command_result_status(content)
        if (outcome.get('exitCode') == 0 and not outcome.get('timedOut')
                and not outcome.get('interrupted')):
            return True
    return False


def capture_background_state(task, *, user_id, retain_unresolved=True):
    """Read current producer launch handles once, without waiting or consuming."""
    if task.get('_userId') != user_id or not isinstance(task.get('id'),str) or not task['id']:
        return [], []
    from lib.tasks_pkg.background_commands import check_background_command
    handles = {}
    for row in task.get('toolRounds') or []:
        if not isinstance(row, dict) or row.get('toolName') not in COMMAND_PRODUCER_TOOL_NAMES or row.get('status') != 'done':
            continue
        if not isinstance(row.get('toolCallId'), str) or not row['toolCallId']:
            continue
        metadata = row.get('results')
        if not isinstance(metadata, list):
            continue
        for meta in metadata:
            if not isinstance(meta, dict) or meta.get('exitCode') != 'background':
                continue
            handle = meta.get('backgroundCommandId') or row.get('backgroundCommandId')
            if (not isinstance(handle, str) or not handle.startswith('bg_')
                    or (row.get('backgroundCommandId') and row['backgroundCommandId'] != handle)):
                continue
            handles.setdefault(handle, row['toolCallId'])
    observed, pending = [], []
    for handle in sorted(handles):
        result = check_background_command(handle, user_id=user_id, wait_s=0,task_id=task['id'])
        if (not isinstance(result, dict) or result.get('command_id') != handle
                or result.get('status') != 'completed'
                or not isinstance(result.get('result'), str)):
            # A declared launch remains unresolved until its owned producer
            # supplies a complete result. Missing/foreign/malformed replies
            # cannot silently remove the launch from the terminal join.
            if retain_unresolved or (isinstance(result,dict) and result.get('command_id')==handle and result.get('status')=='running'):
                pending.append({'command_id':handle,'source_tool_call_id':handles[handle]})
            continue
        observed.append({'tool_call_id': 'background:' + handle, 'name': 'run_command',
            'source': 'owned_background_command_completion', 'command_id': handle,
            'source_tool_call_id': handles[handle], 'content': result['result'],
            'error': result.get('error')})
    return observed, pending


def capture_background_results(task, *, user_id):
    return capture_background_state(task, user_id=user_id)[0]


def background_execution_observed(observations):
    """Completion and failure outputs are retained; only valid success passes."""
    from lib.background_command_projection import command_result_status
    for result in observations:
        text = result['content']
        if result.get('error') or not isinstance(text, str) or not text.startswith('$ '):
            continue
        outcome = command_result_status(text)
        if (outcome.get('exitCode') == 0 and not outcome.get('timedOut')
                and not outcome.get('interrupted')):
            return True
    return False


def observe_command_execution(task, *, user_id):
    """Observe current completed execution; never wait, mutate or replay."""
    if task.get('_userId') != user_id:
        return False
    return command_execution_observed(task) or background_execution_observed(
        capture_background_results(task, user_id=user_id))


def normalize_scientific_protocol(protocol):
    """Reuse Research normalization; protocol authority precedes execution."""
    from lib.research.program import normalize_program_fields
    from .contracts import EvolutionContractError
    try:
        value = normalize_program_fields({'protocol': protocol})['protocol']
    except ValueError as exc:
        raise EvolutionContractError(str(exc)) from exc
    seeds = value['random_seeds']
    if (not all(value.get(key) for key in ('dataset','baseline','primary_metric','decision_rule'))
            or len(seeds)<2 or len(set(seeds))!=len(seeds)):
        raise EvolutionContractError('scientific protocol requires a frozen numeric rule, metric, dataset, baseline and distinct paired seeds')
    return value


def scientific_protocol_digest(protocol):
    """Bind numerical audits to the complete normalized precommitted protocol."""
    value=normalize_scientific_protocol(protocol)
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        separators=(',',':')).encode()).hexdigest()


def scientific_audit(task, protocol):
    """Pure bounded aggregate over retained, current command measurements.

    Foreground records require their own execution proof; owned background
    snapshots were authorized by capture_background_state. No final prose,
    file retrieval, producer success flag or model verdict supplies numbers.
    """
    import hashlib as hashlib
    from lib.research.action import _tool_receipts, _observed_tool_payload
    from lib.research.observation_audit import AUDIT_VERSION, verify_observations
    protocol = normalize_scientific_protocol(protocol)
    result={'audit_version':AUDIT_VERSION,'capture_policy_version':1,
        'protocol_sha256':scientific_protocol_digest(protocol),
        'observation_count':0,'failure_count':0,'evidence_sum':'0'*64,
        'outcomes':dict.fromkeys(('supported','refuted','inconclusive'),0)}
    rounds={row.get('toolCallId'):row for row in task.get('toolRounds') or []
        if isinstance(row,dict) and row.get('toolName') in COMMAND_PRODUCER_TOOL_NAMES and isinstance(row.get('toolCallId'),str) and row.get('toolCallId')}
    captured_ids=task.get('_evolutionScientificCommandCallIds')
    captured_ids=set(captured_ids()) if callable(captured_ids) else set()
    background_launches=set(task.get('_evolutionBackgroundLaunchCallIds') or ())
    def observations():
        for message in task.get('messages') or []:
            if not isinstance(message,dict) or message.get('role')!='tool':continue
            call=message.get('tool_call_id')
            if not isinstance(call,str) or call not in rounds or call in captured_ids or call in background_launches:continue
            row=rounds[call]
            yield dict(row,toolContent=message.get('content')),command_execution_observed({'toolRounds':[row],'messages':[message]})
        reader=task.get('_evolutionScientificCommandRead')
        if callable(reader):
            for message in reader():
                if message['tool_call_id'] in background_launches:continue
                yield {'toolName':'run_command','toolCallId':message['tool_call_id'],
                    'status':'error' if message['error'] else 'done','toolContent':message['content'],
                    '_captured_scientific_original':any(marker in message['content'] for marker in SCIENTIFIC_MARKERS)},background_execution_observed([message])
        for message in task.get('_evolutionBackgroundEvidence') or []:
            yield {'toolName':'run_command','toolCallId':message.get('source_tool_call_id'),
                'status':'error' if message.get('error') else 'done','toolContent':message.get('content'),
                '_captured_scientific_original':any(marker in str(message.get('content') or '')
                    for marker in ('"scientific_observations"','"scientific_outcome"'))},background_execution_observed([message])
    for row,executed in observations():
        receipt=_tool_receipts([row])[0]
        captured=bool(row.get('_captured_scientific_original')) or any(
            marker in str(row.get('toolContent') or '') for marker in SCIENTIFIC_MARKERS)
        payload=None
        if receipt['status']!='pending':
            try:
                if receipt.get('observation_excerpt'):payload=decode_observation_json(receipt['observation_excerpt'])
                else:payload,_=_observed_tool_payload(row,str(row.get('toolContent') or ''))
            except (TypeError,ValueError):pass
        recognized=isinstance(payload,dict) and any(key in payload for key in ('scientific_observations','scientific_outcome'))
        if not recognized and not captured:continue
        result['observation_count']+=1
        result['evidence_sum']=f"{(int(result['evidence_sum'],16)+int(receipt['result_digest'],16))%(1<<256):064x}"
        if not recognized:
            result['failure_count']+=1
            continue
        outcome=payload.get('scientific_outcome')
        if not executed or receipt['status']!='done' or not isinstance(outcome,str) or outcome not in result['outcomes'] or not all(verify_observations(payload,protocol).values()):
            result['failure_count']+=1
        else:result['outcomes'][outcome]+=1
    incomplete=task.get('_evolutionScientificCommandIncomplete')
    if callable(incomplete) and incomplete():
        result['observation_count']+=1
        result['failure_count']+=1
    return result


def merge_scientific_audits(first,second):
    """Combine immutable foreground and late background observations once."""
    from .contracts import EvolutionStateError
    if first['protocol_sha256'] != second['protocol_sha256']:
        raise EvolutionStateError('scientific observation scope changed')
    merged={**first,'observation_count':first['observation_count']+second['observation_count'],
        'failure_count':first['failure_count']+second['failure_count'],
        'evidence_sum':f"{(int(first['evidence_sum'],16)+int(second['evidence_sum'],16))%(1<<256):064x}",
        'outcomes':{key:first['outcomes'][key]+second['outcomes'][key] for key in first['outcomes']}}
    if first['audit_version'] != second['audit_version']:
        # Keep measured originals/counts, but mixed rules cannot certify validity.
        merged['audit_version'] = max(first['audit_version'], second['audit_version'])
        merged.pop('capture_policy_version', None)
    if first.get('capture_policy_version')!=1 or second.get('capture_policy_version')!=1:
        merged.pop('capture_policy_version',None)
    return merged


def scientific_audit_valid(audit):
    """No favorable-receipt selection across contradictory measurements."""
    from lib.research.observation_audit import AUDIT_VERSION
    from .contracts import contract_validator
    if (not contract_validator('evolution_run_v1.schema.json','scientific_audit').is_valid(audit)
            or audit.get('audit_version')!=AUDIT_VERSION or audit.get('capture_policy_version')!=1):
        return False
    classified=sum(audit['outcomes'].values())
    return bool(audit['observation_count'] and not audit['failure_count']
        and classified==audit['observation_count']
        and sum(value>0 for value in audit['outcomes'].values())==1)


def scientific_execution_instruction(protocol):
    if not protocol:return ''
    from lib.research.observation_audit import observation_contract, scientific_inference_instruction
    return ('Frozen scientific protocol: '+json.dumps(protocol,ensure_ascii=False)+'\n'
        'Return one complete JSON object on command stdout with scientific_observations '
        'matching the canonical contract and a matching scientific_outcome. For new measurements use '
        'observation format v2 with method_seeds and baseline_seeds identifying each value vector; '
        'pair by seed identity rather than assuming matching execution order. Use every frozen '
        'seed, recomputed paired statistics and partition digests. Logs go to stderr. Do not '
        'change the criterion after observing results. Reported consistency is not authenticity.\n'
        + scientific_inference_instruction(protocol)
        + 'Scientific observation contract: '+json.dumps(observation_contract(protocol),ensure_ascii=False)+'\n')


def scientific_outcome_met(audit, required):
    return not required or (scientific_audit_valid(audit) and audit['outcomes'].get(required,0)==audit['observation_count'])
