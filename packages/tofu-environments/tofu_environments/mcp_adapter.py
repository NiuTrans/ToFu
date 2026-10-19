"""Actor-bound MCP adapter over the existing environment and job services.

MCPBinding is created by trusted server bootstrap, never tool arguments. The
binding supplies the repository, provider resolver, exact-write authorizer and
cleanup. SDK services continue to own CAS, recovery, no-replay and outputs.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
import json
from pathlib import Path
from jsonschema import Draft202012Validator, ValidationError
from .contracts import Actor, EnvironmentError, canonical, seal
from .schemas import tool_schemas

EXTERNAL_WRITES = frozenset({'environment_apply', 'compute_submit', 'compute_cancel'})

@dataclass(frozen=True)
class MCPBinding:
    actor: Actor
    environment_service: object | None = None
    job_service: object | None = None
    authorize: Callable[[Actor, str, dict], bool] | None = None
    close: Callable[[], None] | None = None

    def schemas(self) -> list[dict]:
        rows = []
        for row in tool_schemas():
            name = row['function']['name']
            # Durable goal waiting belongs to the host, not the platform MCP.
            if name in {'environment_providers', 'compute_wait'}:
                continue
            if name.startswith('environment_') and self.environment_service is None:
                continue
            if name.startswith('compute_') and self.job_service is None:
                continue
            if name == 'compute_collect' and not self.actor.project_root:
                continue
            rows.append(row)
        return rows

    def invoke(self, name: str, arguments: dict) -> dict:
        schemas = {r['function']['name']: r['function'] for r in self.schemas()}
        if name not in schemas:
            raise EnvironmentError('tool_unavailable', 'Operation is not configured for this MCP binding')
        try:
            canonical(arguments)
            Draft202012Validator(schemas[name]['parameters']).validate(arguments)
        except ValidationError as exc:
            raise EnvironmentError('invalid_arguments', 'Arguments do not match the declared operation') from exc
        args = json.loads(canonical(arguments))
        service = self.job_service if name.startswith('compute_') else self.environment_service
        if name in EXTERNAL_WRITES:
            if seal(args['plan']) != args['plan_hash']:
                raise EnvironmentError('approval_scope_changed', 'Preview does not match the sealed plan')
            # The service independently checks the owned durable plan. The
            # trusted policy must consume a one-use grant for these exact args.
            approved = lambda actor, digest: (
                actor == self.actor and digest == args['plan_hash']
                and self.authorize is not None
                and self.authorize(actor, name, json.loads(canonical(args))) is True)
            if name == 'environment_apply':
                result = service.apply(self.actor, args['operation_id'], args['plan_hash'], approved,
                                       approved_plan=args['plan'])
            else:
                command = service.submit if name == 'compute_submit' else service.cancel
                result = command(self.actor, args['operation_id'], args['plan_hash'], approved)
        elif name.endswith('_catalog'):
            result = (service.catalog(self.actor, args['provider']) if name == 'compute_catalog'
                      else service.catalog(self.actor, args['provider'], args['project'], args.get('query', '')))
        elif name.endswith('_plan'):
            result = service.plan(self.actor, args['provider'], args['spec'], args['request_id'])
        elif name.endswith('_status'):
            result = service.status(self.actor, args['operation_id'])
        elif name == 'compute_collect':
            try:
                destination = Path(self.actor.project_root).resolve(strict=True)
            except OSError as exc:
                raise EnvironmentError('project_required', 'Bound project directory is unavailable') from exc
            if not destination.is_dir():
                raise EnvironmentError('project_required', 'Collection requires the bound project directory')
            result = service.collect(self.actor, args['operation_id'], destination)
        else:
            raise EnvironmentError('tool_unavailable', 'Operation is not configured')
        canonical(result)  # Same bounded finite JSON receipt as the SDK.
        return result
