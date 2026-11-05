"""Admit immutable local-model manifests through the common Model boundary.

Entry points: admit_local_model_artifacts for validation/export and
load_local_model_artifacts for the developer-owned catalog. Dependencies are
the existing model-routing normalizer/admission and the local JSON contracts.
No provider is created, no authority is written, and no model is downloaded or
loaded. Heavy schema dependencies load only when this optional catalog is used.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CATALOG = Path(__file__).with_name('local_model_artifacts.json')


def admit_local_model_artifacts(catalog: dict) -> dict:
    """Return detached, fully admitted model facts and exact artifact pins.

    A publishable artifact requires a ready Model. Incomplete facts raise at
    the same registration boundary used by repository writes; supplied
    disabled/draft states are never promoted by this exporter. General Model
    drafts remain supported by that common boundary, outside this catalog.
    """
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    from lib.model_routing.domain import ModelRef, empty_document, public_projection
    from lib.model_routing.registration import prepare_registration_document

    artifact_schema = json.loads(
        (_ROOT / 'contracts/local_model_artifacts_v1.schema.json').read_text())
    model_schema = json.loads(
        (_ROOT / 'contracts/model_routing_v2.schema.json').read_text())
    registry = Registry().with_resources([
        (artifact_schema['$id'], Resource.from_contents(artifact_schema)),
        (model_schema['$id'], Resource.from_contents(model_schema)),
    ])
    # A local registry resolves both contracts without network retrieval.
    Draft202012Validator(artifact_schema, registry=registry).validate(catalog)
    detached = deepcopy(catalog)
    document = empty_document()
    document['creators'] = detached['creators']
    models: dict[ModelRef, dict] = {}
    artifact_ids: set[str] = set()
    for artifact in detached['artifacts']:
        artifact_id = artifact['artifact_id']
        if artifact_id in artifact_ids:
            raise ValueError('duplicate local model artifact identifier')
        artifact_ids.add(artifact_id)
        source = artifact['source']
        names = [row['name'] for row in source['files']]
        if len(names) != len(set(names)):
            raise ValueError('duplicate local model artifact filename')
        if not any(name.endswith('.safetensors') for name in names):
            raise ValueError('local model artifact requires safetensors weights')
        model = artifact['model']
        if model.get('registration_state') not in (None, 'ready'):
            raise ValueError('local model export cannot promote a draft or disabled Model')
        model['registration_state'] = 'ready'
        ref = ModelRef.from_value(model)
        if ref in models and models[ref] != model:
            raise ValueError('local artifacts disagree about the same Model identity')
        models[ref] = model
    document['models'] = list(models.values())
    admitted = public_projection(prepare_registration_document(
        document, previous=empty_document()))
    ready = {ModelRef.from_value(row): row for row in admitted['models']}
    for artifact in detached['artifacts']:
        model = ready[ModelRef.from_value(artifact['model'])]
        if model.get('registration_state') != 'ready':
            raise ValueError('local model artifact has no admitted Model')
        if 'reranking' not in model['capabilities']:
            raise ValueError('qwen3_yes_no execution requires the reranking capability')
        if model['inference_profile']['reasoning']['support'] != 'unsupported':
            raise ValueError('qwen3_yes_no execution exposes no reasoning controls')
        artifact['model'] = deepcopy(model)
    return detached


def load_local_model_artifacts() -> dict:
    """Read and admit the public package catalog on explicit use only."""
    return admit_local_model_artifacts(json.loads(_CATALOG.read_text(encoding='utf-8')))
