"""Name-based compatibility boundary for unconfigured direct library callers."""
from lib.model_info import (
    gemini_reasoning_effort,
    glm_line_version,
    glm_reasoning_effort,
    gpt_reasoning_effort,
    grok_reasoning_effort,
    is_claude,
    is_claude_opus_47,
    is_doubao,
    is_ernie,
    is_gemini,

    is_grok,
    is_glm,
    is_gpt5,
    is_kimi,
    is_kimi_k3,
    is_kimi_k27_code,
    is_longcat,
    is_minimax,
    is_muse_spark,
    is_qwen,
    kimi_k3_reasoning_effort,
    muse_spark_reasoning_effort,
)

from lib.log import get_logger
logger = get_logger(__name__)

def apply_legacy(body, model, thinking_format, thinking_enabled, temperature, _effort):
    clean_messages = body.get("messages", [])
    # Provider-specific thinking / temperature parameters
    _tf = thinking_format
    # ── Plugin dialect (tofu.providers entry point) ──
    # Resolves ONLY for custom keys a plugin registered; built-in formats
    # return None and fall through to the unchanged ladder below.  Placed as
    # the first branch so all downstream post-processing (tools / extra /
    # Claude guards) still runs identically.
    _plugin_dialect = None
    if _tf:
        from lib.llm_dispatch.provider_registry import get_dialect
        _plugin_dialect = get_dialect(_tf)
    if _plugin_dialect is None and _tf == 'enable_thinking':
        # GLM-5.2+ carries its own native thinking contract (effort rungs +
        # preserved thinking). A provider-level legacy ``enable_thinking`` —
        # e.g. the YourProvider template's Qwen-oriented default — is a dead field
        # on GLM-native gateways (live-verified 2026-08-14 on
        # your-llm-gateway.example.com: glm-5.3 ignores it, so a user-disabled model
        # still thinks and bills) and it would silently kill the effort knob
        # and clear_thinking. Deliberate engine declarations (plugin / none /
        # chat_template_kwargs) still win — they describe the engine, not a
        # model-family default.
        _gv = glm_line_version(model)
        if _gv is not None and _gv >= (5, 2):
            _tf = ''
        elif is_grok(model):
            # Same shadowing for xAI Grok: its only thinking knob is the
            # OpenAI-style ``reasoning_effort`` string (docs.x.ai,
            # 2026-08-12); the template's ``enable_thinking`` is a dead
            # field on the wire (live-probed 2026-09-08 on
            # your-llm-gateway.example.com: accepted, zero behavioral effect).
            _tf = ''
    if _plugin_dialect is not None:
        try:
            _plugin_dialect.apply_build(
                body, thinking_enabled=thinking_enabled,
                temperature=temperature, model=model, effort=_effort)
        except Exception as e:
            raise ValueError('reasoning plugin failed') from e
    elif _tf == 'none':
        # Engine declares it does not honor any thinking flag (e.g.
        # DeepSeek-Reasoner where thinking is on by definition, or
        # plain non-thinking endpoints). Send no thinking params; let
        # the engine default. ``temperature`` still flows through.
        body['temperature'] = temperature if temperature is not None else 1.0
    elif _tf == 'chat_template_kwargs':
        # sglang / vLLM self-hosted dual-mode models. Thinking is gated
        # by the Jinja chat template, exposed via the OpenAI-shim's
        # ``chat_template_kwargs`` extension. Top-level
        # ``enable_thinking`` is silently dropped by these engines.
        kw = body.get('chat_template_kwargs')
        if not isinstance(kw, dict):
            kw = {}
        kw['enable_thinking'] = bool(thinking_enabled)
        body['chat_template_kwargs'] = kw
        body['temperature'] = temperature if temperature is not None else 0.7
    elif _tf == 'reasoning_effort' or (not _tf and is_gemini(model)):
        # Gemini 3.x is a reasoning model. The only knob the OpenAI-compat
        # gateway forwards to Vertex's thinkingLevel is the OpenAI-style
        # ``reasoning_effort`` string (verified via usage.reasoning_tokens:
        # minimal≈0 → high≈1000+). The legacy top-level ``enable_thinking``
        # boolean and the nested ``thinking.thinking_level`` field are both
        # silently ignored on this path.  Gemini 3.x also recommends NOT
        # sending temperature/top_p/top_k, so we omit temperature here.
        body['reasoning_effort'] = gemini_reasoning_effort(_effort, thinking_enabled)
    elif _tf == 'enable_thinking' or (not _tf and (is_longcat(model) or is_qwen(model) or is_ernie(model))):
        body['enable_thinking'] = thinking_enabled
        if is_longcat(model):
            body['temperature'] = 1.0 if thinking_enabled else (temperature or 0.7)
        else:
            body['temperature'] = temperature or 0.7
    elif _tf == 'thinking_type' or (not _tf and is_doubao(model)):
        if thinking_enabled:
            body['thinking'] = {'type': 'enabled'}
        else:
            body['thinking'] = {'type': 'disabled'}
        body['temperature'] = temperature or 0.7
    elif not _tf and is_glm(model):
        # GLM-5.3 (2026-08-14) is a FORCED-thinking model: thinking.type
        # 'disabled' is an API ERROR on its native endpoint, so a depth of
        # 'off' degrades to the lightest legal rung (reasoning_effort='low')
        # instead of a 400 — same policy as Kimi K3, the other
        # always-thinks family.
        _glm_v = glm_line_version(model)
        _glm_forced = _glm_v is not None and _glm_v >= (5, 3)
        if thinking_enabled or _glm_forced:
            body['thinking'] = {'type': 'enabled'}
            body['temperature'] = 1.0
            if _glm_v is not None and _glm_v >= (5, 2):
                # Effort knob + preserved thinking ship with GLM-5.2+; older
                # GLM endpoints reject both fields.
                body['reasoning_effort'] = glm_reasoning_effort(
                    _effort, thinking_enabled, model)
                # Preserved thinking: GLM clears replayed reasoning_content
                # server-side by default (clear_thinking=true); opting out
                # keeps the trace in context — officially recommended for
                # tool/agent loops (better quality AND cache hits). Only
                # meaningful when the history actually carries some.
                if any(m.get('reasoning_content')
                       for m in clean_messages
                       if isinstance(m, dict) and m.get('role') == 'assistant'):
                    body['thinking']['clear_thinking'] = False
        else:
            body['thinking'] = {'type': 'disabled'}
            body['temperature'] = max(temperature, 0.01) if temperature else 0.7
    elif not _tf and is_kimi(model):
        if is_kimi_k3(model):
            # K3 contract (official quickstart + verified live against the
            # example-corp gateway 2026-07-24): top-level ``reasoning_effort``
            # (low/high/max, default max); K3 always thinks, so depth 'off'
            # degrades to 'low'. Temperature is FIXED at 1.0 — sending any
            # other value is HTTP 400 — so it must be omitted entirely.
            body['reasoning_effort'] = kimi_k3_reasoning_effort(
                _effort, thinking_enabled)
        elif is_kimi_k27_code(model):
            # K2.7 Code contract (platform.kimi.ai, 2026-09-03): thinking is
            # ALWAYS on — ``{'type': 'disabled'}`` is an API error, so depth
            # 'off' must OMIT the field entirely (server default = enabled).
            # ``keep: 'all'`` pins reasoning_content retention across
            # multi-turn replays. Sampling is pinned server-side; only the
            # fixed temperature=1.0 is legal.
            if thinking_enabled:
                body['thinking'] = {'type': 'enabled', 'keep': 'all'}
            else:
                body.pop('thinking', None)
            body['temperature'] = 1.0
        else:
            _is_k2_thinking = ('k2-thinking' in model.lower()
                               and 'turbo' not in model.lower())
            if _is_k2_thinking or thinking_enabled:
                body['thinking'] = {'type': 'enabled'}
                body['temperature'] = 1.0
            else:
                body['thinking'] = {'type': 'disabled'}
                body['temperature'] = 0.6
        body.pop('top_p', None)
        body.pop('presence_penalty', None)
        body.pop('frequency_penalty', None)
    elif not _tf and is_minimax(model):
        body['temperature'] = temperature or 0.7
        body['reasoning_split'] = True

    elif not _tf and is_muse_spark(model):
        # Meta Muse Spark is a mandatory-reasoning model driven by the
        # top-level ``reasoning_effort`` string: minimal/low/medium/high/
        # xhigh (default medium), NO none rung — depth 'off' degrades to
        # 'minimal', and 'max' is partner-preview only so it clamps to
        # xhigh (muse_spark_reasoning_effort owns the ladder). Sampling
        # params are not part of the verified contract (Kimi K3 / GPT-5
        # reasoning precedent), so temperature is omitted rather than
        # risk a 400 on the first call.
        body['reasoning_effort'] = muse_spark_reasoning_effort(
            _effort, thinking_enabled)
    elif not _tf and is_grok(model):
        # xAI Grok contract (docs.x.ai Reasoning page, 2026-08-12; Grok 4.6
        # page, 2026-09-02): reasoning is ALWAYS on — no disabled rung —
        # driven by the top-level ``reasoning_effort`` string
        # (low/medium/high(default)/xhigh; xhigh is grok-4.6+, older lines
        # treat it as high). grok_reasoning_effort owns the ladder. Depth
        # 'off' degrades to 'low'. presencePenalty / frequencyPenalty / stop
        # are hard errors on reasoning models — stripped post-merge below.
        # Live note (your-llm-gateway.example.com, 2026-09-08): the gateway accepts the
        # knob and bills reasoning_tokens, but does NOT forward the
        # summarized reasoning trace xAI streams as delta.reasoning_content.
        body['reasoning_effort'] = grok_reasoning_effort(
            _effort, thinking_enabled)
        body['temperature'] = temperature
    elif not _tf and is_gpt5(model):
        # OpenAI GPT-5 family is a reasoning model driven by the native
        # ``reasoning_effort`` string. Current Codex models use ``none`` when
        # disabled, add ``xhigh`` on 5.3+, and add ``max`` on 5.6+.
        # gpt_reasoning_effort owns that generation-aware mapping.
        # Temperature is omitted — GPT-5 reasoning ignores sampling params.
        body['reasoning_effort'] = gpt_reasoning_effort(
            _effort, thinking_enabled, model)
    elif not _tf and is_claude(model) and thinking_enabled:
        body['thinking'] = {'type': 'adaptive'}
        if is_claude_opus_47(model):
            body['thinking']['display'] = 'summarized'
        else:
            body['temperature'] = 1.0
        # Opus 4.7+ states every rung, including the product default medium:
        # Anthropic's API default is high, so omission is not equivalent.
        # The older-family omit-medium wire stays for compatibility only;
        # it is NOT a claim that the official pre-4.7 default is medium.
        # tests/llm/test_claude_effort_rung_parity.py pins builder/swap parity.
        _omit_medium = not is_claude_opus_47(model)
        if _effort and not (_omit_medium and _effort == 'medium'):
            if _effort == 'xhigh' and not is_claude_opus_47(model):
                logger.info('[build_body] effort=xhigh not supported on %s — '
                            'downgrading to high', model)
                _effort = 'high'
            elif _effort == 'ultra':
                # ``ultra`` is Tofu's legacy GPT-5.6 compatibility label;
                # the public GPT value is ``max`` and Claude also tops at max.
                # map to Claude's top rung (max) so the effort is honoured.
                logger.info('[build_body] effort=ultra has no Claude tier on '
                            '%s — mapping to max', model)
                _effort = 'max'
            body['effort'] = _effort
    elif not _tf and is_claude_opus_47(model):
        # ── Thinking OFF on the ADAPTIVE-thinking generation (Opus 4.7+) ──
        # Claude Opus 5 runs adaptive thinking ON BY DEFAULT: a body with NO
        # ``thinking`` key thinks anyway. Every earlier Claude defaulted OFF,
        # so "disabled" used to be expressible by omitting the key — correct
        # then, silently wrong now. The user picks depth=off, we send nothing,
        # and Opus 5 bills them for reasoning they declined.
        #
        # Live-measured on the example-corp gateway (yuju-claude-opus-5-evaDaily,
        # identical prompt, 4 samples, 2026-07-26):
        #   omit thinking       → median 2925 completion tokens (36.3s, 24.8s)
        #   {"type":"disabled"} → median 1518 completion tokens (19.4s, 19.6s)
        #
        # Gated on is_claude_opus_47 (the adaptive generation) rather than all
        # Claude: this is the documented disable form for 4.7+, and it was
        # verified a live no-op on 4.7 (omit [562,536,628] vs disabled
        # [462,535,601]), so it corrects 5+ without changing 4.7/4.8 outcomes.
        # Pre-4.7 Claude keeps the byte-identical omit wire — it already
        # defaults off, and those revisions were never verified to accept the
        # key, so adding one would be an unforced 400 risk.
        #
        # No ``effort`` is set here, and that is load-bearing: Anthropic
        # returns HTTP 400 for thinking=disabled combined with xhigh/max.
        body['thinking'] = {'type': 'disabled'}
    else:
        body['temperature'] = temperature


    if is_kimi_k3(model) or is_gpt5(model) or is_gemini(model) or is_muse_spark(model):
        body.pop('temperature', None)

    if is_claude_opus_47(model):
        for key in ("temperature", "top_p", "top_k"):
            body.pop(key, None)
    if is_grok(model):
        for key in ("presence_penalty", "frequency_penalty", "stop"):
            body.pop(key, None)
