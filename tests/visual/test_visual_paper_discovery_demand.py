"""Complete paper candidate selection in a real locally served browser."""
from pathlib import Path
import os
import subprocess
import pytest

pytestmark = pytest.mark.visual


def test_candidates_beyond_64_remain_selectable(page, tmp_path):
    root = Path(__file__).resolve().parents[2]
    bundle = tmp_path / 'discovery.js'
    subprocess.run([os.environ.get('PLAYWRIGHT_NODEJS_PATH') or 'node',
        'scripts/vite_test_bundle.mjs', 'frontend/src/features/paper/preview-controller.ts',
        '--bundle', '--format=iife', '--platform=browser', '--global-name=PaperDiscovery',
        '--outfile=' + str(bundle)], cwd=root, check=True, capture_output=True)
    page.add_script_tag(path=str(bundle))
    result = page.evaluate("""async () => {
      const host=document.createElement('div');host.className='chat-wrapper';
      host.innerHTML='<div class="chat-container"></div><div class="input-area"><textarea></textarea></div>';
      document.body.append(host);
      const choices=Array.from({length:71},(_,i)=>({source:'local',path:'paper-'+i+'/main.tex',
        main_file:'paper-'+i+'/main.tex',project_root:'/owned',label:'paper-'+i}));
      let selectedPath=null;
      const documentsApi={discover:async()=>({documents:choices,selected:null,diagnostics:[]}),
        open:async()=>({document:null}),saveReading:async()=>({revision:1}),
        select:async body=>{selectedPath=body.path;return {selected:choices.find(row=>row.path===body.path)}}};
      const preview=PaperDiscovery.createPaperPreview({host:()=>host,activeConversation:()=> 'c',
        translate:key=>key,documentsApi,debounceMs:1,pollMs:1});
      try {
        preview.openDocument('c');
        let select;
        for(let i=0;i<200;i++) {
          select=host.querySelector('select[aria-label="preview.chooseDocument"]');
          if(!select)host.querySelector('button[aria-label="preview.chooseDocument"]')?.click();
          if(select?.options.length===72)break;
          await new Promise(resolve=>setTimeout(resolve,5));
        }
        if(!select)throw new Error('Missing document selector');
        const count=select.options.length;
        select.value='70';select.dispatchEvent(new Event('change',{bubbles:true}));
        for(let i=0;i<200&&!selectedPath;i++)await new Promise(resolve=>setTimeout(resolve,5));
        return {count,selectedPath};
      } finally {preview.destroy();host.remove();}
    }""")
    assert result == {'count': 72, 'selectedPath': 'paper-70/main.tex'}
