"""Navigation/overlay cleanup reads component class names, not CSS values.

Utility CSS puts values inside class tokens (Tailwind arbitrary values such as
``pt-[calc(10rem+var(--banner-height,2.5rem))]``). Matching the noise rule
against those values deleted Mintlify's whole content wrapper as a "banner",
left ``<main>`` empty and made a server-rendered page look like a JS shell.
"""

import pytest

from tofu_search.fetch import utils
from tofu_search.fetch.html_extract import extract_html_text

pytestmark = pytest.mark.unit

CONTENT = ('GLM-5.3-Flash native visual capabilities let the model observe '
           'interfaces, rendering results and interaction feedback. ' * 3)


def _page(wrapper_class, *, extra=''):
    return (f'<html><head><title>Release notes</title></head><body><div id="__next">'
            f'<main><div class="{wrapper_class}"><article><h1>New Released</h1>'
            f'<p>{CONTENT}</p></article></div>{extra}</main></div></body></html>')


@pytest.mark.parametrize('wrapper_class', [
    'relative pt-[calc(10rem+var(--banner-height,2.5rem))]',
    'lg:[&_.modal]:hidden mx-auto',
    'grid-cols-[minmax(0,1fr)_var(--sidebar-width)]',
])
def test_a_css_value_inside_a_class_token_is_not_a_component_name(wrapper_class):
    html = _page(wrapper_class)
    scope = {}

    text = extract_html_text(html, 100_000, url='https://docs.example/release', scope=scope)

    assert 'native visual capabilities' in text
    assert scope['rendering']['readable_primary'] is True
    assert not utils._looks_like_spa_shell(html, text, signals=scope['rendering'])


@pytest.mark.parametrize('noise_class', [
    'cookie-banner', 'modal', 'site-sidebar', 'popup bg-[#fff]', 'nav-menu', 'advert',
])
def test_named_overlays_and_navigation_are_still_removed(noise_class):
    html = _page('prose', extra=f'<div class="{noise_class}">Accept all cookies and subscribe</div>')

    text = extract_html_text(html, 100_000, url='https://docs.example/release')

    assert 'native visual capabilities' in text
    assert 'Accept all cookies' not in text
