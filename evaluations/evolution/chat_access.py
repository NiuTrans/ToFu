"""Paired evaluation adapter over the running application's authenticated Chat API.

Only model references and public outputs cross HTTP. Model secrets stay in the
Chat service. The explicit owner must match the authenticated principal; the
normal server routing, billing and task authority own every invocation.
"""
from __future__ import annotations
import json
import os
import time
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise RuntimeError('evaluation API redirects are not allowed')


class ChatEvaluator:
    def __init__(self, *, user_id, base_url='http://127.0.0.1:15000', requested=None,
                 run_id=None, role='executor', request=None, json_object=False):
        parsed=urlparse(base_url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid Tofu application URL')
        if parsed.scheme=='http' and parsed.hostname not in ('127.0.0.1','localhost','::1'):
            raise ValueError('remote Tofu API requires HTTPS')
        self.base_url=base_url.rstrip('/')
        self.json_object = json_object
        self._request=request or self._http
        identity=self._request('/api/v4/keys/whoami')
        if not identity.get('authenticated') or identity.get('owner_id')!=user_id:
            raise ValueError('evaluation owner does not match the authenticated Chat user')
        if run_id:
            if requested is not None:raise ValueError('choose a role or an explicit model')
            from lib.evolution.role_models import selected_model
            run=self._request('/api/v4/evolution/runs/'+quote(run_id,safe=''))['run']
            requested=selected_model(run,role)
        models=self._request('/api/v4/evolution/models')['models']
        if not models:raise ValueError('Chat has no available model route')
        self.model=requested or models[0]['model']
        if self.model not in [row['model'] for row in models]:
            raise ValueError('selected model is not available in Chat')
        self.deployment_identity = self._deployment_identity()

    def _deployment_identity(self):
        health = self._request('/api/health', timeout_s=2)
        boot = health.get('bootId') if isinstance(health, dict) else None
        if not isinstance(boot, str) or not boot or len(boot) > 160:
            raise ValueError('application boot identity unavailable')
        fingerprint = health.get('codeFingerprint')
        return {'boot_id':boot, 'code_fingerprint':
                {key:fingerprint.get(key) for key in ('head','dirty','digest')}
                if isinstance(fingerprint, dict) else None}


    def _http(self, path, body=None, timeout_s=10):
        headers={'Content-Type':'application/json'}
        token=os.environ.get('TOFU_API_KEY','')
        if token:headers['Authorization']='Bearer '+token
        request=Request(self.base_url+path,headers=headers,
                        data=json.dumps(body).encode() if body is not None else None)
        with build_opener(_NoRedirect).open(request,timeout=timeout_s) as response:
            raw=response.read(262145)
        if len(raw)>262144:raise ValueError('Chat response exceeds evaluation bound')
        payload=json.loads(raw)
        return payload.get('data',payload)

    def invoke(self,messages,*,timeout_s):
        if self._deployment_identity()['boot_id'] != self.deployment_identity['boot_id']:
            raise RuntimeError('application restarted during evaluation; no new call dispatched')
        response=self._request('/api/v4/chat/completions',{
            'model':self.model,'messages':messages,'max_tokens':2048,'temperature':0,
            **({'response_format':{'type':'json_object'}} if self.json_object else {}),
            'timeout_s':timeout_s,'config':{
                'toolSpecAllowlist':[],'memoryEnabled':False,'humanGuidanceEnabled':False,
                'executionLimits':{'max_tokens':8192,'max_tool_calls':0,
                                   'deadline':time.time()+timeout_s}}},timeout_s=timeout_s+2)
        choice=(response.get('choices') or [{}])[0]
        try:
            identity_match = self._deployment_identity()['boot_id'] == self.deployment_identity['boot_id']
        except Exception:
            identity_match = False
        return {'application_boot_id':self.deployment_identity['boot_id'],
                'deployment_identity_match':identity_match,
                'content':(choice.get('message') or {}).get('content',''),
                'status':'done' if choice.get('finish_reason')=='stop' else 'incomplete',
                'error':response.get('error') or (None if identity_match else 'application_identity_unverified'),
                'usage':response.get('usage'),
                'task_id':response.get('task_id',''),'model':response.get('model',''),
                'provider_id':response.get('provider_id','')}
