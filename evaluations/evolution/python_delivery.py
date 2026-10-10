"""Offline Python-delivery check for response envelopes; never executes code.

Entry: check_python_delivery. It checks reported completion, unambiguous JSON
and bounded Python compilation. It proves neither behavior nor experiment
validity; execution remains behind the production task/tool boundary.
"""
from hashlib import sha256
from .paired import _decode_json

CHECKER = 'python_delivery_compile_v1'
MAX_CODE_BYTES = 65536
MAX_ANSWER_BYTES = 262144


def check_python_delivery(response):
    result = {'checker':CHECKER,'outcome':'fail','executed':False}
    if not isinstance(response, dict) or response.get('status') not in ('done','completed') or response.get('error'):
        return {**result,'reason':'response_not_completed'}
    answer = response.get('content')
    if not isinstance(answer,str):
        return {**result,'reason':'answer_not_text'}
    try:
        encoded = answer.encode('utf-8')
    except UnicodeEncodeError:
        return {**result,'reason':'answer_not_utf8'}
    result['answer_sha256'] = sha256(encoded).hexdigest()
    if len(encoded) > MAX_ANSWER_BYTES:
        return {**result,'reason':'answer_exceeds_bound'}
    try:
        payload = _decode_json(answer)
    except (ValueError, TypeError):
        return {**result,'reason':'invalid_json'}
    if not isinstance(payload,dict) or set(payload) != {'python'} or not isinstance(payload['python'],str):
        return {**result,'reason':'invalid_python_delivery_shape'}
    source = payload['python']
    try:
        code_bytes = source.encode('utf-8')
    except UnicodeEncodeError:
        return {**result,'reason':'source_not_utf8'}
    result.update(source_sha256=sha256(code_bytes).hexdigest(),source_bytes=len(code_bytes))
    if not source.strip() or len(code_bytes) > MAX_CODE_BYTES:
        return {**result,'reason':'code_empty_or_exceeds_bound'}
    try:
        compile(source,'<delivered-python>','exec',dont_inherit=True)
    except SyntaxError as exc:
        return {**result,'reason':'syntax_error','line':exc.lineno,'offset':exc.offset}
    except (ValueError, RecursionError):
        return {**result,'reason':'compilation_refused'}
    return {**result,'outcome':'pass','reason':'compilation_only'}
