"""HTML parser dependency and fault boundaries for the canonical knowledge adapter."""
import builtins

import pytest

pytestmark=pytest.mark.unit

from lib.knowledge import ingest
from lib.knowledge.html_text import extract_html_text


def test_html_projection_does_not_activate_beautifulsoup(monkeypatch):
    original=builtins.__import__
    def import_guard(name,*args,**kwargs):
        if name=='bs4' or name.startswith('bs4.'):
            raise AssertionError('HTML text projection imported BeautifulSoup')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',import_guard)
    assert ingest._html_to_text('<h2>Guide</h2><script>hidden</script><p>中文</p>')=='## Guide\n中文'


def test_parser_failure_preserves_existing_text_fallback(monkeypatch):
    def fail(value):raise RuntimeError('injected HTML parser failure')
    monkeypatch.setattr('lib.knowledge.html_text.extract_html_text',fail)
    assert ingest._html_to_text('<p>kept</p><script>hidden</script><p>tail</p>')=='kept\n\ntail'


def test_html_state_is_per_call_and_deep_nesting_is_iterative():
    assert extract_html_text('<pre>  x\n y')=='x\ny'
    assert extract_html_text('<div>'*5000+'tail'+'</orphan>'*5000) =='tail'
    assert extract_html_text('<h1>fresh</h1>')=='# fresh'


@pytest.mark.parametrize('source,expected',[
    ('a<br>b</br>c','a\nbc'),
    ('&#0;&#128;&#xD800;','\ufffd€\ufffd'),
    ('<table><tr><th>a</th><td>82|181</td></tr></table>','| a | 82\\|181 |'),
])
def test_text_structure_and_entity_recovery(source,expected):
    assert extract_html_text(source)==expected


def test_html_parser_loads_only_on_first_html_use():
    import subprocess
    import sys
    code="""import sys
from lib.knowledge import ingest
assert 'lib.knowledge.html_text' not in sys.modules
ingest.extract(b'<h1>lazy</h1>', 'lazy.html')
assert 'lib.knowledge.html_text' in sys.modules
"""
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
