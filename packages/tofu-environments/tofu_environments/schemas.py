"""Provider-neutral tool schemas shared by native compatibility and MCP adapters.

Only this module defines tool names and arguments. It imports no host code;
callers filter these declarations by trusted provider and actor availability.
"""
from __future__ import annotations
import json

def tool_schemas():
    from pathlib import Path
    spec_schema = json.loads(Path(__file__).with_name("spec.schema.json").read_text())
    spec_schema.pop('$schema', None)
    job_schema = json.loads(Path(__file__).with_name("job.schema.json").read_text())
    job_schema.pop("$schema", None)
    string = {"type": "string", "minLength": 1, "maxLength": 128}
    def function(name, description, properties, required):
        return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}}
    schemas = [function("environment_providers", "List installed environment providers without starting a daemon or authenticating.", {}, []),
        function("environment_catalog", "Read available base-image references, revisions, queue names and provider constraints before planning. Authentication is owner-bound; this creates no resource.",
                 {"provider": string, "project": string, "query": {"type": "string", "maxLength": 256}}, ["provider", "project"]),
        function("environment_plan", "Validate and seal a provider-neutral image/workspace plan. Returns exact resource and build scope; creates no external resource. Profiles support deploying Tofu or project development/training. Supply explicit CPU/memory and owned project.",
        {"provider": string, "request_id": string, "spec": spec_schema}, ["provider", "request_id", "spec"]),
        function("environment_apply", "Apply the next stage of an immutable environment plan under exact write approval. May build an image or allocate a workspace; use the plan returned by environment_plan. Repeated or uncertain writes are never blindly resubmitted.",
        {"operation_id": string, "plan_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
         "plan": {"type": "object", "required": ["provider", "spec", "evidence", "project_root", "scope"],
                  "properties": {"provider": string, "spec": spec_schema, "evidence": {"type": "object"},
                                 "project_root": {"type": "string"}, "scope": {"type": "string"}}, "additionalProperties": False}},
         ["operation_id", "plan_hash", "plan"]),
        function("environment_status", "Read image/workspace progress and reconcile uncertain results without creating resources. When next_action=apply, request the approved next stage. Ready requires provider-specific readiness evidence.",
        {"operation_id": string}, ["operation_id"]),
        function("compute_catalog", "Read actor-bound compute queues, resource capacity and provider option schemas without planning or allocating. Availability is a temporary observation, not admission.", {"provider": string}, ["provider"]),
        function("compute_plan", "Validate and seal a bounded batch job with an installed actor-bound compute provider. No resource is allocated. Source identity, command, resources and cost limits are explicit.",
                 {"provider": string, "request_id": string, "spec": job_schema}, ["provider", "request_id", "spec"]),
        function("compute_submit", "Submit the exact approved compute plan once. Uncertain results are reconciled through compute_status; never replay a submission with a new request ID.",
                 {"operation_id": string, "plan_hash": {"type":"string", "pattern":"^[a-f0-9]{64}$"},
                  "plan": {"type":"object", "additionalProperties":False,
                           "required":["scope","project_root","provider","spec","admission"],
                           "properties":{"scope":string,"project_root":{"type":"string"},"provider":string,"spec":job_schema,"admission":{"type":"object"}}}},
                 ["operation_id","plan_hash","plan"]),
        function("compute_status", "Read scoped batch job status or reconcile unknown submission without allocation. Success requires a proven zero exit code.",
                 {"operation_id":string}, ["operation_id"]) ]
    schemas.append(function('compute_wait', 'Register a durable compute dependency for the current Evolution executor. The existing run reaper observes it without model polling or resubmission and resumes the original goal after terminal status. No remote allocation or cancellation.', {'operation_id': string}, ['operation_id']))
    schemas.append(function('compute_collect', 'Collect declared outputs from an owned proven-successful job into a fresh private directory in the active project. Returns file sizes and SHA256 receipts; does not allocate compute or resubmit jobs.', {'operation_id': string}, ['operation_id']))
    submit_schema = next(row for row in schemas if row['function']['name'] == 'compute_submit')
    schemas.append(function('compute_cancel', 'Request cancellation of the exact approved owned job. An acknowledgement is not terminal proof; follow compute_status. Unknown stop outcomes are not replayed.',
                            submit_schema['function']['parameters']['properties'], ['operation_id','plan_hash','plan']))
    return schemas
