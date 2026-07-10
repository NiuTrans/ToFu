"""lib/agent_core/personal_scope.py — the personal-vs-headless capability seam.

> **Single source of truth for "which cfg capabilities are app-level personal
> state and therefore MUST be opt-in (never default-on) on the headless API".**

Why this module exists
-----------------------
Tofu is two products sharing one orchestrator:

* the **interactive Tofu app** (the chat UI), where a single owner's personal
  state — their accumulated *memory store*, their rolling *preference profile* —
  is exactly what makes the assistant feel personal, and should be ON by
  default; and
* the **headless agent runtime** (``/api/v1/agent/run``,
  ``/api/v1/chat/completions``, the OpenAI/Anthropic compat surfaces, the
  in-process ``tofu.chat`` facade), where the caller brings their own model and
  prompt and the server is a stateless executor. On a shared / multi-tenant
  deployment, silently injecting the *operator's* memories or preference file
  into an unrelated API caller's prompt is both a hallucination vector ("why
  does the model think I like terse Chinese answers?") and a privacy / isolation
  leak.

The trap this prevents
----------------------
Defaults are sticky and invisible. ``memoryEnabled`` defaulted to ``True`` for
the UI (``lib/conv_config.py``), and every headless cfg-builder inherited that
default unless it remembered to override it — and the *preference profile* rode
``memoryEnabled``, so a caller who legitimately enabled the memory store for an
accumulating agent ALSO got the operator's personal ``USER.md`` spliced in.
Each new personal feature added one more thing every headless surface had to
remember to turn off — the same "add it in 5 places, forget one, leak it"
failure mode the ``lib/agent_artifacts.py`` registry was created to kill.

The mechanism
-------------
1. Register every app-level personal capability ONCE in
   :data:`PERSONAL_CAPABILITIES` (cfg key + headless default + UI default + a
   human description + the prompt block it gates).
2. Every headless cfg-builder calls :func:`apply_headless_personal_defaults`
   exactly once, right after it has merged the caller's explicit cfg. The
   function is ``setdefault``-based, so an explicit caller value ALWAYS wins —
   it only fills the gap a caller left, with the *fail-closed* headless default
   instead of the UI default.
3. The prompt-assembly side (``lib/tasks_pkg/system_context.py``) reads the SAME
   flags to decide whether to inject (and therefore *describe*) each capability,
   so a capability that wasn't provided is never advertised to the model.

Adding a new personal capability later = ONE entry here + gate its injection on
the flag in ``system_context.py``. Every headless surface inherits the
fail-closed default automatically; the ratchet test
(``tests/test_personal_scope_headless.py``) fails if a new entry is added
without the headless surfaces honouring it.

Design contract
---------------
* **Additive & non-breaking for the UI.** The UI builder
  (``resolve_conv_config``) does NOT call this — it keeps its own UI defaults,
  so the interactive product is byte-identical.
* **Explicit caller cfg always wins.** Opt-in is ``config.memory=true`` /
  ``config.preferences=true`` (aliases) or the raw ``memoryEnabled`` /
  ``preferencesEnabled`` keys.
* **camelCase cfg keys** — same wire vocabulary as the rest of cfg.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'PersonalCapability',
    'PERSONAL_CAPABILITIES',
    'PERSONAL_CFG_KEYS',
    'apply_headless_personal_defaults',
    'resolve_preferences_enabled',
    'resolve_paper_insight_personal_context',
]


@dataclass(frozen=True)
class PersonalCapability:
    """One app-level personal capability gated at the headless boundary.

    Attributes:
        cfg_key:          The camelCase cfg flag (e.g. ``'memoryEnabled'``).
        headless_default: Value forced (via setdefault) on headless surfaces
            when the caller did not set ``cfg_key``. Always fail-closed.
        ui_default:       The interactive-app default (documentation only — the
            UI builder owns its own defaults; this records intent + lets the
            capabilities endpoint explain the asymmetry).
        summary:          One-line human description.
        prompt_block:     The marker / name of the prompt section this flag
            gates in ``system_context.py`` (documentation + test cross-check).
    """

    cfg_key: str
    headless_default: bool
    ui_default: bool
    summary: str
    prompt_block: str


# ── The registry — the ONLY place app-personal capabilities are declared ──
PERSONAL_CAPABILITIES: dict[str, PersonalCapability] = {
    'memoryEnabled': PersonalCapability(
        cfg_key='memoryEnabled',
        headless_default=False,
        ui_default=True,
        summary=(
            'Proactive injection of the server-side memory store '
            '(memory-count hint + <memory_accumulation> instructions). The '
            'search_memories / create_memory TOOLS remain available whenever '
            'tools are on — this flag only controls the proactive prompt '
            'plumbing, never tool availability.'),
        prompt_block='<memory_accumulation>'),
    'preferencesEnabled': PersonalCapability(
        cfg_key='preferencesEnabled',
        headless_default=False,
        ui_default=True,
        summary=(
            "Injection of the operator's rolling personal-preference profile "
            '(the global <data>/memories/.tofu_user_profile.md). Decoupled '
            'from memoryEnabled so enabling the memory store on the API does '
            'NOT splice the operator\'s personal preferences into an '
            'unrelated caller\'s prompt.'),
        prompt_block='[USER PREFERENCE PROFILE]'),
    'langCorrectionEnabled': PersonalCapability(
        cfg_key='langCorrectionEnabled',
        headless_default=False,
        ui_default=True,
        summary=(
            'The LLM-correction tier of the input language detector '
            '(lib/text_lang.detect_language). When on, an AMBIGUOUS statistical '
            'detection (low confidence / very short / thin English-vs-Latin '
            'margin) is escalated to a cheap-tier LLM call — the "typeless" '
            'corrector. Unlike memory/preferences this injects no operator '
            'state into the prompt; it is registered here because it can '
            'SILENTLY BILL an LLM call, so it must fail closed on headless '
            'surfaces unless the caller opts in.'),
        prompt_block=''),
    'paperInsightPersonalContext': PersonalCapability(
        cfg_key='paperInsightPersonalContext',
        headless_default=False,
        ui_default=True,
        summary=(
            "The Paper Reading-Mode insight second-pass injects a 'reader "
            "context' block — the OPERATOR's paper library (prior reports) + "
            'relevant entries from their memory store — so it can build '
            '"this connects to «a paper you already read»" transfer bridges. '
            "That is app-personal reading history + memories; on a headless / "
            'BYO surface it would splice one operator\'s library into an '
            "unrelated caller's paper analysis (privacy leak + hallucination "
            'vector). Fail-closed: the insight pass still runs on headless, but '
            'WITHOUT the personal reader-context block unless the caller opts '
            'in. Not a prompt-assembly block (its own engine gates it), so '
            'prompt_block is empty.'),
        prompt_block=''),
}

#: Convenience: the set of cfg keys the registry governs.
PERSONAL_CFG_KEYS: frozenset[str] = frozenset(PERSONAL_CAPABILITIES)


def apply_headless_personal_defaults(cfg: dict) -> dict:
    """Force the fail-closed headless default for every personal capability.

    Call ONCE per headless cfg-builder, AFTER merging the caller's explicit
    cfg. Uses ``setdefault`` so an explicit caller value (opt-in) always wins;
    only gaps are filled, with the registry's fail-closed ``headless_default``.

    Mutates *cfg* in place and returns it (for chaining).
    """
    filled = []
    for cap in PERSONAL_CAPABILITIES.values():
        if cap.cfg_key not in cfg:
            cfg[cap.cfg_key] = cap.headless_default
            filled.append(cap.cfg_key)
    if filled:
        logger.debug('[PersonalScope] headless fail-closed defaults applied: %s',
                     ', '.join(filled))
    return cfg


def resolve_preferences_enabled(cfg: dict | None, *,
                                memory_enabled: bool) -> bool:
    """Decide whether the personal-preference profile may be injected.

    The preference profile is a DISTINCT personal capability from the memory
    store. Resolution rules:

      * If the cfg explicitly carries ``preferencesEnabled`` (set by a headless
        builder via the registry, or by an opt-in caller), honour it verbatim.
      * Otherwise (the interactive UI never sets it) fall back to
        ``memory_enabled`` — preserving the historical UI behaviour where the
        profile rode the Memory toggle.

    Args:
        cfg: The task config dict (``task['config']``); may be ``None``.
        memory_enabled: The already-resolved memory flag, used as the
            back-compat fallback.

    Returns:
        True when the preference profile may be injected this turn.
    """
    if isinstance(cfg, dict) and 'preferencesEnabled' in cfg:
        return bool(cfg['preferencesEnabled'])
    return bool(memory_enabled)


def resolve_paper_insight_personal_context(cfg: dict | None) -> bool:
    """May the paper-insight pass inject the operator's personal reader-context?

    The reader-context block (operator paper library + memory store) is
    app-personal state. Resolution:

      * Explicit ``paperInsightPersonalContext`` in cfg (set by a headless
        builder via the registry → fail-closed False, or an opt-in caller) is
        honoured verbatim.
      * Absent (the interactive UI report path never sets it) → default TRUE,
        preserving the interactive owner's transfer moat.

    The KEY invariant: every headless cfg-builder runs
    ``apply_headless_personal_defaults``, which STAMPS this key to False, so a
    headless/BYO caller lands on ``False`` here unless they opted in. The
    interactive report route passes no cfg (or a cfg without this key) → True.
    """
    if isinstance(cfg, dict) and 'paperInsightPersonalContext' in cfg:
        return bool(cfg['paperInsightPersonalContext'])
    return True
