"""An old Paper selection cannot replace the newly selected PDF or its error UI."""
import pytest
from tests.support.paper_reader_frontend import run_paper_adapter
pytestmark = pytest.mark.unit


def test_new_paper_selection_fences_late_failure_and_releases_resources(tmp_path):
    result = run_paper_adapter(tmp_path, r"""
      let rejectOld;
      hold=name=>name==='/old.pdf'?new Promise((resolve,reject)=>{rejectOld=reject}):null;
      const old=load('/old.pdf');
      while(!rejectOld)await new Promise(resolve=>setTimeout(resolve,0));
      await load('/new.pdf');
      rejectOld(new Error('late failure'));await old;
      const result={hasPdf:!!root.querySelector('canvas'),hasError:!!root.querySelector('.paper-error'),
        pages:entry.pageCount,downloads:downloads.length,oldDestroyed:destroyed.includes('/old.pdf')};
      stop();result.childrenAfterClose=root.childElementCount;
      result.newDestroyed=destroyed.includes('/new.pdf');
      console.log(JSON.stringify(result));
    """)
    assert result == {'hasPdf': True, 'hasError': False, 'pages': 20, 'downloads': 0,
                      'oldDestroyed': True, 'childrenAfterClose': 0, 'newDestroyed': True}
