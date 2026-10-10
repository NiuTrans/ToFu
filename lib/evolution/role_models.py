"""Goal role model overrides, using the Chat owner model catalog.

Entry points: selected_model, validate_role_models, update_role_model.
Only model references persist. CAS updates affect future task launches; an
already running task keeps its pinned route and original receipt identity.
"""
from .contracts import EvolutionStateError
from .workspace import model_options


def selected_model(run, stage):
    role = {'executing': 'executor', 'reviewing': 'reviewer'}.get(stage, stage)
    return (run.get('role_models') or {}).get(role) or run.get('model')


def validate_role_models(models, *, user_id):
    if not models:
        return {}
    choices = [row['model'] for row in model_options(user_id=user_id)]
    if any(model not in choices for model in models.values()):
        raise EvolutionStateError('Choose an available model from Chat settings')
    return {role: dict(model) for role, model in models.items()}


def update_role_model(run_id, payload, *, user_id):
    from . import runs
    runs._validate(payload, 'role_model_update')
    run, version = runs._read(run_id, user_id)
    if run is None:
        raise EvolutionStateError('unknown goal for this owner')
    role, model = payload['role'], payload['model']
    if model is not None:
        validate_role_models({role: model}, user_id=user_id)
    overrides = dict(run.get('role_models') or {})
    if model is None:
        overrides.pop(role, None)
    else:
        overrides[role] = dict(model)
    if overrides == (run.get('role_models') or {}):
        return run
    run['role_models'] = overrides
    from .stage_launch import supersede_model_launch
    reference = supersede_model_launch(run, payload, user_id=user_id)
    if reference is not None:
        run['stage_launch'] = reference
    runs._save(run, version, user_id)
    return run
