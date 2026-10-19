"""Selected-route input safety, after body adaptation and before transport.

Task prompt admission owns proactive compaction and working-set policy. This
boundary only refuses input exceeding the selected provider's hard window; it
never truncates history or calls a provider. Opaque reasoning/signature payloads
are not text tokens: use the canonical counter, not serialized body byte size.
"""

from lib.llm.llm_errors import PromptTooLongError
from lib.token_counter.evidence import ADMITTED_INPUT_TOKENS_KEY


def enforce_body_input_limit(body, *, model, provider_id='', reuse_admission=False,
                             context_window=0):
    """Validate the final adapted prompt and retain its count for this attempt.

    A positive count and the caller's reuse hint do not prove content identity:
    messages, system text, or nested tool schemas may have changed in place.
    Always recount with the local backend, whose bounded content-digest cache
    reuses unchanged text safely. Provider usage anchors and network counters
    must not participate in this guard. ``reuse_admission`` remains accepted
    for callers, but cannot authorize skipping this final safety check.
    """
    from lib.tasks_pkg.compaction._tokens import resolve_model_context_limit

    if context_window:
        body['_route_context_window'] = int(context_window)
        body['_route_context_model'] = model
    window = (body.get('_route_context_window')
              if body.get('_route_context_model') == model else None)
    window = window or resolve_model_context_limit(model, provider_id)
    from lib.token_counter import count_tokens

    tokens = count_tokens(
        body.get('messages') or [], model=model,
        system=body.get('system'), tools=body.get('tools'),
        mode='tiktoken',
    )['tokens']
    if window and tokens > window:
        raise PromptTooLongError(
            'Provider prompt refused before transport: selected model input '
            f'exceeds its hard context window ({tokens:,} > {window:,} tokens).'
        )
    body[ADMITTED_INPUT_TOKENS_KEY] = tokens
    if body.get('max_tokens'):
        from lib.llm.body._clamp import _clamp_completion_to_context_window
        body['max_tokens'] = _clamp_completion_to_context_window(
            model, body.get('messages'), body['max_tokens'],
            provider_id=provider_id, context_window=window,
            precomputed_input_tokens=tokens)
    # Routing metadata is internal only; final ingress consumes then strips it.
    body['_admission_provider_id'] = str(provider_id or '')
    return tokens
