"""Real browser delivers complete source changes on the locally served app."""
from pathlib import Path
import subprocess
import os
import pytest

pytestmark = pytest.mark.visual


def test_complete_source_change_list_in_real_browser(page, tmp_path):
    root = Path(__file__).resolve().parents[2]
    for entry, global_name in [
        ('frontend/src/features/paper/preview-changes.ts', 'PaperChanges'),
        ('frontend/src/core/turn-state.ts', 'PaperTurns'),
    ]:
        bundle = tmp_path / (global_name + '.js')
        subprocess.run([os.environ.get('PLAYWRIGHT_NODEJS_PATH') or 'node', 'scripts/vite_test_bundle.mjs', entry, '--bundle',
            '--format=iife', '--platform=browser', '--global-name=' + global_name,
            '--outfile=' + str(bundle)], cwd=root, check=True, capture_output=True)
        page.add_script_tag(path=str(bundle))
    result = page.evaluate("""() => {
      const received=[];
      const owner=PaperChanges.createPaperPreviewChanges({changed:rows=>received.push(...rows),activated:()=>{}});
      const store=PaperTurns.createTurnStore('complete-source');
      store.subscribe((state,update)=>owner.observe('complete-source',state,update));
      owner.activateConversation('complete-source');
      store.dispatch({type:'snapshot',snapshot:{turns:[],conversationRevision:1}});
      const files=Array.from({length:1101},(_,i)=>({path:'part-'+i+'.tex',changeId:'change-'+i,source:'local'}));
      store.dispatch({type:'snapshot',snapshot:{conversationRevision:2,turns:[{
        turnId:'turn-source',ordinal:1,projection:{fileChanges:{blockId:'file-changes',
          count:files.length,files,state:'applied'}}}]}});
      return {count:received.length,last:received.at(-1)?.changeId};
    }""")
    assert result == {'count': 1101, 'last': 'change-1100'}
