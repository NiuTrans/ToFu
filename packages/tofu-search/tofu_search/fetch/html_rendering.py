"""Rendering-admission signals from the extraction owner's sanitized DOM.

Entries: javascript_notice, rendering_signals, empty_application_main,
needs_rendering. This module makes no request and does not sanitize or reparse
HTML. html_extract supplies the same restored/visible tree used for evidence.
"""

import re

PRIMARY_SELECTORS = ('main', '[role="main"]', 'article')
_ROOT_IDS = frozenset({'root', 'app', '__next', '__nuxt', 'main-app', 'react-root', 'vue-app'})
_JS_REQUIRED = (
    'enable javascript', 'requires javascript', 'javascript is required',
    'javascript is disabled', 'need javascript', 'activate javascript',
    'turn on javascript', '启用 javascript', '需要启用 javascript',
    '请启用 javascript', '开启 javascript',
)
_LOADING = re.compile(r'(?:loading|please wait|正在加载|加载中|载入中)[\s.!…。]*', re.I)


def javascript_notice(soup):
    return any(any(term in node.get_text(' ', strip=True).lower() for term in _JS_REQUIRED)
               for node in soup.find_all('noscript')
               if not any(parent.name in {'template', 'script'} for parent in node.parents))


def rendering_signals(soup, *, requires_javascript=False):
    roots = soup.find_all(lambda node: (
        node.get('id') in _ROOT_IDS or node.has_attr('data-reactroot')
        or node.has_attr('ng-app') or node.has_attr('ng-version')))
    roots = [node for node in roots if not any(p.name == 'template' for p in node.parents)]
    regions = []
    for selector in PRIMARY_SELECTORS:
        regions = [node for node in soup.select(selector)
                   if not any(p.name == 'template' for p in node.parents)]
        if regions:
            break
    primary_text = ' '.join(node.get_text(' ', strip=True) for node in regions).strip()
    return {
        'application_root': bool(roots),
        'primary_region': bool(regions),
        'readable_primary': bool(primary_text and not _LOADING.fullmatch(primary_text)),
        'requires_javascript': bool(requires_javascript),
    }


def empty_application_main(signals):
    return bool(signals and signals.get('application_root') and signals.get('primary_region')
                and not signals.get('readable_primary'))


def needs_rendering(html_length, extracted_text, signals):
    # A readable short section or server-rendered article remains usable even
    # when a noscript notice advertises optional interactive features.
    if signals.get('readable_primary'):
        return False
    if empty_application_main(signals):
        return True
    text_length = len(extracted_text or '')
    if signals.get('application_root'):
        if signals.get('requires_javascript') or text_length < 150:
            return True
    return html_length > 2000 and text_length < 100
