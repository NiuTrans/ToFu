"""Materialize declared Model inheritance inside the owner routing aggregate.

Entry points: inherited_model_fields and apply_inherited_model_fields.
The v2 Offering schema owns the two-field vocabulary. Legacy unmarked values
remain explicit overrides; fallback budgets never become measured limits.
"""
from copy import deepcopy

MODEL_METADATA_FIELDS = ('capabilities', 'context_window')
# Admission budget for an unbound ID-only listing, never a provider claim.
UNKNOWN_CONTEXT_ADMISSION_BUDGET = 32_768


def declared_context_window(row):
    """Accept only explicit positive catalog values, never model-name guesses."""
    for field in ('context_window', 'context_length'):
        value = row.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def discovery_context_budget(row):
    """Use a declared/inherited window, otherwise a bounded pending budget."""
    return declared_context_window(row) or UNKNOWN_CONTEXT_ADMISSION_BUDGET


def inherited_model_fields(offering):
    fields = offering.get('inherited_model_fields', [])
    if (not isinstance(fields, list) or len(fields) > 2
            or any(not isinstance(key, str) or key not in MODEL_METADATA_FIELDS for key in fields)
            or len(set(fields)) != len(fields)):
        from .domain import ModelRoutingError
        raise ModelRoutingError('invalid inherited Model fields', field='inherited_model_fields')
    return sorted(fields)


def apply_inherited_model_fields(offering, model):
    """Resolve marked fields only; explicit provider differences are untouched."""
    fields = inherited_model_fields(offering)
    if 'inherited_model_fields' in offering:
        offering['inherited_model_fields'] = fields
    if model is not None:
        for key in fields:
            offering[key] = deepcopy(model[key])
