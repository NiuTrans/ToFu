"""Read-only v2 authority binding to a native host; no provider/secret globals."""

import copy
from pathlib import Path
import unittest
from types import SimpleNamespace
from lib.identity import PrincipalContext
from lib.model_routing.domain import ModelRef, NativeModelSelection
from lib.model_routing.repository import OwnerBoundary, StoredAuthority
from lib.model_routing.native_route import (
    compile_native_route,
    run_native_route,
    NativeRouteError,
)
from tests.model_routing.test_model_routing_contract import _full_document


def principal(owner=7):
    return PrincipalContext.user(subject_id="native-route-fixture", owner_user_id=owner)


class Repository:
    def __init__(self):
        self.document = _full_document()
        self.boundary = OwnerBoundary.create(7)
        self.secret_calls = []
        self.document["connections"][0]["extra_headers"] = {"X-Region": "fixture"}
        self.document["credentials"][0].update(
            kind="api_key", secret_reference="secret-a"
        )

    def get(self, boundary):
        return StoredAuthority(self.boundary, 3, copy.deepcopy(self.document), 0)

    def resolve_secret(self, boundary, reference):
        self.secret_calls.append((boundary, reference))
        return "private-fixture-token"


def selection():
    return NativeModelSelection(
        ModelRef("creator", "alpha"), None, preferred_provider_id="provider-a"
    )


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_anthropic_endpoint_headers_and_native_protocol_are_owned(self):
        for base,endpoint in [("https://a.example/v1","https://a.example/v1/messages"),
                              ("https://a.example/anthropic","https://a.example/anthropic/v1/messages"),
                              ("https://a.example/v1/messages","https://a.example/v1/messages")]:
            repo=Repository();repo.document["connections"][0].update(protocol="anthropic",base_url=base)
            route=compile_native_route(repo,repo.boundary,selection())
            public,private=route.consume(principal())
            self.assertEqual(public["protocol"],"anthropic")
            self.assertEqual(private["endpoint"],endpoint)
            self.assertEqual(private["provider_protocol"],"anthropic")
            self.assertEqual(private["headers"]["x-api-key"],"private-fixture-token")
            self.assertEqual(private["headers"]["anthropic-version"],"2023-06-01")
            self.assertEqual(len(repo.secret_calls),1)

    async def test_responses_replay_is_declared_by_owned_route(self):
        for declared, expected in [(None, False), ('unknown', False), ('unsupported', False), ('supported', True)]:
            repo = Repository()
            connection = repo.document['connections'][0]
            connection['protocol'] = 'openai_responses'
            if declared is not None:
                connection['inference_profile'] = {'features': {'responses_reasoning_replay': declared}}
            route = compile_native_route(repo, repo.boundary, selection())
            public, private = route.consume(principal())
            self.assertEqual(public['protocol'], 'openai_responses')
            self.assertEqual(private['endpoint'], 'https://a.example/v1/responses')
            self.assertEqual(private['provider_protocol'], 'responses')
            self.assertIs(private['responses_reasoning_replay'], expected)
            self.assertEqual(len(repo.secret_calls), 1)

    async def test_responses_dialect_is_selected_only_by_owned_inference_profile(self):
        for profile in ('compatible', 'openai', 'codex'):
            repo = Repository()
            repo.document['connections'][0].update(protocol='openai_responses', responses_profile=profile)
            route = compile_native_route(repo, repo.boundary, selection())
            public, private = route.consume(principal())
            self.assertEqual(private['responses_profile'], profile)
            self.assertEqual(public['responses_profile'], profile)
            route = compile_native_route(repo, repo.boundary, selection())
            with self.assertRaises(NativeRouteError) as error:
                await run_native_route(SimpleNamespace(principal=principal()), route,
                    {'owner_user_id': 7, 'responses_profile': 'codex'},
                    prepare_template=lambda _: {}, callbacks={}, working_directory=Path('/tmp'))
            self.assertEqual(error.exception.kind, 'native_route_override')
            route.consume(principal())

    async def test_responses_oauth_refuses_before_secret_resolution(self):
        repo = Repository()
        repo.document['connections'][0]['protocol'] = 'openai_responses'
        repo.document['credentials'][0]['kind'] = 'oauth'
        with self.assertRaises(NativeRouteError) as error:
            compile_native_route(repo, repo.boundary, selection())
        self.assertEqual(error.exception.kind, 'native_route_credential_unsupported')
        self.assertEqual(repo.secret_calls, [])

    async def test_replay_override_refuses_without_consuming_route(self):
        repo = Repository()
        route = compile_native_route(repo, repo.boundary, selection())
        with self.assertRaises(NativeRouteError) as error:
            await run_native_route(SimpleNamespace(principal=principal()), route,
                                   {'owner_user_id': 7, 'responses_reasoning_replay': True},
                                   prepare_template=lambda _: {}, callbacks={}, working_directory=Path('/tmp'))
        self.assertEqual(error.exception.kind, 'native_route_override')
        route.consume(principal())

    async def test_owner_and_authorization_fail_before_secret_resolution(self):
        repo = Repository()
        with self.assertRaises(NativeRouteError):
            compile_native_route(repo, OwnerBoundary.create(8), selection())
        self.assertEqual(repo.secret_calls, [])
        repo.document["credentials"][0]["authorization"]["models"] = []
        repo.document["credentials"][1]["enabled"] = False
        with self.assertRaises(NativeRouteError):
            compile_native_route(repo, repo.boundary, selection())
        self.assertEqual(repo.secret_calls, [])

    async def test_unsupported_primary_protocol_is_not_silently_replaced(self):
        repo = Repository()
        repo.document["connections"][0]["protocol"] = "custom"
        with self.assertRaises(NativeRouteError) as error:
            compile_native_route(repo, repo.boundary, selection())
        self.assertEqual(error.exception.kind, "native_route_protocol_unsupported")
        self.assertEqual(repo.secret_calls, [])

    async def test_native_run_freezes_authority_clamps_output_and_consumes_route_once(
        self,
    ):
        repo = Repository()
        route = compile_native_route(repo, repo.boundary, selection())
        self.assertEqual(len(repo.secret_calls), 1)
        self.assertNotIn("private-fixture-token", repr(route))
        repo.document["connections"][0]["base_url"] = "https://mutated.invalid"
        captured = []

        async def run(config, **kwargs):
            captured.append(copy.deepcopy(config))
            return {"type": "done", "status": "done"}

        client = SimpleNamespace(principal=principal(), run=run)
        result = await run_native_route(
            client,
            route,
            {"owner_user_id": 7, "messages": [], "tools": {}},
            prepare_template=lambda metadata: {
                "model": metadata["wire_model"],
                "max_tokens": 100000,
            },
            callbacks={},
            working_directory=Path("/tmp"),
        )
        self.assertEqual(
            captured[0]["endpoint"], "https://a.example/v1/chat/completions"
        )
        self.assertEqual(
            captured[0]["headers"]["Authorization"], "Bearer private-fixture-token"
        )
        self.assertEqual(captured[0]["template"]["model"], "alpha-wire-a")
        self.assertEqual(captured[0]["template"]["max_tokens"], 65535)
        self.assertEqual(result.route["provider_id"], "provider-a")
        self.assertEqual(result.route["revision"], 3)
        with self.assertRaises(NativeRouteError):
            route.consume(principal())

    async def test_wrong_owner_cannot_consume_and_route_endpoint_override_is_refused(
        self,
    ):
        repo = Repository()
        route = compile_native_route(repo, repo.boundary, selection())
        with self.assertRaises(NativeRouteError):
            route.consume(principal(8))
        client = SimpleNamespace(principal=principal())
        with self.assertRaises(NativeRouteError):
            await run_native_route(
                client,
                route,
                {"owner_user_id": 7, "endpoint": "http://foreign"},
                prepare_template=lambda _: {},
                callbacks={},
                working_directory=Path("/tmp"),
            )
        _, private = route.consume(principal())
        self.assertEqual(private["headers"]["X-Region"], "fixture")

    async def test_failed_template_does_not_reuse_resolved_credentials_or_start_host(
        self,
    ):
        repo = Repository()
        route = compile_native_route(repo, repo.boundary, selection())
        client = SimpleNamespace(principal=principal())
        with self.assertRaises(NativeRouteError):
            await run_native_route(
                client,
                route,
                {"owner_user_id": 7},
                prepare_template=lambda _: {"model": "wrong", "max_tokens": 1},
                callbacks={},
                working_directory=Path("/tmp"),
            )
        with self.assertRaises(NativeRouteError):
            route.consume(principal())
        self.assertEqual(len(repo.secret_calls), 1)

    async def test_local_protocol_and_header_only_credentials_use_canonical_decoder(
        self,
    ):
        import json

        repo = Repository()
        repo.document["connections"][0]["protocol"] = "local"
        repo.resolve_secret = lambda boundary, ref: json.dumps(
            {
                "format": "tofu.credential-secret/v1",
                "api_key": "",
                "extra_headers": {"X-Service-Token": "private-header"},
            }
        )
        route = compile_native_route(repo, repo.boundary, selection())
        public, private = route.consume(principal())
        self.assertEqual(public["protocol"], "local")
        self.assertEqual(
            public["model_ref"], {"creator_id": "creator", "model_id": "alpha"}
        )
        self.assertEqual(private["headers"]["X-Service-Token"], "private-header")
        self.assertNotIn("Authorization", private["headers"])

    async def test_carrier_bound_and_close_do_not_allow_reuse_or_manual_issuance(self):
        from lib.model_routing.native_route import Route

        repo = Repository()
        with self.assertRaises(NativeRouteError):
            compile_native_route(repo, repo.boundary, selection(), byte_limit=1)
        with self.assertRaises(NativeRouteError):
            Route(7, {}, {}, 100)
        route = compile_native_route(repo, repo.boundary, selection())
        route.close()
        with self.assertRaises(NativeRouteError):
            route.consume(principal())


if __name__ == "__main__":
    unittest.main()
