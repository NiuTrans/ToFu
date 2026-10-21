"""Project the anonymous HTTP route into one isolated browser context.

Entry: anonymous_browser_proxy. The existing proxy manager selects the route;
Requests resolves environment/scheme/NO_PROXY for the initial destination.
Playwright owns its context-wide transport. No retry, preference mutation,
cookie authority, persistent state or browser dependency is introduced here.
"""

from urllib.parse import unquote, urlsplit

import requests

from tofu_search.config import get_config


def _browser_proxy_options(value):
    """Use Playwright's separate authentication fields without leaking userinfo."""
    try:
        parsed = urlsplit(value if '://' in value else 'http://' + value)
        scheme = parsed.scheme.lower()
        if scheme == 'socks5h':
            scheme = 'socks5'  # Chromium SOCKS5 always resolves names at the proxy.
        if scheme not in ('http', 'https', 'socks5') or not parsed.hostname:
            raise ValueError
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError
        if scheme == 'socks5' and (parsed.username is not None or parsed.password is not None):
            # Chromium does not support authenticated SOCKS5; never silently
            # remove its credentials or fall back to a direct browser route.
            raise ValueError
        host = parsed.hostname
        if ':' in host:
            host = '[' + host + ']'
        server = scheme + '://' + host
        if parsed.port is not None:
            server += ':' + str(parsed.port)
        options = {'server': server}
        if parsed.username is not None:
            options['username'] = unquote(parsed.username)
            options['password'] = unquote(parsed.password or '')
        return options
    except (TypeError, ValueError):
        raise ValueError('Unsupported browser proxy configuration') from None


def anonymous_browser_proxy(url):
    """Reuse the first eligible HTTP route, including its learned preference.

Only the initial URL resolves environment bypass/scheme rules. Subresources
and redirects share the resulting browser-context route. Identity-bearing
fetch/search and self-contained PDF rendering do not consult this function.
"""
    # Lazy import avoids search -> fetch -> search initialization cycles.
    from tofu_search.search.proxy_mode import proxy_mode_manager

    _host_key, plan = proxy_mode_manager.fetch_attempt_plan(url, get_config())
    _mode, proxies = plan[0]
    selected = requests.utils.get_environ_proxies(url) if proxies is None else proxies
    proxy = requests.utils.select_proxy(url, selected)
    return _browser_proxy_options(proxy) if proxy else None
