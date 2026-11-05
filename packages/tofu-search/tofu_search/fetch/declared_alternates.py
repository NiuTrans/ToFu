"""Publisher-declared text representations of a received HTML document.

Entries: declared_text_alternate selects the first same-origin URL a response
declares as an alternate with a text media type, through ``<link
rel="alternate" type=...>`` (HTML) or an HTTP ``Link`` header (RFC 8288);
is_text_alternate_type checks a declared or served media type. This module
makes no request and owns no cache or budget: fetch/core.py admits the one
request, delivers its text and never searches an alternate for alternates.
Dependencies: stdlib html.parser/urllib and requests.utils.parse_header_links.
"""

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from requests.utils import parse_header_links

TEXT_ALTERNATE_TYPES = frozenset({'text/markdown', 'text/x-markdown', 'text/plain'})
_DEFAULT_PORTS = {'http': 80, 'https': 443}


def is_text_alternate_type(media_type):
    return (media_type or '').split(';', 1)[0].strip().lower() in TEXT_ALTERNATE_TYPES


class _Declarations(HTMLParser):
    """Collect the document base, language and rel=alternate link elements.

    The stdlib tokenizer keeps script/style bodies as text, and inert template
    content declares nothing for the document.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.base = None
        self.language = None
        self.links = []
        self._templates = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'template':
            self._templates += 1
        if self._templates:
            return
        attributes = {}
        for name, value in attrs:
            attributes.setdefault(name, value or '')
        if tag == 'html' and self.language is None:
            self.language = attributes.get('lang', '')
        elif tag == 'base' and self.base is None and attributes.get('href'):
            self.base = attributes['href']
        elif tag == 'link' and 'alternate' in attributes.get('rel', '').lower().split():
            self.links.append(attributes)

    def handle_endtag(self, tag):
        if tag == 'template' and self._templates:
            self._templates -= 1


def _origin(url):
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or _DEFAULT_PORTS.get(parts.scheme)


def declared_text_alternate(document_url, headers, html):
    """Return the first same-origin text alternate the response declares, or None.

    Link elements resolve through the document base URL and precede Link
    header targets, which resolve against the response URL. An hreflang other
    than the document language names a translation (HTML), and a header
    ``anchor`` names another resource (RFC 8288); neither represents this page.
    """
    parser = _Declarations()
    try:
        parser.feed(html)
        parser.close()
    except AssertionError:
        # html.parser (CPython 3.10+) signals fatal markup errors this way;
        # declarations seen before the error still stand.
        pass
    declared = [(parser.base or '', link.get('href', ''), link) for link in parser.links]
    for link in parse_header_links(headers.get('Link') or ''):
        params = {name.lower(): value for name, value in link.items()}
        if 'alternate' in params.get('rel', '').lower().split() and 'anchor' not in params:
            declared.append(('', params.get('url', ''), params))
    origin = _origin(document_url)
    language = (parser.language or '').lower()
    for base, href, link in declared:
        hreflang = link.get('hreflang', '').lower()
        if not is_text_alternate_type(link.get('type')) or (hreflang and hreflang != language):
            continue
        try:
            target = urljoin(urljoin(document_url, base), href.strip())
            if _origin(target) == origin:
                return target
        except ValueError:  # an invalid host or port in untrusted markup
            continue
    return None
