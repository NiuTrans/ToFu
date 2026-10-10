"""Read-only v2 route binding for the owned native process host.

Entry compile_native_route/run_native_route. Canonical candidate policy and
credential decoding remain their existing owners. No slot registration, network
probe, fallback, billing, model registration or persistence is performed here.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from lib.agent_core.native_process import (
    NativeProcessClient,
    RoundPolicy,
    ProgressPolicy, CheckpointPolicy,
    ToolCallback,
    TerminalPolicy,
    TextDelta,
    ApprovalPolicy,
)
from lib.identity import PrincipalContext
from lib.provider_headers import sanitise_extra_headers
from .dispatch_adapter import decode_credential_secret
from .domain import ModelRoutingError, NativeModelSelection, MAX_DOCUMENT_BYTES
from .inference import candidate_inference_metadata
from .repository import OwnerBoundary, RepositoryPort
from .routing import RoutePolicy, compile_candidates


class NativeRouteError(ModelRoutingError):
    def __init__(self, kind):
        super().__init__("Native routed execution refused", kind=kind)


@dataclass(frozen=True, slots=True)
class Result:
    event: dict
    route: dict


_ISSUED = object()


class Route:
    """Single-use owner-bound route carrier; secrets are excluded from repr."""

    __slots__ = ("_owner", "_public", "_private", "_lock")

    def __init__(self, owner, public, private, byte_limit, *, _proof=None):
        if _proof is not _ISSUED:
            raise NativeRouteError("native_route_not_compiled")
        self._owner = owner
        self._public = json.dumps(public, allow_nan=False, ensure_ascii=False).encode()
        self._private = json.dumps(
            private, allow_nan=False, ensure_ascii=False
        ).encode()
        if len(self._public) + len(self._private) > byte_limit:
            raise NativeRouteError("native_route_bytes")
        self._lock = threading.Lock()

    def __repr__(self):
        return f"NativeRoute(owner_user_id={self._owner})"

    def close(self):
        with self._lock:
            self._private = None

    def consume(self, principal: PrincipalContext):
        if principal.require_owner(context="native route") != self._owner:
            raise NativeRouteError("native_route_owner")
        with self._lock:
            if self._private is None:
                raise NativeRouteError("native_route_consumed")
            private = self._private
            self._private = None
        return json.loads(self._public), json.loads(private)


def compile_native_route(
    repository: RepositoryPort,
    boundary: OwnerBoundary,
    selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    byte_limit: int = MAX_DOCUMENT_BYTES,
) -> Route:
    if type(byte_limit) is not int or byte_limit <= 0:
        raise NativeRouteError("native_route_bytes")
    authority = repository.get(boundary)
    if authority.boundary != boundary:
        raise NativeRouteError("native_route_owner")
    candidates = compile_candidates(
        authority.document, selection, policy=policy, max_candidates=1
    )
    if not candidates:
        raise NativeRouteError("native_route_unavailable")
    chosen = candidates[0]
    if chosen.connection["protocol"] not in {"openai", "local", "anthropic", "openai_responses"}:
        raise NativeRouteError("native_route_protocol_unsupported")
    if chosen.credential["kind"] not in {"api_key", "local_identity"}:
        raise NativeRouteError("native_route_credential_unsupported")
    base = chosen.connection["base_url"]
    try:
        url = urlsplit(base)
        valid = (
            url.scheme in {"http", "https"}
            and url.hostname
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
        )
        _ = url.port
    except ValueError:
        valid = False
    if not valid:
        raise NativeRouteError("native_route_endpoint")
    headers, error = sanitise_extra_headers(chosen.connection.get("extra_headers"))
    if error:
        raise NativeRouteError("native_route_headers")
    api_key = ""
    reference = chosen.credential["secret_reference"]
    if reference:
        secret = repository.resolve_secret(boundary, reference)
        api_key, oauth, secret_headers = decode_credential_secret(
            secret, kind=chosen.credential["kind"]
        )
        if oauth:
            raise NativeRouteError("native_route_credential_unsupported")
        if (
            chosen.credential["kind"] == "api_key"
            and not api_key
            and not secret_headers
        ):
            raise NativeRouteError("native_route_credential_missing")
        headers.update(secret_headers)
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
    elif chosen.credential["kind"] == "api_key":
        raise NativeRouteError("native_route_credential_missing")
    private = {"endpoint": base.rstrip("/") + "/chat/completions", "headers": headers}
    if chosen.connection["protocol"] == "anthropic":
        from lib.llm.anthropic_outbound._url import anthropic_headers, anthropic_messages_url
        private = {"endpoint": anthropic_messages_url(base),
                   "headers": anthropic_headers(api_key, headers), "provider_protocol": "anthropic"}
    metadata = candidate_inference_metadata(chosen)
    if chosen.connection["protocol"] == "openai_responses":
        from lib.llm.responses_outbound._url import responses_url
        from lib.llm.route_features import route_feature
        private = {"endpoint": responses_url(base), "headers": headers,
                   "provider_protocol": "responses",
                   "responses_profile": metadata.get("responses_profile") or "compatible",
                   "responses_reasoning_replay": route_feature(
                       metadata["inference_profile"], "responses_reasoning_replay",
                       protocol="openai_responses") is True}

    metadata.update(
        owner_user_id=boundary.owner_user_id,
        base_url=base,
        protocol=chosen.connection["protocol"],
        revision=authority.revision,
        tenant_id=boundary.tenant_id,
        model_ref=(
            {
                "creator_id": chosen.model["creator_id"],
                "model_id": chosen.model["model_id"],
            }
            if chosen.model
            else None
        ),
        provider_access_id=chosen.provider_access["provider_access_id"],
        provider_id=chosen.provider_id,
        offering_id=chosen.offering["offering_id"],
        deployment_id=chosen.deployment["deployment_id"],
        connection_id=chosen.connection["connection_id"],
        credential_id=chosen.credential["credential_id"],
        logical_model=chosen.model["model_id"]
        if chosen.model
        else chosen.offering["pending_model_id"],
        wire_model=chosen.deployment["wire_model_id"],
        max_output_tokens=chosen.deployment.get("max_output_tokens") or 0,
    )
    return Route(
        boundary.owner_user_id,
        metadata,
        private,
        byte_limit,
        _proof=_ISSUED,
    )


async def run_native_route(
    client: NativeProcessClient,
    route: Route,
    configuration: Mapping[str, Any],
    *,
    prepare_template: Callable[[dict], Mapping[str, Any]],
    callbacks: Mapping[str, ToolCallback],
    working_directory: Path,
    round_policy: RoundPolicy | None = None,
    progress_policy: ProgressPolicy | None = None,
    checkpoint_policy: CheckpointPolicy | None = None,
    terminal_policy: TerminalPolicy | None = None,
    on_text: Callable[[TextDelta], Any] | None = None,
    approval_policy: ApprovalPolicy | None = None,
    bind_round_policy: Callable[[dict, dict, dict], None] | None = None,
    model_policy=None,
) -> Result:
    """Bind trusted prepared generation parameters; Rust owns the model loop.

    prepare_template must run the selected route's existing inference/body policy.
    Messages/catalog are already owned by configuration; they cannot be replaced
    through this route port. Proxy selection remains an explicit caller concern.
    """
    if {"endpoint", "headers", "template", "provider_protocol", "responses_reasoning_replay", "responses_profile"} & configuration.keys():
        raise NativeRouteError("native_route_override")
    owner = client.principal.require_owner(context="native routed host")
    if (
        type(configuration.get("owner_user_id")) is not int
        or configuration["owner_user_id"] != owner
    ):
        raise NativeRouteError("native_route_owner")
    metadata, private = route.consume(client.principal)
    try:
        template = dict(prepare_template(json.loads(json.dumps(metadata))))
        if (
            template.get("model") != metadata["wire_model"]
            or {"messages", "tools"} & template.keys()
            or any(not isinstance(key, str) or key.startswith("_") for key in template)
        ):
            raise NativeRouteError("native_route_template")
        fields = [
            key for key in ("max_tokens", "max_completion_tokens") if key in template
        ]
        if (
            len(fields) != 1
            or type(template[fields[0]]) is not int
            or template[fields[0]] <= 0
        ):
            raise NativeRouteError("native_route_output_limit")
        cap = metadata["max_output_tokens"]
        if cap:
            template[fields[0]] = min(template[fields[0]], cap)
        if bind_round_policy is not None:
            binding = bind_round_policy(json.loads(json.dumps(metadata)),
                json.loads(json.dumps(template)), json.loads(json.dumps(configuration.get('tools') or {})))
            if binding is not None:
                raise NativeRouteError('native_route_policy_binding')
        config = dict(configuration, **private, template=template)
        event = await client.run(
            config,
            callbacks=callbacks,
            working_directory=working_directory,
            round_policy=round_policy, progress_policy=progress_policy, checkpoint_policy=checkpoint_policy, model_policy=model_policy,
            terminal_policy=terminal_policy,
            on_text=on_text,
            approval_policy=approval_policy,
        )
        return Result(event, metadata)
    finally:
        private.get("headers", {}).clear()
        private.clear()
