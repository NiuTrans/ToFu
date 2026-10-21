"""Unified result views and bounded audit; no execution, storage, or authority.

Entry points: result_policy, project_model_source, inspect_tool_round,
inspect_swarm_notification, inspect_result_document. Inputs are existing Turn
projections; canonical evidence is referenced, never reconstructed from display.
Dependencies: generated result surface contract and existing evidence envelope.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from xml.etree import ElementTree

from lib.tools.result_projection_json import dump_projection_json, load_projection_json
from lib.tools.result_surfaces_generated import CONTRACT

LIMITS = CONTRACT['limits']


def result_policy(tool_name):
    return dict(CONTRACT['toolPolicies'].get(tool_name, CONTRACT['defaultPolicy']))


def requires_canonical_budget(tool_name):
    """Some model projections require canonical retention in every wrapper mode."""
    return result_policy(tool_name).get('budgetFormat') == 'canonical'


def _text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def _report_text(value):
    """Keep parsed numeric tokens numeric; only actual report strings unwrap."""
    return value if type(value) is str else dump_projection_json(value)


def _agent_report(value, _depth=0):
    """Unwrap only a recognized native report; keep every producer field."""
    if not isinstance(value, dict) or _depth > LIMITS['jsonScanDepth']:
        return None
    reports = CONTRACT['reportFields']
    collection_keys = CONTRACT['reportCollectionFields']
    collections = {key: value[key] for key in collection_keys if isinstance(value.get(key), list)}
    if any(isinstance(row, dict) and any(type(row.get(k)) is str for k in reports)
           for rows in collections.values() for row in rows):
        # Mixed batches retain unknown/running/error entries rather than
        # omitting status or manufacturing successful final reports.
        lines = [f'{key}: {_report_text(item)}' for key, item in value.items()
                 if key not in collections]
        for key, rows in collections.items():
            if not rows:
                lines.append(f'{key}: []')
            for index, row in enumerate(rows):
                lines.extend((f'\n## {key}[{index}]', _agent_report(row, _depth + 1) or _report_text(row)))
        return '\n'.join(lines)
    if not any(type(value.get(key)) is str for key in reports):
        return None
    # The producer's outer status alone is not agent completion. Retain it,
    # agent_status, error and partial_output separately and without promotion.
    lines = [f'{key}: {_report_text(item)}' for key, item in value.items() if key not in reports]
    for key in reports:
        if key not in value:
            continue
        item = value[key]
        lines.extend((f'\n## {key}', _report_text(item)))
    return '\n'.join(lines)


def _spawn_receipt(source):
    """Native launch handle only: objectives remain in canonical/UI metadata."""
    try:
        value = load_projection_json(source)
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None
    if not isinstance(value, dict) or value.get('status') != 'async_launched':
        return None
    agents = value.get('agents')
    if (not isinstance(agents, list) or not agents
            or type(value.get('swarm_key')) is not str
            or not all(isinstance(a, dict) and type(a.get('id')) is str
                       and type(a.get('objective')) is str for a in agents)):
        return None
    projected = {**value, 'agents': [{k: v for k, v in a.items() if k != 'objective'}
                                   for a in agents]}
    # No broad recursive stripping: dedup diagnostics/unknown application fields
    # and every execution/authorization/continuation field survive unchanged.
    return dump_projection_json(projected)


def _send_message_receipt(source):
    """Compact legacy exact-run receipts only; stable assignment handles stay actionable."""
    from lib.tools.result_projection_json import JSONNumber
    try:
        value = load_projection_json(source)
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None
    policy = CONTRACT['sendMessageReceipt']

    # Stable sends can start ordinary children, and busy receipts pin the exact
    # assignment to inspect. Keep the complete producer JSON (including launch
    # handles) in the model view, not just in canonical/frontend retention.
    if isinstance(value, dict) and 'retained_agent_id' in value:
        return source
    if not isinstance(value, dict) or set(value) != set(policy['fields']):
        return None
    if (not all(type(value[key]) is str and value[key]
                for key in ('agent_id', 'swarm_run_id', 'message_id'))
            or value['kind'] not in ('reference', 'steer')
            or not isinstance(value['contentChars'], JSONNumber)
            or not re.fullmatch(r'[1-9][0-9]*', value['contentChars'])
            or value['execution_started'] is not False
            or type(value['state']) is not str
            or value['state'] not in policy['legacyStates']):
        return None
    return policy['outcomes'].get(value['state'])


def project_model_source(tool_name, source):
    """Optional lossless text view; the caller retains the original canonical JSON.

    Unknown tools, typed failures and unsupported shapes pass through unchanged.
    Formatting precedes the normal token budget, so it cannot evade overflow.
    """
    policy = result_policy(tool_name)['modelFormat']
    if policy == 'producer' or not isinstance(source, str):
        return source

    if policy == 'spawn_receipt':
        return _spawn_receipt(source) or source

    if policy == 'send_message_receipt':
        return _send_message_receipt(source) or source
    if policy == 'artifact_query':
        from lib.tools.artifact_result_projection import project_artifact_result
        return project_artifact_result(tool_name, source) or source
    if policy == 'gateway_results':
        from lib.tools.gateway_result_projection import project_gateway_result
        return project_gateway_result(tool_name, source) or source

    def project_one(text):
        try:
            value = load_projection_json(text)
            if isinstance(value, dict) and value.get('status') == 'error':
                return None
            return _agent_report(value)
        except (ValueError, TypeError, RecursionError, OverflowError):
            return None

    report = project_one(source)
    if report is not None:
        return report
    if policy == 'agent_report_sections':
        # A whole JSON document that did not qualify above remains opaque.
        # Its interior lines are not independent native poll reports: rewriting
        # them can destroy typed errors, unknown shapes or duplicate-key data.
        if source.lstrip().startswith(('{', '[')):
            return source
        # poll composes text headers and one-line native JSON outcomes. Only
        # recognized report objects change; task/command/status lines are exact.
        lines = []
        for line in source.splitlines(keepends=True):
            replacement = project_one(line) if line.lstrip().startswith('{') else None
            lines.append((replacement + ('\n' if line.endswith('\n') else ''))
                         if replacement is not None else line)
        return ''.join(lines)
    return source


def _field(row, path):
    current = row
    for key in path.split('.'):
        if not isinstance(current, Mapping) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _preview(value, limit):
    text = _text(value)
    safe = re.sub(r'data:[^,\s"\']{1,128};base64,[A-Za-z0-9+/=]+', '[inline media omitted]', text)
    view = {'text': safe[:limit], 'chars': len(text), 'truncated': len(safe) > limit}
    if safe != text:
        view['mediaOmitted'] = True
    return view


def audit_model_result(content):
    """Advisory findings, bounded traversal; JSON structure alone is not failure."""
    text = _text(content)
    metrics = {'format': 'text', 'chars': len(text), 'jsonKeys': 0,
               'emptyValues': 0, 'scanComplete': True}
    findings = []
    if len(text) > LIMITS['jsonScanChars']:
        metrics['scanComplete'] = False
        metrics['format'] = 'unscanned'
        return {'metrics': metrics, 'findings': findings}
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return {'metrics': metrics, 'findings': findings}
    metrics['format'] = 'json'
    stack = [(value, 0)]
    visited = 0
    escaped_report = False
    while stack:
        item, depth = stack.pop()
        visited += 1
        if visited > LIMITS['jsonScanNodes'] or depth > LIMITS['jsonScanDepth']:
            metrics['scanComplete'] = False
            break
        if isinstance(item, dict):
            metrics['jsonKeys'] += len(item)
            for key, child in item.items():
                if key in CONTRACT['reportFields'] and isinstance(child, str) and '\n' in child:
                    escaped_report = True
                if child is None or child == '' or child == [] or child == {}:
                    metrics['emptyValues'] += 1
                if visited + len(stack) < LIMITS['jsonScanNodes']:
                    stack.append((child, depth + 1))
                else:
                    metrics['scanComplete'] = False
        elif isinstance(item, list):
            available = max(0, LIMITS['jsonScanNodes'] - visited - len(stack))
            stack.extend((child, depth + 1) for child in item[:available])
            if len(item) > available:
                metrics['scanComplete'] = False
    if isinstance(value, dict) and value.get('contractVersion') == 'tofu.tool-result/v2':
        findings.append('internal_envelope_leak')
    if escaped_report:
        findings.append('escaped_report')
    if metrics['jsonKeys'] >= LIMITS['manyJsonFields']:
        findings.append('many_json_fields')
    if metrics['emptyValues'] >= LIMITS['manyEmptyValues']:
        findings.append('empty_json_fields')
    return {'metrics': metrics, 'findings': findings}


def inspect_tool_round(row, *, preview_chars=None):
    """Observe retained model and frontend inputs; never claim provider receipt."""
    limit = min(16384, max(1, int(preview_chars or LIMITS['diagnosticPreviewChars'])))
    sources = CONTRACT['sources']
    model_source, model_value = '', None
    blocks = row.get(sources['modelBlocks'])
    if isinstance(blocks, list):
        model_source, model_value = sources['modelBlocks'], blocks
    else:
        for path in sources['modelText']:
            found, value = _field(row, path)
            if found:
                model_source, model_value = path, value
                break
    frontend_source, frontend_value = '', None
    deferred = row.get(sources['deferred'])
    if isinstance(deferred, Mapping) and isinstance(deferred.get('preview'), str):
        frontend_source, frontend_value = sources['deferred'] + '.preview', deferred['preview']
    else:
        for path in sources['modelText']:
            found, value = _field(row, path)
            if found:
                frontend_source, frontend_value = path, value
                break
    name = str(row.get('toolName') or '')
    evidence = row.get(sources['evidence'])
    evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    audit = audit_model_result(model_value) if model_source else {
        'metrics': {'format': 'missing', 'scanComplete': False},
        'findings': ['missing_model_result']}
    excluded = {path.split('.')[0] for path in sources['modelText']} | {
        sources['modelBlocks'], sources['frontendMetadata'], sources['evidence'],
        sources['frontendArguments'], 'thinking'}
    sidecars = {key: value for key, value in row.items() if key not in excluded}
    return {
        'contractVersion': CONTRACT['contractVersion'],
        'kind': 'tool', 'toolName': name, 'toolCallId': row.get('toolCallId'),
        'roundNum': row.get('roundNum'), 'status': row.get('status'),
        'policy': result_policy(name),
        'model': {'availability': 'retained' if model_source else 'missing',
                  'source': model_source, **(_preview(model_value, limit) if model_source else {})},
        'frontend': {'availability': 'retained' if frontend_source else 'missing',
                     'source': frontend_source,
                     **(_preview(frontend_value, limit) if frontend_source else {}),
                     'metadata': _preview(row.get(sources['frontendMetadata'], []), limit),
                     'arguments': _preview(row.get(sources['frontendArguments'], {}), limit),
                     'roundSidecars': _preview(sidecars, limit),
                     'scope': 'renderer inputs; actual mounted HTML is browser-owned'},
        'canonical': {'availability': 'referenced' if evidence.get('artifactRef') else 'not-referenced',
                      'evidence': evidence,
                      'scope': 'retained evidence reference; no automatic artifact read'},
        'audit': audit,
    }


def inspect_swarm_notification(preview, *, preview_chars=None):
    limit = min(16384, max(1, int(preview_chars or LIMITS['diagnosticPreviewChars'])))
    text = str(preview.get('text') or '')
    contract = CONTRACT['notifications']['swarm']
    report, tag, artifact = None, '', ''
    # The historical sidecar may already be truncated. Never infer a complete
    # report or model-delivery acknowledgement from that preview.
    if len(text) <= LIMITS['jsonScanChars'] and '<!DOCTYPE' not in text:
        try:
            node = ElementTree.fromstring(text)
            if node.tag in {contract['container'], contract['legacyContainer']}:
                artifact = node.findtext(contract['logTag']) or ''
                for field in contract['reportTags']:
                    child = node.find(field)
                    if child is not None:
                        report, tag = child.text or '', field
                        break
        except ElementTree.ParseError:
            pass
    return {'contractVersion': CONTRACT['contractVersion'], 'kind': 'swarm_notification',
            'toolName': 'swarm-update', 'toolCallId': None, 'agentId': preview.get('agentId'),
            'model': {'availability': 'unverified', 'source': '_inboxInjects.previews.text',
                      **_preview(text, limit)},
            'frontend': {'availability': 'retained' if report is not None else 'missing',
                         'source': tag, **(_preview(report, limit) if report is not None else {})},
            'canonical': {'availability': 'log-reference' if artifact else 'not-referenced',
                          'logPath': artifact, 'scope': 'audit log is not the final report'},
            'audit': {'metrics': {'format': 'xml'},
                      'findings': [] if report is not None else ['missing_report_body']}}


def inspect_result_document(document, *, tool_call_id=None, max_results=None, preview_chars=None):
    """One Turn-native observation interface for live, historical and synthetic calls."""
    maximum = min(LIMITS['auditMaxResults'], max(1, int(max_results or LIMITS['auditMaxResults'])))
    records, total = [], 0
    for index, message in enumerate(document.get('messages') or []):
        if not isinstance(message, Mapping):
            continue
        for row in message.get('toolRounds') or []:
            if not isinstance(row, Mapping) or (tool_call_id and row.get('toolCallId') != tool_call_id):
                continue
            total += 1
            if len(records) < maximum:
                record = inspect_tool_round(row, preview_chars=preview_chars)
                record['turnIndex'] = index
                records.append(record)
        if tool_call_id:
            continue
        for injection in message.get('_inboxInjects') or []:
            if not isinstance(injection, Mapping):
                continue
            for preview in injection.get('previews') or []:
                if not isinstance(preview, Mapping):
                    continue
                total += 1
                if len(records) < maximum:
                    record = inspect_swarm_notification(preview, preview_chars=preview_chars)
                    record['turnIndex'] = index
                    records.append(record)
    return {'contractVersion': CONTRACT['contractVersion'], 'records': records,
            'totalRecords': total, 'truncated': total > len(records),
            'coverage': 'read turns only' if document.get('diagnostic_incomplete') or document.get('diagnostic_unread_count') else 'loaded transcript',
            'rules': CONTRACT['auditRules']}
