"""Read-only v2 route binding for the owned native process host.

Entry compile_native_route/run_native_route. Canonical candidate policy and
credential decoding remain their existing owners. No slot registration, network
probe, fallback, billing, model registration or persistence is performed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import hashlib
from contextlib import ExitStack
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
from lib.provider.provider_headers import sanitise_extra_headers
from .dispatch_adapter import decode_credential_secret, issued_route_identity
from .domain import ModelRoutingError, NativeModelSelection
from .inference import candidate_inference_metadata
from .repository import OwnerBoundary, RepositoryPort
from .routing import RoutePolicy, compile_candidates


ROUTE_OVERRIDE_FIELDS = frozenset({"endpoint", "headers", "template", "provider_protocol", "responses_reasoning_replay", "responses_profile", "oauth", "secret_reference", "_credential_resolver", "chat_preserve_text_parts"})


class NativeRouteError(ModelRoutingError):
    def __init__(self, kind):
        super().__init__("Native routed execution refused", kind=kind)


@dataclass(frozen=True, slots=True)
class Result:
    event: dict
    route: dict


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeCredentialRequest:
    """Selected credential/destination proof for a joined owner-aware resolver.

    The resolver must validate source ownership, revocation and destination
    before returning complete HTTP headers. This request grants no vault access.
    """
    principal: PrincipalContext
    task_id: str
    attempt_ordinal: int
    credential_id: str
    provider_id: str
    offering_id: str
    connection_id: str
    deployment_id: str
    secret_reference: str = field(repr=False)
    subscription: str
    endpoint: str
    wire_model: str
    cache_affinity_key: str
    extra_headers_json: bytes = field(repr=False)
    cancelled: threading.Event = field(repr=False)
    route_binding_sha256: str = ""

    @property
    def extra_headers(self):
        return json.loads(self.extra_headers_json)


def route_binding_sha256(metadata):
    """Freeze selected route semantics; unrelated aggregate revisions may advance."""
    raw=json.dumps({key:value for key,value in metadata.items() if key!='revision'},
        sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
    return hashlib.sha256(raw).hexdigest()


_ISSUED = object()


class Route:
    """Single-use owner-bound route carrier; secrets are excluded from repr."""

    __slots__ = ("_owner", "_public", "_private", "_lock", "_credential_resolver")

    def __init__(self, owner, public, private, byte_limit, *, _proof=None, credential_resolver=None):
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
        self._credential_resolver = credential_resolver

    def __repr__(self):
        return f"NativeRoute(owner_user_id={self._owner})"

    def close(self):
        with self._lock:
            self._private = None
            self._credential_resolver = None

    def consume(self, principal: PrincipalContext):
        if principal.require_owner(context="native route") != self._owner:
            raise NativeRouteError("native_route_owner")
        public = json.loads(self._public)
        if public.get('tenant_id', '') != (principal.tenant_id or ''):
            raise NativeRouteError('native_route_owner')
        with self._lock:
            if self._private is None:
                raise NativeRouteError("native_route_consumed")
            private = self._private
            self._private = None
            resolver = self._credential_resolver
            self._credential_resolver = None
        decoded = json.loads(private)
        if resolver is not None:
            decoded['_credential_resolver'] = resolver
        return public, decoded


def compile_native_route(
    repository: RepositoryPort,
    boundary: OwnerBoundary,
    selection: NativeModelSelection,
    *,
    policy: RoutePolicy | None = None,
    byte_limit: int = 8 * 1024 * 1024,
    credential_resolver=None,
    expected_route_identity: Mapping | None = None,
) -> Route:
    if type(byte_limit) is not int or byte_limit <= 0:
        raise NativeRouteError("native_route_bytes")
    authority = repository.get(boundary)
    if authority.boundary != boundary:
        raise NativeRouteError("native_route_owner")
    if expected_route_identity is not None and (
            not isinstance(expected_route_identity, Mapping)
            or expected_route_identity.get("revision") != authority.revision):
        raise NativeRouteError("native_route_changed")
    candidates = compile_candidates(
        authority.document, selection, policy=policy,
        max_candidates=None if expected_route_identity is not None else 1,
    )
    if expected_route_identity is not None:
        candidates = [candidate for candidate in candidates
            if issued_route_identity(candidate, boundary, authority.revision) == dict(expected_route_identity)]
        if not candidates:
            raise NativeRouteError("native_route_changed")
    if not candidates:
        raise NativeRouteError("native_route_unavailable")
    chosen = candidates[0]
    if chosen.connection["protocol"] not in {"openai", "local", "anthropic", "openai_responses"}:
        raise NativeRouteError("native_route_protocol_unsupported")
    oauth_selected = chosen.credential["kind"] == "oauth"
    metadata = candidate_inference_metadata(chosen)
    if (chosen.credential["kind"] not in {"api_key", "local_identity", "oauth"}
            or (oauth_selected and (not callable(credential_resolver)
                or chosen.connection["protocol"] != "openai_responses"
                or metadata.get("responses_profile") != "codex"))):
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
        if oauth and (not oauth_selected or oauth != 'codex' or api_key):
            raise NativeRouteError("native_route_credential_unsupported")
        if oauth_selected and oauth != 'codex':
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
    elif chosen.credential["kind"] in {"api_key", "oauth"}:
        raise NativeRouteError("native_route_credential_missing")
    private = {"endpoint": base.rstrip("/") + "/chat/completions", "headers": headers}
    if chosen.connection["protocol"] == "anthropic":
        from lib.llm.anthropic_outbound._url import anthropic_headers, anthropic_messages_url
        private = {"endpoint": anthropic_messages_url(base),
                   "headers": anthropic_headers(api_key, headers), "provider_protocol": "anthropic"}
    if chosen.connection["protocol"] == "openai_responses":
        from lib.llm.responses_outbound._url import responses_url
        from lib.llm.route_features import route_feature
        private = {"endpoint": responses_url(base), "headers": headers,
                   "provider_protocol": "responses",
                   "responses_profile": metadata.get("responses_profile") or "compatible",
                   "responses_reasoning_replay": route_feature(
                       metadata["inference_profile"], "responses_reasoning_replay",
                       protocol="openai_responses", oauth='codex' if oauth_selected else '') is True}

    if oauth_selected:
        private['oauth'] = 'codex'
        private['secret_reference'] = reference
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
        actual_pricing=chosen.offering.get('actual_pricing'),
        wire_model=chosen.deployment["wire_model_id"],
        max_output_tokens=chosen.deployment.get("max_output_tokens") or 0,
    )
    if chosen.connection['protocol'] == 'openai':
        from lib.llm.cache import _gateway_honors_cache_markers
        private['chat_preserve_text_parts'] = _gateway_honors_cache_markers(metadata['wire_model'])
    if oauth_selected:
        metadata['credential_kind'] = 'oauth'
    return Route(
        boundary.owner_user_id,
        metadata,
        private,
        byte_limit,
        _proof=_ISSUED,
        credential_resolver=credential_resolver if oauth_selected else None,
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
    stop_signal=None,
    prepare_model_counter=None,
    prepare_wire_tools=None,
) -> Result:
    """Bind trusted prepared generation parameters; Rust owns the model loop.

    prepare_template must run the selected route's existing inference/body policy.
    Messages/catalog are already owned by configuration; they cannot be replaced
    through this route port. Proxy selection remains an explicit caller concern.
    """
    if ROUTE_OVERRIDE_FIELDS & configuration.keys():
        raise NativeRouteError("native_route_override")
    owner = client.principal.require_owner(context="native routed host")
    if (
        type(configuration.get("owner_user_id")) is not int
        or configuration["owner_user_id"] != owner
    ):
        raise NativeRouteError("native_route_owner")
    metadata, private = route.consume(client.principal)
    scopes = None
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
        resolver = private.pop('_credential_resolver', None)
        subscription = private.pop('oauth', None)
        reference = private.pop('secret_reference', None)
        if subscription is not None:
            if not callable(resolver) or not callable(getattr(model_policy, 'bind_credentials', None)):
                raise NativeRouteError('native_route_credential_binding')
            scopes = ExitStack()
            cache_key = scopes.enter_context(model_policy.bind_credentials(
                principal=client.principal, task_id=configuration.get('task_id'), metadata=metadata,
                subscription=subscription, secret_reference=reference, endpoint=private['endpoint'],
                extra_headers=private['headers'], resolver=resolver))
            from lib.llm.route_features import route_feature
            if route_feature(metadata['inference_profile'], 'responses_prompt_cache',
                    protocol=metadata['protocol'], oauth=subscription) is True:
                if 'prompt_cache_key' in template and template['prompt_cache_key'] != cache_key:
                    raise NativeRouteError('native_route_cache_binding')
                template['prompt_cache_key'] = cache_key
            elif 'prompt_cache_key' in template:
                raise NativeRouteError('native_route_cache_binding')
        if getattr(model_policy,'pricing_resolver',None) is not None:
            if (configuration.get('model_control') or {}).get('pricing') is not True or not callable(getattr(model_policy,'bind_pricing',None)):
                raise NativeRouteError('native_route_pricing_binding')
            if scopes is None: scopes=ExitStack()
            scopes.enter_context(model_policy.bind_pricing(principal=client.principal,
                task_id=configuration.get('task_id'),metadata=metadata))
        bind_cost=getattr(round_policy,'bind_pricing_route',None)
        if callable(bind_cost): bind_cost(json.loads(json.dumps(metadata)))
        wire_tools = None
        if prepare_wire_tools is not None:
            if not callable(prepare_wire_tools) or 'wire_tools' in configuration:
                raise NativeRouteError('native_route_wire_binding')
            wire_tools = prepare_wire_tools(json.loads(json.dumps(metadata)),
                json.loads(json.dumps(template)), json.loads(json.dumps(configuration.get('tools') or {})))
            import inspect
            if inspect.isawaitable(wire_tools):
                wire_tools = await wire_tools
            if not isinstance(wire_tools,list):
                raise NativeRouteError('native_route_wire_binding')
        binding_template = dict(template, tools=wire_tools) if wire_tools is not None else template
        if bind_round_policy is not None:
            binding = bind_round_policy(json.loads(json.dumps(metadata)),
                json.loads(json.dumps(binding_template)), json.loads(json.dumps(configuration.get('tools') or {})))
            if binding is not None:
                raise NativeRouteError('native_route_policy_binding')
        config = dict(configuration, **private, template=template)
        if wire_tools is not None:
            config['wire_tools'] = wire_tools
        if prepare_model_counter is not None:
            if not callable(prepare_model_counter):
                raise NativeRouteError('native_route_counter_binding')
            config['model_counter'] = prepare_model_counter(
                json.loads(json.dumps(metadata)), json.loads(json.dumps(template)),
                json.loads(json.dumps(configuration.get('model_counter'))))
        event = await client.run(
            config,
            callbacks=callbacks,
            working_directory=working_directory,
            round_policy=round_policy, progress_policy=progress_policy, checkpoint_policy=checkpoint_policy, model_policy=model_policy,
            terminal_policy=terminal_policy,
            on_text=on_text,
            approval_policy=approval_policy,
            stop_signal=stop_signal,
        )
        return Result(event, metadata)
    finally:
        import sys
        primary = sys.exception()
        try:
            if scopes is not None:
                scopes.close()
        except BaseException as cleanup_error:
            if primary is None:
                raise
            primary.add_note('native credential cleanup failed: '+type(cleanup_error).__name__)
        finally:
            private.get("headers", {}).clear()
            private.clear()
