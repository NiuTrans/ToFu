"""The Paper adapter retains cancellable proxy fallback and explicit byte mode."""
import pytest
from tests.support.paper_reader_frontend import run_paper_adapter
pytestmark = pytest.mark.unit


def test_pdf_transport_fallback_is_bounded_and_handles_render_failures(tmp_path):
    result = run_paper_adapter(tmp_path, r"""
      await load('/normal.pdf');
      const normal=[...opened];
      openFault=name=>name==='/open-failed.pdf';
      await load('/open-failed.pdf');
      const recoveredOpen=!!root.querySelector('canvas');
      renderFault=name=>name==='/render-failed.pdf';
      await load('/render-failed.pdf');
      const recoveredRender=!!root.querySelector('canvas')&&!root.querySelector('.paper-error');
      localStorage.setItem('tofu_paper_pdf_data','1');
      await load('/explicit-bytes.pdf');
      const result={normal,opened,downloads:downloads.map(row=>row.url),recoveredOpen,recoveredRender,
        cancellable:downloads.every(row=>row.signal instanceof AbortSignal)};
      stop();console.log(JSON.stringify(result));
    """)
    assert result == {
        'normal': ['/normal.pdf'],
        'opened': ['/normal.pdf', '/open-failed.pdf', 'bytes', '/render-failed.pdf', 'bytes', 'bytes'],
        'downloads': ['/open-failed.pdf', '/render-failed.pdf', '/explicit-bytes.pdf'],
        'recoveredOpen': True, 'recoveredRender': True, 'cancellable': True,
    }
