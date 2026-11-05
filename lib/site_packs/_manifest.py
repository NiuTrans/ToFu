"""Site pack manifest parsing and validation (tofu.site-pack/v1).

Schema validation runs against contracts/site_pack_v1.schema.json; the
semantic rules below are the ones JSON Schema cannot express (cross-field
references into an operation's params, step/auth interdependencies, inline
schema budgets). Both layers fail closed: an invalid pack never reaches the
registry.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ._contract import schema_validator
from ._inputs import operation_input_schema, prepare_steps, walk_steps
from ._templates import template_params

#: Inline input/output schemas stay small enough to audit and to project into
#: model-facing tool schemas without a prompt budget surprise.
MAX_INLINE_SCHEMA_CHARS = 8192

class ManifestError(ValueError):
    """A pack file failed schema or semantic validation."""


@dataclass(frozen=True)
class PackAuth:
    login_url: str
    label: str = ''
    aliases: tuple[str, ...] = ()
    access_strategy: str = 'browser_first'
    risk_notice: str = ''
    risk_note_key: str = ''
    fields: tuple[dict, ...] = ()
    #: Auth-source catalog domain override; empty projects the pack's first
    #: domain. ``project=False`` keeps the section ensure_auth-only.
    domain: str = ''
    project: bool = True


@dataclass(frozen=True)
class PackOperation:
    name: str
    description: str
    access: str
    input_schema: dict
    output_schema: dict
    session: str
    window_mode: str
    timeout: int
    min_interval_s: int
    cooldown_s: int
    steps: tuple[dict, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PackManifest:
    id: str
    name: str
    version: int
    routing_hint: str
    domains: tuple[str, ...]
    aliases: tuple[str, ...]
    auth: PackAuth | None
    operations: tuple[PackOperation, ...]
    source_path: str = ''


def _check_semantics(raw: dict, *, source: str) -> None:
    auth = raw.get('auth')
    seen_ops: set[str] = set()
    for op in raw.get('operations') or []:
        name = op['name']
        if name in seen_ops:
            raise ManifestError(f'{source}: duplicate operation {name!r}')
        seen_ops.add(name)
        where = f'{source}: operation {name!r}'
        for key in ('input_schema', 'output_schema'):
            schema = op.get(key) or {'type': 'object'}
            if len(json.dumps(schema)) > MAX_INLINE_SCHEMA_CHARS:
                raise ManifestError(f'{where}: {key} exceeds the inline schema budget')
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                raise ManifestError(f'{where}: invalid {key}: {exc.message}') from exc
        params = set((op.get('input_schema') or {}).get('properties') or {})
        access = op.get('access') or 'read'
        steps = tuple(walk_steps(op.get('steps')))
        kinds = {step['do'] for step in steps}
        extracted_slots = {step['into'] for step in steps if step['do'] == 'extract'}
        if 'ensure_auth' in kinds and not (auth and auth.get('login_url')):
            raise ManifestError(f'{where}: ensure_auth requires the pack auth section')
        if 'click' in kinds and access != 'write':
            raise ManifestError(
                f'{where}: click is a mutation and requires access="write"')
        for step in steps:
            do = step.get('do')
            referenced: set[str] = set()
            if do in ('navigate', 'research'):
                referenced |= template_params(step.get('url') or '')
            elif do == 'fill':
                referenced |= template_params(step.get('value') or '')
            elif do == 'extract':
                referenced |= set(step.get('args_from_params') or [])
            for key in ('times_from_param', 'query_from_param',
                        'limit_from_param', 'max_chars_from_param',
                        'max_pages_from_param'):
                if step.get(key):
                    referenced.add(step[key])
            unknown = referenced - params
            if unknown:
                raise ManifestError(
                    f'{where}: step {do} references params not in input_schema: '
                    + ', '.join(sorted(unknown)))
        for step in steps:
            if step.get('do') != 'normalize':
                continue
            kind = step.get('kind')
            if kind == 'search_records':
                for key in ('query_from_param', 'limit_from_param', 'source_name'):
                    if not step.get(key):
                        raise ManifestError(
                            f'{where}: search_records normalize requires {key}')
            if kind == 'detail' and 'fields' in step and 'fields' not in extracted_slots:
                raise ManifestError(
                    f'{where}: detail fields require an extract into "fields" step')
            if kind == 'json_payload' and 'payload' not in extracted_slots and 'research' not in kinds:
                raise ManifestError(
                    f'{where}: json_payload normalize requires an extract '
                    'into "payload" or a research step')


def _parse_operation(op: dict) -> PackOperation:
    steps = prepare_steps(op['steps'])
    return PackOperation(
        name=op['name'],
        description=op['description'],
        access=op.get('access') or 'read',
        input_schema=operation_input_schema(op.get('input_schema') or {'type': 'object'}, steps),
        output_schema=op.get('output_schema') or {'type': 'object'},
        session=op.get('session') or 'ephemeral',
        window_mode=op.get('window_mode') or 'background',
        timeout=int(op.get('timeout') or 30),
        min_interval_s=int(op.get('min_interval_s') or 0),
        cooldown_s=int(op.get('cooldown_s') or 0),
        steps=steps,
    )


def parse_manifest(raw: dict, *, source: str) -> PackManifest:
    """Validate one pack document and return its typed manifest."""
    if not isinstance(raw, dict):
        raise ManifestError(f'{source}: pack file must be a JSON object')
    raw = deepcopy(raw)
    error = next(schema_validator().iter_errors(raw), None)
    if error is not None:
        raise ManifestError(f'{source}: {error.message}')
    _check_semantics(raw, source=source)
    auth_raw = raw.get('auth')
    auth = None
    if auth_raw:
        auth = PackAuth(
            login_url=auth_raw['login_url'],
            label=str(auth_raw.get('label') or ''),
            aliases=tuple(auth_raw.get('aliases') or ()),
            access_strategy=auth_raw.get('access_strategy') or 'browser_first',
            risk_notice=str(auth_raw.get('risk_notice') or ''),
            risk_note_key=str(auth_raw.get('risk_note_key') or ''),
            fields=tuple(dict(f) for f in auth_raw.get('fields') or ()),
            domain=str(auth_raw.get('domain') or ''),
            project=bool(auth_raw.get('project', True)),
        )
    operations = tuple(_parse_operation(op) for op in raw['operations'])
    return PackManifest(
        id=raw['id'],
        name=raw['name'],
        version=int(raw.get('version') or 1),
        routing_hint=str(raw.get('routing_hint') or ''),
        domains=tuple(raw['domains']),
        aliases=tuple(raw.get('aliases') or ()),
        auth=auth,
        operations=operations,
        source_path=source,
    )
