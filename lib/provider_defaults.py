"""Provider-defaults builder + pricing-tier tag re-evaluation.

Pure, framework-agnostic logic moved out of ``routes/config.py`` (2026-06):
builds the default provider/model config from environment-driven ``lib.*``
constants, and re-applies managed pricing-tier capability tags. No Flask
dependency — ``routes/config.py`` re-exports these for back-compat.
"""

from lib.log import get_logger

logger = get_logger(__name__)


def reeval_cheap_tags(providers: list):
    """Re-evaluate pricing-tier capability tags on all provider models.

    Delegates to :func:`lib.llm_dispatch.config.reevaluate_pricing_tags`,
    which is driven by the PRICING_TIERS table (currently just 'cheap' —
    input < $3/1M AND output < $15/1M, strict).  New tiers added to that
    table are auto-applied here with no further code changes.

    The legacy name ``_reeval_cheap_tags`` is retained for continuity
    with existing call sites; it now covers every managed tier tag.
    """
    from lib.llm_dispatch.config import reevaluate_pricing_tags

    for prov in providers:
        models = prov.get('models') or []
        if not models:
            continue
        reevaluate_pricing_tags(models, log_prefix='provider=%s' % prov.get('id', '?'))


def build_default_providers():
    """Build default provider config from environment/hardcoded values."""
    import lib as _lib
    from lib.llm_dispatch.config import (
        DEFAULT_SLOT_CONFIGS,
        MODEL_ALIAS_GROUPS,
        MANAGED_TIER_TAGS,
        get_pricing_tiers,
    )

    base_url = getattr(_lib, 'LLM_BASE_URL', '')
    api_keys = list(getattr(_lib, 'LLM_API_KEYS', []))

    def _auto_cheap(model_id, caps_set, cost):
        # Apply every managed pricing-tier tag (cheap, plus any future tier).
        if 'image_gen' in caps_set or 'embedding' in caps_set:
            return caps_set
        tiers = get_pricing_tiers(model_id, fallback_cost_per_1k=cost)
        # Drop any stale managed tier tag not in the current desired set.
        caps_set -= (MANAGED_TIER_TAGS - tiers)
        caps_set |= tiers
        return caps_set

    def _build_chat_model_entry(model_id, think_default):
        slot_cfg = DEFAULT_SLOT_CONFIGS.get(model_id, {})
        caps_set = _auto_cheap(model_id, set(slot_cfg.get('caps', {'text'})), slot_cfg.get('cost', 0.01))
        aliases = []
        for group in MODEL_ALIAS_GROUPS:
            if model_id in group:
                aliases = sorted(a for a in group if a != model_id)
                break
        return {
            'model_id': model_id, 'aliases': aliases, 'capabilities': sorted(caps_set),
            'rpm': slot_cfg.get('rpm', 30), 'cost': slot_cfg.get('cost', 0.01),
            'thinking_default': think_default,
        }

    preset_model_keys = [
        ('opus', 'LLM_MODEL', True), ('qwen', 'QWEN_MODEL', True),
        ('gemini', 'GEMINI_MODEL', True), ('gemini_flash', 'GEMINI_FLASH_PREVIEW_MODEL', True),
        ('doubao', 'DOUBAO_MODEL', True), ('minimax', 'MINIMAX_MODEL', True),
    ]
    seen_model_ids = set()
    models = []
    presets = {}
    for preset_key, env_key, think_default in preset_model_keys:
        model_id = getattr(_lib, env_key, '')
        if not model_id:
            continue
        if preset_key != 'opus':
            presets[preset_key] = model_id
        if model_id in seen_model_ids:
            continue
        seen_model_ids.add(model_id)
        models.append(_build_chat_model_entry(model_id, think_default))

    extra_model_keys = [
        ('GEMINI_PRO_MODEL', True),
        ('GEMINI_PRO_PREVIEW_MODEL', True),
        ('CLAUDE_SONNET_MODEL', True),
    ]
    for env_key, think_default in extra_model_keys:
        model_id = getattr(_lib, env_key, '')
        if not model_id or model_id in seen_model_ids:
            continue
        seen_model_ids.add(model_id)
        models.append(_build_chat_model_entry(model_id, think_default))

    image_gen_id = getattr(_lib, 'IMAGE_GEN_MODEL', '')
    if image_gen_id and image_gen_id not in seen_model_ids:
        seen_model_ids.add(image_gen_id)
        slot_cfg = DEFAULT_SLOT_CONFIGS.get(image_gen_id, {})
        models.append({
            'model_id': image_gen_id, 'aliases': [],
            'capabilities': sorted(slot_cfg.get('caps', {'image_gen'})),
            'rpm': slot_cfg.get('rpm', 10),
            'cost': slot_cfg.get('cost', 0.015),
            'thinking_default': False,
        })

    for emb_id in getattr(_lib, 'EMBEDDING_MODELS', []):
        if emb_id in seen_model_ids:
            continue
        seen_model_ids.add(emb_id)
        from lib.embeddings import AVAILABLE_EMBEDDING_MODELS
        emb_info = AVAILABLE_EMBEDDING_MODELS.get(emb_id, {})
        models.append({
            'model_id': emb_id, 'aliases': [],
            'capabilities': ['embedding'],
            'rpm': emb_info.get('max_rpm', 60),
            'cost': 0.001,
            'thinking_default': False,
        })

    return [{'id': 'default', 'name': 'Default', 'base_url': base_url,
             'api_keys': api_keys, 'enabled': True, 'models': models}], presets


__all__ = ['build_default_providers', 'reeval_cheap_tags']
