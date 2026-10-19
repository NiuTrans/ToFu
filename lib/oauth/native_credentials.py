"""Owner/source-fenced production Codex credentials for issued native routes.

Entry NativeCodexCredentialResolver: revalidate selected v2 grant, resolve through
existing outbound/refresh authority, then revalidate grant/source before returning.
No vault, cache, executor, route fallback or persistence implementation is added.
"""
from urllib.parse import urlsplit
import json
import hashlib

from lib.identity import PrincipalContext
from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
from lib.model_routing.native_route import NativeCredentialRequest, NativeRouteError
from lib.model_routing.repository import OwnerBoundary


def _unavailable(reason):
    return SubscriptionCredentialUnavailableError(reason=reason,credential_source='tofu_oauth',transient=reason=='credential_source_unavailable')


class NativeCodexCredentialResolver:
    __slots__=('repository','principal','boundary','_source_binding')

    def __init__(self, *, repository, principal: PrincipalContext):
        self.repository,self.principal=repository,principal
        self._source_binding=None
        self.boundary=OwnerBoundary.create(principal.require_owner(context='native OAuth resolver'),principal.tenant_id)

    def _authorize(self, request):
        from lib.model_routing.domain import NativeModelSelection, ProviderOfferingRef
        from lib.model_routing.native_route import compile_native_route, route_binding_sha256
        if (not isinstance(request,NativeCredentialRequest)
                or request.principal.require_owner(context='native OAuth attempt')!=self.boundary.owner_user_id
                or (request.principal.tenant_id or '')!=self.boundary.tenant_id or request.subscription!='codex'):
            raise NativeRouteError('native_route_owner')
        if request.cancelled.is_set():
            import asyncio
            raise asyncio.CancelledError
        route=compile_native_route(self.repository,self.boundary,NativeModelSelection(None,
            ProviderOfferingRef(request.provider_id,request.offering_id)),credential_resolver=self)
        try:
            metadata,private=route.consume(request.principal)
            try:
                if (route_binding_sha256(metadata)!=request.route_binding_sha256
                        or private['secret_reference']!=request.secret_reference
                        or private['headers']!=request.extra_headers):
                    raise NativeRouteError('native_route_credential_revoked')
                endpoint=private['endpoint']
                # The existing source only authorizes the first-party destination.
                # Proxy egress stays in transport policy, never an arbitrary URL.
                url=urlsplit(endpoint)
                if (endpoint!=request.endpoint or url.scheme!='https' or url.hostname!='chatgpt.com'
                        or url.port not in (None,443) or url.path!='/backend-api/codex/responses'
                        or url.query or url.fragment or url.username or url.password):
                    raise NativeRouteError('native_route_credential_destination')
            finally:
                private.get('headers',{}).clear()
                private.clear()
        finally:route.close()

    def _source(self):
        from lib.oauth.token_store import load_stored_token
        stored=load_stored_token('codex')
        if not isinstance(stored,dict):raise _unavailable('credential_source_unavailable')
        reference=stored.get('codex_shared_reference')
        if isinstance(reference,dict):
            owner=reference.get('owner_user_id')
            from lib.oauth.codex_shared_login import local_owner
            if local_owner(owner) != owner:
                raise _unavailable('credential_source_binding')
            account=reference.get('account_id')
            tenant=reference.get('tenant_id','')
            try:
                identity=('shared',hashlib.sha256(json.dumps(reference,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest())
            except (TypeError,ValueError):
                raise _unavailable('credential_source_binding') from None
        else:
            owner=stored.get('owner_user_id')
            tenant=stored.get('tenant_id','')
            account=stored.get('account_id')
            identity=('owned',owner,tenant,account)
            if owner is None:
                # Reuse the declared personal legacy source boundary. Core code
                # never infers that a provider-global store belongs to any owner.
                from lib.oauth.codex_shared_login import local_owner
                owner=local_owner(self.boundary.owner_user_id)
        if (type(owner) is not int or owner!=self.boundary.owner_user_id
                or tenant!=self.boundary.tenant_id or not isinstance(account,str) or not account):
            raise _unavailable('credential_source_binding')
        return account,identity

    def __call__(self, request):
        self._authorize(request)
        account,identity=self._source()
        binding=(account,identity)
        if self._source_binding is None:
            self._source_binding=binding
        elif self._source_binding!=binding:
            raise _unavailable('credential_source_changed')
        from lib.oauth.outbound import resolve_oauth_request
        body={'model':request.wire_model,'_task_id':request.task_id}
        from lib.llm.llm_errors import SubscriptionLoginRequiredError
        try:
            token,headers,_=resolve_oauth_request('codex',body,request.extra_headers,
                user_id=str(self.boundary.owner_user_id),expected_account_id=account,cache_affinity_key=request.cache_affinity_key)
        except (SubscriptionLoginRequiredError,SubscriptionCredentialUnavailableError):
            self._authorize(request)
            if self._source()!=(account,identity):
                raise _unavailable('credential_source_changed') from None
            raise
        self._authorize(request)
        after_account,after_identity=self._source()
        if after_account!=account or after_identity!=identity:raise _unavailable('credential_source_changed')
        if not isinstance(token,str) or not token:raise _unavailable('credential_source_unavailable')
        headers=dict(headers)
        headers['Authorization']='Bearer '+token
        return headers
