"""Knowledge HTML text projection with per-call lightweight tree ownership.

Entry point: extract_html_text. Dependencies: standard HTML tokenizer/entities.
Preserves heading/table/hidden-content and text-node rules; no storage or DOM.
"""
from html.parser import HTMLParser
from html.entities import html5
import re
_VOID = frozenset('area base basefont bgsound br col command embed frame hr image img input isindex keygen link menuitem meta nextid param source spacer track wbr'.split())
_HIDDEN = frozenset(('script', 'style', 'template', 'noscript', 'svg'))
_ENTITIES = {name.rstrip(';'): value for name, value in html5.items()}
_ASCII_SPACE = frozenset(' \t\n\r\x0c')

class _HtmlTextNode:
    __slots__ = ('name', 'children')

    def __init__(self, name):
        self.name = name
        self.children = []

def _iter_descendants(node):
    stack = list(reversed(node.children))
    while stack:
        child = stack.pop()
        yield child
        if isinstance(child, _HtmlTextNode):
            stack.extend(reversed(child.children))

def _iter_text_nodes(node, strip=False):
    for child in _iter_descendants(node):
        if isinstance(child, str):
            value = child.strip() if strip else child
            if value:
                yield value

class _KnowledgeHtmlParser(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.root = _HtmlTextNode('root')
        self.stack = [self.root]
        self.pending = []
        self.void_ends = {}
        self.open_counts = {}
        self.preserved_depth = 0

    def flush(self):
        if not self.pending:
            return
        data = ''.join(self.pending)
        self.pending.clear()
        if not self.preserved_depth and all((c in _ASCII_SPACE for c in data)):
            data = '\n' if '\n' in data else ' '
        self.stack[-1].children.append(data)

    def handle_starttag(self, tag, attrs):
        self.flush()
        node = _HtmlTextNode(tag)
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)
            self.open_counts[tag] = self.open_counts.get(tag, 0) + 1
            self.preserved_depth += int(tag in {'pre', 'textarea'})
        else:
            self.void_ends[tag] = self.void_ends.get(tag, 0) + 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)
        else:
            self.void_ends[tag] -= 1

    def handle_endtag(self, tag):
        if self.void_ends.get(tag, 0):
            self.void_ends[tag] -= 1
            return
        self.flush()
        if not self.open_counts.get(tag, 0):
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].name == tag:
                for closed in self.stack[index:]:
                    self.open_counts[closed.name] -= 1
                    self.preserved_depth -= int(closed.name in {'pre', 'textarea'})
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.pending.append(data)

    def handle_entityref(self, name):
        self.handle_data(_ENTITIES.get(name, '&' + name))

    def handle_charref(self, name):
        hexadecimal = name.lower().startswith('x')
        raw = name[1:] if hexadecimal else name
        match = re.match('([0-9a-fA-F]+)(.*)' if hexadecimal else '([0-9]+)(.*)', raw)
        if not match:
            self.handle_data(raw)
            return
        value = int(match[1], 16 if hexadecimal else 10)
        if value == 0 or value > 1114111 or 55296 <= value <= 57343:
            data = '�'
        elif 128 <= value <= 159:
            try:
                data = bytes([value]).decode('cp1252')
            except UnicodeDecodeError:
                data = chr(value)
        else:
            data = chr(value)
        self.handle_data(data + match[2])

    def handle_comment(self, data):
        self.flush()

    def handle_decl(self, data):
        self.flush()

    def handle_pi(self, data):
        self.flush()

    def unknown_decl(self, data):
        self.flush()
        if data.upper().startswith('CDATA['):
            self.handle_data(data[6:])
            self.flush()

def extract_html_text(value: str) -> str:
    parser = _KnowledgeHtmlParser()
    parser.feed(value or '')
    parser.close()
    parser.flush()
    root = parser.root
    stack = [root]
    while stack:
        node = stack.pop()
        node.children = [child for child in node.children if not isinstance(child, _HtmlTextNode) or child.name not in _HIDDEN]
        stack.extend((child for child in node.children if isinstance(child, _HtmlTextNode)))
    headings = [node for node in _iter_descendants(root) if isinstance(node, _HtmlTextNode) and re.fullmatch('h[1-6]', node.name)]
    for heading in headings:
        heading.children = ['#' * int(heading.name[1]) + ' ' + ' '.join(_iter_text_nodes(heading, strip=True))]
    rows = [node for node in _iter_descendants(root) if isinstance(node, _HtmlTextNode) and node.name == 'tr']
    for row in rows:
        cells = [' '.join(_iter_text_nodes(node, strip=True)).replace('|', '\\|') for node in _iter_descendants(row) if isinstance(node, _HtmlTextNode) and node.name in {'th', 'td'}]
        if cells:
            row.children = ['| ' + ' | '.join(cells) + ' |']
    text = '\n'.join(_iter_text_nodes(root))
    text = re.sub('[ \\t]+\\n', '\n', text)
    text = re.sub('\\n[ \\t]+', '\n', text)
    return re.sub('\\n{3,}', '\n\n', text).strip()
