"""Image composer offers only the options the selected Model documents.

``/api/v4/images/models`` carries each routed Model's ``media_profile``
(registered, else maintained official facts). The creative composer disables
aspect-ratio and resolution pills that profile does not list, moves an
unsupported active choice to the first supported one, and keeps every option
for "All Models" or a Model without documented constraints.

Both stubbed responses are produced by the real owners: the model-routing
projection (which decides whether the image mode exists at all) and the
capability-route rows the image endpoint serves.
"""
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


def _owner_image_supply():
    """One owner's routing projection and image routes for three Models.

    Gemini 2.5 Flash Image takes its profile from the maintained official
    catalogue, Wide Only registers its own, Undocumented has none.
    """
    from lib.model_routing import (
        InMemoryModelRoutingRepository,
        OwnerBoundary,
        empty_document,
        list_capability_routes,
        upsert_local_provider,
    )
    from lib.model_routing.domain import public_projection
    from tests.support.model_registration import complete_registration

    repository = InMemoryModelRoutingRepository()
    owner = OwnerBoundary.create(1, None)
    repository.compare_and_swap(owner, empty_document(), expected_revision=0)
    authority = repository.get(owner)

    def model(creator_id, model_id, name, **facts):
        return {'creator_id': creator_id, 'model_id': model_id, 'display_name': name,
                'capabilities': ['image_gen'], 'context_window': 8192, 'quality_rank': 1,
                **facts}

    repository.compare_and_swap(owner, complete_registration({
        'contract_version': 'tofu.model-routing/v2', 'revision': authority.revision,
        'creators': [{'creator_id': 'google', 'name': 'Google'},
                     {'creator_id': 'acme', 'name': 'Acme'}],
        'models': [
            model('google', 'gemini-2.5-flash-image', 'Gemini 2.5 Flash Image'),
            model('acme', 'wide-only-image', 'Wide Only', media_profile={
                'aspect_ratios': ['16:9', '9:16'], 'resolutions': ['1K', '2K']}),
            model('acme', 'undocumented-image', 'Undocumented'),
        ],
        'providers': [], 'provider_accesses': [], 'connections': [], 'credentials': [],
        'offerings': [], 'deployments': [],
    }), expected_revision=authority.revision)
    upsert_local_provider(
        repository, owner, provider_id='gateway', display_name='Gateway',
        base_url='http://127.0.0.1:18100/v1',
        models=[{'model_id': model_id, 'capabilities': ['image_gen'],
                 'context_window': 8192, 'rpm': 12}
                for model_id in ('gemini-2.5-flash-image', 'wide-only-image',
                                 'undocumented-image')])
    routing = public_projection(repository.get(owner).document)
    routes = [route.public_dict()
              for route in list_capability_routes(repository, owner, 'image_gen')]
    return routing, routes


def _disabled(page, bar):
    return page.eval_on_selector_all(
        f'#{bar} .ig-pill', 'pills => pills.filter(p => p.disabled).map(p => p.textContent.trim())')


def _active(page, bar):
    return page.eval_on_selector(f'#{bar} .ig-pill.active', 'pill => pill.textContent.trim()')


def test_image_composer_disables_options_the_model_does_not_document(browser, live_server):
    from playwright.sync_api import expect

    routing, routes = _owner_image_supply()
    # The composer selects the first served route; serve Gemini first.
    routes.sort(key=lambda route: route['model'] != 'gemini-2.5-flash-image')
    context = browser.new_context(viewport={'width': 1280, 'height': 900}, service_workers='block')
    # Toolbar actions bind at boot completion; a click before it is lost.
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done', '1');"
                            "localStorage.setItem('tofu_ui_lang', 'zh');"
                            "window.__igAppReady = false;"
                            "window.addEventListener('tofu:app-ready', () => { window.__igAppReady = true; });")

    def route_request(route):
        url = urlparse(route.request.url)
        if url.netloc != urlparse(live_server).netloc:
            return route.abort()
        if url.path == '/api/v4/model-routing':
            return route.fulfill(json={'ok': True, 'revision': 1, 'model_routing': routing,
                                       'reasoning_controls': {},
                                       'metadata_inheritance_supported': True})
        if url.path == '/api/v4/images/models':
            return route.fulfill(json={'ok': True, 'models': routes})
        if url.path == '/api/v4/update/check':
            return route.fulfill(json={'update_available': False})
        return route.continue_()

    context.route('**/*', route_request)
    page = context.new_page()
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.__igAppReady === true')
        page.locator('#submenuExtras .submenu-trigger').click()
        # The image mode exists because the routing projection supplies image Models.
        expect(page.locator('#imageGenModeBtn')).to_be_visible()
        page.locator('#imageGenModeBtn').click()
        options = page.locator('#igModelDropdown .ig-model-option[data-model="wide-only-image"]')
        expect(options).to_have_count(1)

        # The first route is selected: its maintained profile is 1K-only and
        # documents every offered ratio.
        expect(page.locator('#igModelDropdown .ig-model-option.active')).to_have_attribute(
            'data-model', 'gemini-2.5-flash-image')
        expect(page.locator('#igResolutionBar .ig-pill[data-res="2K"]')).to_be_disabled()
        assert _disabled(page, 'igAspectBar') == []
        assert _active(page, 'igResolutionBar') == '1K'

        # A wide-only Model: square/classic ratios disabled, 1:1 moves to 16:9.
        page.locator('#igModelPicker .ig-preset').click()
        options.click()
        expect(page.locator('#igAspectBar .ig-pill[data-ar="1:1"]')).to_be_disabled()
        assert _disabled(page, 'igAspectBar') == ['1:1', '4:3', '3:4']
        assert _active(page, 'igAspectBar') == '16:9'
        assert _disabled(page, 'igResolutionBar') == []

        # An undocumented Model and "All Models" keep every option.
        page.locator('#igModelPicker .ig-preset').click()
        page.locator('#igModelDropdown .ig-model-option[data-model="undocumented-image"]').click()
        expect(page.locator('#igAspectBar .ig-pill[data-ar="1:1"]')).to_be_enabled()
        assert _disabled(page, 'igAspectBar') == []
        assert _disabled(page, 'igResolutionBar') == []
        assert _active(page, 'igAspectBar') == '16:9'
        page.locator('#igModelPicker .ig-preset').click()
        page.locator('#igModelDropdown .ig-model-option[data-model="__all__"]').click()
        assert _disabled(page, 'igAspectBar') == []
        assert _disabled(page, 'igResolutionBar') == []
    finally:
        context.close()
