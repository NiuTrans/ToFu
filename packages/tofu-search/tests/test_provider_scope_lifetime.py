"""Provider views survive workers and retire state when their scope ends."""

import gc
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest

from tofu_search import providers
from tofu_search._provider_scope import auth_source_scope
from tofu_search.search.engines._xhs_sessions import capture_source

pytestmark = pytest.mark.unit


class Source(providers.AuthSourceProvider):
    def get_source(self, domain):
        return {'enabled': True, 'cookies': [{'name': 'session', 'value': 'fixture'}]}


def references_to_session():
    binding = auth_source_scope.current_binding()
    snapshot = capture_source(binding, 'xiaohongshu.com')
    return weakref.ref(binding), weakref.ref(snapshot.guard)


def test_registration_replacement_releases_retired_credential_state():
    providers.register_auth_source_provider(Source())
    binding, guard = references_to_session()
    providers.register_auth_source_provider(Source())
    gc.collect()
    assert binding() is None
    assert guard() is None


def test_active_worker_keeps_its_view_until_exit_then_releases_state():
    providers.register_auth_source_provider(Source())
    with providers.bound_optional_providers(), ThreadPoolExecutor(max_workers=1) as worker:
        binding, guard = references_to_session()
        providers.register_auth_source_provider(None)
        current = providers.get_auth_source_provider()
        future = providers.submit_with_provider_context(worker, providers.get_auth_source_provider)
        assert future.result() is current
        gc.collect()
        assert binding() is not None and guard() is not None
    gc.collect()
    assert binding() is None and guard() is None


def test_bound_view_releases_state_while_template_stays_registered():
    class Template(Source):
        def bind(self):
            return Source()

    template = Template()
    providers.register_auth_source_provider(template)
    with providers.bound_optional_providers():
        binding, guard = references_to_session()
    gc.collect()
    assert providers.get_auth_source_provider() is template
    assert binding() is None and guard() is None


def test_scoped_absence_cannot_discover_a_new_registration():
    with providers.bound_optional_providers():
        providers.register_auth_source_provider(Source())
        with providers.bound_optional_providers():
            assert providers.get_auth_source_provider() is None
    assert isinstance(providers.get_auth_source_provider(), Source)


def test_all_registrations_are_captured_before_any_bind_callback():
    source, replacement = Source(), Source()

    class Browser(providers.BrowserProvider):
        def bind(self):
            providers.register_auth_source_provider(replacement)
            return self

    providers.register_auth_source_provider(source)
    providers.register_browser_provider(Browser())
    with providers.bound_optional_providers():
        assert providers.get_auth_source_provider() is source
    assert providers.get_auth_source_provider() is replacement


@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt])
def test_later_bind_failure_restores_every_prior_scope(failure):
    class Browser(providers.BrowserProvider):
        def bind(self):
            return providers.BrowserProvider()

    class Site(providers.SiteSearchProvider):
        def bind(self):
            return providers.SiteSearchProvider()

    class Broken(Source):
        def bind(self):
            raise failure('binding failed')

    browser, site, auth = Browser(), Site(), Broken()
    providers.register_browser_provider(browser)
    providers.register_site_search_provider(site)
    providers.register_auth_source_provider(auth)
    with pytest.raises(failure, match='binding failed'), providers.bound_optional_providers():
        pytest.fail('failed binding must not enter the operation')
    assert providers.get_browser_provider() is browser
    assert providers.get_site_search_provider() is site
    assert providers.get_auth_source_provider() is auth


def test_static_duck_provider_needs_neither_hashing_nor_weak_reference_support():
    class Static:
        __slots__ = ()
        __hash__ = None

        def __eq__(self, other):
            return True

        def get_source(self, domain):
            return Source().get_source(domain)

    provider = Static()
    providers.register_auth_source_provider(provider)
    with providers.bound_optional_providers():
        assert providers.get_auth_source_provider() is provider
        binding, guard = references_to_session()
    providers.register_auth_source_provider(None)
    gc.collect()
    assert binding() is None and guard() is None
