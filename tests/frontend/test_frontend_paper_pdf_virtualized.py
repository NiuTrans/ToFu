"""Paper actions delegate to the bounded reader; native layout is browser-tested."""
import pytest
from tests.support.paper_reader_frontend import run_paper_adapter
pytestmark = pytest.mark.unit


def test_paper_adapter_does_not_rasterize_the_whole_document_on_open(tmp_path):
    result = run_paper_adapter(tmp_path, r"""
      pages=500;await load('/long.pdf');
      const result={shells:root.querySelectorAll('.paper-reader-page').length,
        rasterized:[...rasterized],metadataPages:entry.pageCount};
      window.paperSetScaleFromInput('125%');
      result.zoom=document.getElementById('paperZoomLevel').value;
      stop();result.liveCanvases=root.querySelectorAll('canvas').length;
      result.busy=root.getAttribute('aria-busy');
      console.log(JSON.stringify(result));
    """)
    assert result == {'shells': 500, 'rasterized': [1], 'metadataPages': 500,
                      'zoom': '125%', 'liveCanvases': 0, 'busy': None}
