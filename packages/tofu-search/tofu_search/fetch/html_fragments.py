"""Select a literal HTML fragment with its structural documentation context.

Entries: fragment_identifier and select_fragment_html. The extraction owner
passes a sanitized BeautifulSoup document; this module never fetches, rewrites
source text, infers semantic relevance, or resolves ambiguous identifiers.
"""

from urllib.parse import unquote, urlsplit

_HEADINGS = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')
_CONTAINERS = ('section', 'article', 'main', 'div', 'details')


def fragment_identifier(url):
    """Decode a conventional element identifier once, without normalization."""
    try:
        fragment = urlsplit(url).fragment
        if not fragment or ':~:' in fragment:
            return None
        return unquote(fragment, encoding='utf-8', errors='strict')
    except (ValueError, UnicodeError):
        return None


def _render(nodes, wrapper=''):
    if not any(node.get_text(strip=True) for node in nodes):
        return None
    content = ''.join(str(node) for node in nodes)
    if wrapper:
        content = f'<{wrapper}>{content}</{wrapper}>'
    return '<html><body><main>' + content + '</main></body></html>'


def _definition_html(term):
    if not term.parent or term.parent.name != 'dl':
        return None
    # HTML permits consecutive terms (aliases) before their shared definitions.
    first = term
    previous = first.find_previous_sibling()
    while previous is not None and previous.name == 'dt':
        first, previous = previous, previous.find_previous_sibling()
    nodes, definitions = [], False
    node = first
    while node is not None and node.name in ('dt', 'dd'):
        if node.name == 'dt' and definitions:
            break
        definitions = definitions or node.name == 'dd'
        nodes.append(node)
        node = node.find_next_sibling()
    return _render(nodes, 'dl') if definitions else None


def _heading_html(heading):
    level = int(heading.name[1])
    node = heading
    # Markdown/Wikipedia renderers may wrap a heading in a small div, with
    # its actual paragraphs following that wrapper rather than the heading.
    while (node.parent is not None and node.parent.name == 'div'
           and node.parent.find_all(_HEADINGS) == [heading]):
        node = node.parent
    nodes = [node]
    for sibling in node.next_siblings:
        if not getattr(sibling, 'name', None):
            # The extraction owner preserves text in ordinary block markup.
            # A heading followed by bare non-whitespace text is ambiguous here.
            if str(sibling).strip():
                return None
            continue
        headings = ([sibling] if sibling.name in _HEADINGS else
                    sibling.find_all(_HEADINGS))
        if any(int(item.name[1]) <= level for item in headings):
            break
        nodes.append(sibling)
    return _render(nodes)


def select_fragment_html(soup, identifier):
    """Return a scoped document, or None when the original page must remain."""
    targets = soup.find_all(id=identifier)
    if not targets:
        targets = soup.find_all('a', attrs={'name': identifier})
    if len(targets) != 1:
        return None
    target = targets[0]

    # Rustdoc and disclosure widgets put the signature in a summary and the
    # explanation beside it. The enclosing details element owns both pieces.
    summary = target if target.name == 'summary' else target.find_parent('summary')
    if summary is not None and summary.parent.name == 'details':
        return _render([summary.parent])
    term = target if target.name == 'dt' else target.find_parent('dt')
    if term is not None:
        return _definition_html(term)
    heading = target if target.name in _HEADINGS else target.find_parent(_HEADINGS)
    if heading is not None:
        return _heading_html(heading)
    if target.name in _CONTAINERS:
        return _render([target])
    block = target if target.name in ('p', 'pre', 'table', 'li') else target.find_parent(
        ('p', 'pre', 'table', 'li'))
    if block is not None and block.get_text(strip=True):
        return _render([block])
    if not target.get_text(strip=True):
        following = (block or target).find_next_sibling()
        if following is not None and following.name in _HEADINGS:
            return _heading_html(following)
    return None
