"""Stateless helpers around the Creator/Model registry.

The registry state authority is model-routing v2 (``lib/model_routing``);
this package owns no persisted state. What remains here:

- ``_creator_identity`` / ``_creator_families`` — pure identity helpers
  (canonical keys, merge keys, family prefixes) used by model-routing
  migration, domain normalization, routing, and world-catalogue sync.
- ``_score_matching`` — name-matching helpers shared by the enrichers.
- ``aa`` / ``openrouter`` — third-party enrichment feeds (Artificial
  Analysis scores, OpenRouter catalogue/pricing) consumed by
  ``routes/api_v1/model_intelligence.py``.
"""

from . import _creator_identity


__all__ = ['_creator_identity']
