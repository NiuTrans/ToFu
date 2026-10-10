"""Real-browser saved-reading demand and teardown on the locally served app.

The storage service is a controlled port here; native/SQL tests separately own
publication, ordering and persistent recovery. This proves browser lifecycle.
"""
from pathlib import Path
import subprocess
import pytest

pytestmark = pytest.mark.visual


def test_independent_saved_documents_drain_in_real_browser(page, tmp_path):
    root = Path(__file__).resolve().parents[2]
    bundle = tmp_path / 'saved-reading.js'
    subprocess.run(['node', 'scripts/vite_test_bundle.mjs',
        'frontend/src/features/paper/saved-reading.ts', '--bundle', '--format=iife',
        '--platform=browser', '--global-name=ReadingDemand', '--outfile=' + str(bundle)],
        cwd=root, check=True, capture_output=True)
    page.add_script_tag(path=str(bundle))
    result = page.evaluate("""async () => {
      const requests = [], errors = [];
      const writer = new ReadingDemand.SavedReading({saveReading:(id,body) =>
        new Promise(resolve => requests.push({id,body,resolve}))},
        (id,error) => errors.push(String(error)));
      const state = {build_id:'b'.repeat(32),page:1,fraction:0,horizontal:0,
        scale:1,fit:true,reading_mode:false};
      for (let index=1;index<=256;index++) {
        const id = index.toString(16).padStart(64,'0');
        writer.open({document_id:id,reading_revision:0,reading:null});
        writer.capture(id,state);
      }
      writer.flush();
      const started = requests.length;
      for (const request of requests) writer.release(request.id);
      for (const request of requests) request.resolve({revision:1,state:request.body.state});
      await new Promise(resolve => setTimeout(resolve,0));
      const released = writer.entries.size;
      writer.close();
      const id = '1'.padStart(64,'0');
      writer.capture(id,{...state,page:2});writer.flush();
      return {started, requests:requests.length, released, errors};
    }""")
    assert result == {'started':256, 'requests':256, 'released':0, 'errors':[]}
