"""Declared site-domain scope for adapter execution on BrowserPage.

This restriction never grants browser access. BrowserPage still applies live
owner/device policy after checking the scope; adapters bind it only for their
invocation. Login redirects retain the shared adapter failure vocabulary.
"""

from dataclasses import dataclass

from .access import normalize_domain
from .adapter_errors import AdapterExecutionError


def matches_site_domain(url: str, domains: tuple[str, ...]) -> bool:
    host = normalize_domain(url)
    return bool(host) and any(
        host == normalize_domain(domain) or host.endswith('.' + normalize_domain(domain))
        for domain in domains)


@dataclass(frozen=True)
class SiteScope:
    domains: tuple[str, ...]
    site: str
    login_url: str = ''

    def __post_init__(self):
        object.__setattr__(self, 'domains', tuple(self.domains))

    def require(self, url: str, *, redirect: bool) -> None:
        if matches_site_domain(url, self.domains):
            return
        host = normalize_domain(url)
        if redirect and self.login_url:
            raise AdapterExecutionError(
                'login_required',
                f'{self.site} redirected outside its adapter domain; finish sign-in in the browser',
                retryable=True, details={'login_url': self.login_url, 'redirect_domain': host})
        raise AdapterExecutionError(
            'access_denied', f'URL is outside adapter domains: {host or "invalid URL"}',
            details={'domain': host})
