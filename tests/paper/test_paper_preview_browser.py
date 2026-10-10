"""Real PDF.js preview split: desktop, mobile, controls and closing intent."""
import json
import subprocess
from pathlib import Path
import pytest
from tests.frontend import test_pdf_reader_browser as browser_support
from tests._runtime_sections import _require_frontend_bundler

reader_page = browser_support.reader_page

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


def test_preview_split_reading_controls_and_mobile_teardown(reader_page, tmp_path):
    page, errors = reader_page
    # Shared dep guard: skip on dep-less lanes, fail under
    # TOFU_REQUIRE_FRONTEND=1 (was an unguarded bundler call).
    _require_frontend_bundler()
    subprocess.run(['node','scripts/vite_test_bundle.mjs','frontend/src/features/paper/preview-controller.ts',
                    '--bundle','--format=iife','--platform=browser','--global-name=Preview',
                    '--outfile='+str(tmp_path/'preview.js')],cwd=ROOT,check=True,capture_output=True)
    style_root=ROOT/'frontend/src/styles/application'
    manifest=json.loads((style_root/'manifest.json').read_text())
    preview_css=''.join((style_root/row['path']).read_text() for row in manifest['deferredSections']
                        if row['path'].startswith('24-paper-preview-'))
    (tmp_path/'preview.css').write_text(preview_css)
    labels=json.loads((ROOT/'frontend/src/i18n/locales/zh.json').read_text())
    labels={key:value for key,value in labels.items() if key.startswith('preview.') or key in ['paper.zoomOut','paper.zoomIn','paper.fitWidth','common.close','common.loading']}
    page.evaluate('reader.destroy()')
    page.set_content('''<!doctype html><html lang="zh"><meta charset="utf-8">
      <link rel="stylesheet" href="/reader.css"><link rel="stylesheet" href="/preview.css">
      <style>:root{--bg-primary:white;--bg-secondary:#f2f3f5;--border:#e0e3e7;--text-primary:#20242a;--text-secondary:#626872;--accent:#526fd3;--accent-subtle:#ebeffb;--bg-hover:#f1f3f8}
      *{box-sizing:border-box}body{margin:0;font:14px Arial;color:var(--text-primary)}
      .chat-wrapper{position:relative;display:flex;flex-direction:column;height:100vh;width:100%;min-width:0}
      .chat-container{flex:1;overflow:auto;min-height:0;padding:32px}.input-area{padding:16px;border-top:1px solid var(--border)}
      textarea{display:block;width:100%;height:78px;resize:none;padding:12px;border:1px solid var(--border);border-radius:8px;font:inherit}
      </style><div class="chat-wrapper"><div class="chat-container"><p>把实验结果和论文放在一起阅读。</p></div>
      <div class="input-area"><textarea aria-label="输入消息" placeholder="继续讨论这篇论文"></textarea></div></div></html>''')
    page.get_by_role('textbox',name='输入消息').focus()
    page.add_script_tag(url='/preview.js')
    page.evaluate(r'''async labels=>{
      const pdfjs=await import('/pdf.mjs');pdfjs.GlobalWorkerOptions.workerSrc='/pdf.worker.mjs';
      localStorage.setItem('tofu_paper_preview_split','55');
      window.buildCount=0;window.activeConversation='conv-a';window.detached=[];window.lastBuild={};window.discoveryCount=0;
      window.viewerHolds=new Set();window.viewerCalls=[];window.expireViewer=false;
      const documentId=()=>(lastBuild.source==='overleaf'?'e':'d').repeat(64);
      const receipt=()=>({schema_version:1,build_id:String(buildCount).padStart(32,'0'),document_id:documentId(),
        owner_user_id:1,target:lastBuild.source==='overleaf'?'overleaf':'host',source_digest:lastBuild.source==='overleaf'?null:'s'.repeat(64),
        version_verified:lastBuild.source!=='overleaf',main_file:'main.tex',engine:'pdflatex',
        toolchain:'test',status:'success',started_at:1,completed_at:2,artifacts:[],diagnostics:[]});
      const api={start:async body=>{lastBuild=body;buildCount++;return{task_id:'task-'+buildCount,document_id:documentId(),subscriber_id:body.subscriber_id}},
        task:async()=>({status:'done',result:{receipt:receipt(),superseded:false,pdf_url:'/paper.pdf?build='+buildCount,
          location:{build_id:receipt().build_id,file:'main.tex',line:3,page:17,rects:[{page:17,h:56,v:200,width:200,height:15}]}}}),
        viewer:async(d,b,body)=>{viewerCalls.push({document:d,build:b,...body});
          if(body.operation==='retain')viewerHolds.add(body.viewer_id);
          if(body.operation==='release'||expireViewer)viewerHolds.delete(body.viewer_id);
          const retained=viewerHolds.has(body.viewer_id);return{retained,expires_in_seconds:retained?240:0}},
        renew:async()=>({renewed:true}),detach:async id=>{detached.push(id)},resolve:url=>url,
        bytes:async(url,signal)=>(await fetch(url,{signal})).arrayBuffer(),log:async()=>'<script>window.unwanted=true</script>\n'+'Build log\n'.repeat(4096)};
      const sourceDocument={source:'local',path:'main.tex',main_file:'main.tex',project_root:'/owned',label:'main.tex'};
      const sibling={...sourceDocument,path:'migration/main.tex',main_file:'migration/main.tex',label:'migration/main.tex'};
      window.savedDocument=null;window.savedRemote=null;
      const documentsApi={open:async()=>({document:null}),saveReading:async(id,body)=>({revision:1,state:body.state}),discover:async()=>{discoveryCount++;return{documents:[sourceDocument,sibling,...(savedRemote?[savedRemote]:[])],
          selected:savedDocument,diagnostics:[],overleaf_servers:['papers']}},
        select:async body=>{if(body.project_id){savedRemote={source:'overleaf',project_id:body.project_id,mcp_server:body.mcp_server,
          main_file:body.main_file,label:'papers / '+body.project_id.slice(0,8)};savedDocument=savedRemote}
          else savedDocument=body.path===sourceDocument.path?sourceDocument:sibling;return{selected:savedDocument}}};
      window.preview=Preview.createPaperPreview({api,documentsApi,host:()=>document.querySelector('.chat-wrapper'),
        activeConversation:()=>activeConversation,translate:key=>labels[key]||key,loadPdfJs:async()=>pdfjs,debounceMs:1,pollMs:1});
      window.edit=id=>preview.observeChanges([{conversationId:activeConversation,changeId:id,source:'local',path:'main.tex'}]);
      preview.openDocument(activeConversation);
    }''',labels)
    panel=page.locator('#paperPreviewPanel')

    page.get_by_role('button',name='选择主文档',exact=True).click()
    page.get_by_role('combobox',name='选择主文档').select_option('0')
    page.wait_for_function("window.savedDocument?.path === 'main.tex'")
    assert page.evaluate('buildCount') == 0
    page.get_by_role('button',name='编译 PDF',exact=True).click()
    page.wait_for_function("document.querySelector('#paperPreviewPanel')?.dataset.buildId?.endsWith('1')")
    geometry=page.evaluate('''()=>{
      const chat=document.querySelector('.chat-container').getBoundingClientRect();
      const input=document.querySelector('.input-area').getBoundingClientRect();
      const paper=document.querySelector('#paperPreviewPanel').getBoundingClientRect();
      return{chatRight:chat.right,inputRight:input.right,paperLeft:paper.left,paperWidth:paper.width,
        overflow:document.documentElement.scrollWidth>innerWidth};
    }''')
    assert geometry['chatRight']<=geometry['paperLeft'] and geometry['inputRight']<=geometry['paperLeft']
    assert geometry['paperWidth'] > 1440 * 0.58 and not geometry['overflow']
    assert page.evaluate("localStorage.getItem('tofu_paper_preview_split')") is None
    assert panel.locator('.paper-preview-status').bounding_box()['width'] <= 1
    assert panel.locator('canvas').count()<20
    page.get_by_role('spinbutton',name='页码').fill('15')
    page.get_by_role('spinbutton',name='页码').press('Tab')
    page.wait_for_function("document.querySelector('.paper-preview-page-number').value==='15' && document.querySelector('#paperPreviewPanel [data-page=\"15\"] canvas')")
    page.get_by_role('textbox',name='缩放',exact=True).fill('125%')
    page.get_by_role('textbox',name='缩放',exact=True).press('Tab')
    assert page.get_by_role('spinbutton',name='页码').input_value()=='15'
    page.get_by_role('separator',name='调整论文分栏').focus()
    page.get_by_role('separator',name='调整论文分栏').press('ArrowLeft')
    assert panel.bounding_box()['width']>geometry['paperWidth']
    divider=page.get_by_role('separator',name='调整论文分栏').bounding_box()
    page.mouse.move(divider['x']+divider['width']/2,divider['y']+50)
    page.mouse.down();page.mouse.move(divider['x']-24,divider['y']+50);page.mouse.up()
    assert page.evaluate('document.body.style.userSelect')==''
    split_width = panel.bounding_box()['width']
    assert page.evaluate("JSON.parse(localStorage.getItem('tofu_paper_preview_layout')).chatPercent") < 40
    page.evaluate('''() => {
      window.readingViewport = document.querySelector('.paper-preview-viewport');
      const navigation = document.createElement('button'); navigation.id = 'globalNavigation';
      navigation.textContent = '项目导航'; document.body.append(navigation);
    }''')
    page.get_by_role('button',name='阅读模式',exact=True).click()
    page.wait_for_function("document.querySelector('#paperPreviewPanel').clientWidth===innerWidth")
    assert page.evaluate('''() => {
      const dialog = document.querySelector('dialog:modal');
      const box = dialog.getBoundingClientRect();
      document.getElementById('globalNavigation').focus();
      return box.x === 0 && box.y === 0 && box.width === innerWidth && box.height === innerHeight
        && dialog.contains(document.activeElement) && dialog.contains(readingViewport);
    }''')
    assert not page.get_by_role('textbox',name='输入消息').is_visible()
    assert page.get_by_role('button',name='退出阅读模式',exact=True).get_attribute('aria-pressed')=='true'
    assert page.get_by_role('spinbutton',name='页码').input_value()=='15'
    page.evaluate("edit('second')")
    page.wait_for_function("document.querySelector('#paperPreviewPanel').dataset.buildId.endsWith('2')")
    assert page.get_by_role('spinbutton',name='页码').input_value()=='15'
    assert page.get_by_role('button',name='退出阅读模式',exact=True).is_visible()
    panel.locator('.paper-preview-viewport').focus()
    panel.locator('.paper-preview-viewport').press('Escape')
    page.wait_for_function("!document.querySelector('.chat-wrapper[data-paper-preview-reading=true]')")
    assert page.evaluate("!document.querySelector('dialog') && readingViewport.parentElement.id === 'paperPreviewPanel'")
    assert abs(panel.bounding_box()['width']-split_width)<1
    assert page.get_by_role('button',name='阅读模式',exact=True).evaluate('(el)=>document.activeElement===el')
    page.locator('#globalNavigation').evaluate('(el) => {el.focus(); if (document.activeElement !== el) throw Error("background remained inert"); el.remove()}')
    assert page.get_by_role('spinbutton',name='页码').input_value()=='15'
    page.get_by_role('button',name='更多操作').click()
    page.get_by_role('button',name='跟随',exact=True).click()
    page.get_by_role('button',name='关闭',exact=True).click()
    page.evaluate("edit('third')")
    page.wait_for_function("document.querySelector('#paperPreviewPanel').dataset.buildId.endsWith('3') && document.querySelector('.paper-preview-page-number').value==='17'")
    page.get_by_role('button',name='更多操作').click()
    page.locator('.paper-preview-drawer').get_by_role('button',name='适应宽度').click()
    page.get_by_role('button',name='关闭',exact=True).click()
    artifact=ROOT/'output/playwright/paper-preview';artifact.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(artifact/'desktop.png'))
    page.get_by_role('button',name='搜索 PDF',exact=True).click()
    page.get_by_role('searchbox',name='搜索 PDF').fill('Page 17.')
    page.get_by_role('searchbox',name='搜索 PDF').press('Enter')
    page.wait_for_function("document.querySelectorAll('.paper-preview-drawer button').length>3")
    assert panel.locator('.paper-preview-drawer').inner_text().count('17 ·')>0
    page.get_by_role('button',name='关闭',exact=True).click()
    page.get_by_role('button',name='更多操作').click()
    page.get_by_role('button',name='目录',exact=True).click()
    page.get_by_role('button',name='Later chapter',exact=True).click()
    page.get_by_role('button',name='编译日志',exact=True).click()
    page.wait_for_function("document.querySelector('.paper-preview-drawer pre')")
    assert page.evaluate('window.unwanted') is None
    assert page.get_by_text('已显示日志开头。',exact=True).is_visible()
    log = page.get_by_role('link',name='下载完整日志',exact=True)
    assert log.get_attribute('href').endswith('/'+str(3).zfill(32)+'/artifacts/build.log')
    log_file = tmp_path/log.get_attribute('href').lstrip('/')
    log_file.parent.mkdir(parents=True,exist_ok=True)
    log_file.write_text('Full log\n'+'line\n'*8192)
    with page.expect_download() as downloaded:
        log.click()
    assert Path(downloaded.value.path()).read_text().endswith('line\n'*8192)
    page.get_by_role('button',name='关闭',exact=True).click()
    page.set_viewport_size({'width':390,'height':844})
    page.wait_for_function("document.querySelector('#paperPreviewPanel').clientWidth===390")
    page.wait_for_function("parseInt(document.querySelector('.paper-preview-zoom').value)<100")
    assert not page.get_by_role('textbox',name='输入消息').is_visible()
    assert page.get_by_role('tab',name='论文',exact=True).get_attribute('aria-selected')=='true'
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert page.get_by_role('textbox',name='缩放',exact=True).evaluate('(el) => el.clientWidth >= 54')
    assert panel.locator('.paper-preview-viewport').bounding_box()['y']<=100
    assert panel.get_attribute('role')=='tabpanel'
    assert page.get_by_role('tab',name='论文',exact=True).get_attribute('aria-controls')==panel.get_attribute('id')
    assert not page.get_by_role('combobox',name='选择主文档').is_visible()
    page.screenshot(path=str(artifact/'mobile.png'))
    page.get_by_role('button',name='选择主文档',exact=True).click()
    assert page.get_by_role('combobox',name='选择主文档').is_visible()
    page.get_by_role('combobox',name='选择主文档').press('Escape')
    assert page.get_by_role('button',name='选择主文档',exact=True).evaluate('(el)=>document.activeElement===el')
    page.set_viewport_size({'width':320,'height':740})
    page.wait_for_function("document.querySelector('#paperPreviewPanel').clientWidth===320")
    assert panel.locator('.paper-preview-viewport').bounding_box()['y']<=144
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    for label in ['搜索 PDF','阅读模式','更多操作']:
        box=page.get_by_role('button',name=label,exact=True).bounding_box()
        assert box['width']>=44 and box['height']>=44 and box['x']+box['width']<=320
    page.get_by_role('button',name='阅读模式',exact=True).click()
    assert not page.get_by_role('tab',name='聊天',exact=True).is_visible()
    assert panel.locator('.paper-preview-viewport').bounding_box()['y']<=96
    page.screenshot(path=str(artifact/'mobile-reading.png'))
    page.get_by_role('button',name='退出阅读模式',exact=True).click()
    mobile_page = page.get_by_role('spinbutton',name='页码').input_value()
    page.get_by_role('tab',name='聊天',exact=True).click()
    assert page.get_by_role('textbox',name='输入消息').is_visible()
    assert not panel.is_visible()
    page.get_by_role('tab',name='聊天',exact=True).press('Tab')
    assert page.get_by_role('textbox',name='输入消息').evaluate('(el)=>document.activeElement===el')
    page.get_by_role('tab',name='聊天',exact=True).press('ArrowRight')
    assert page.get_by_role('tab',name='论文',exact=True).get_attribute('aria-selected')=='true'
    page.wait_for_function('number=>document.querySelector(".paper-preview-page-number").value===number', arg=mobile_page)
    panel.locator('.paper-preview-viewport').focus()
    panel.locator('.paper-preview-viewport').press('Escape')
    page.wait_for_function("!document.querySelector('#paperPreviewPanel') && document.querySelectorAll('canvas').length === 0", timeout=5000)
    assert page.get_by_role('textbox',name='输入消息').evaluate('(el)=>document.activeElement===el')
    before=page.evaluate('buildCount');page.evaluate("edit('closed')")
    assert page.evaluate('buildCount')==before
    assert page.get_by_role('button',name='打开论文预览').is_visible()
    assert page.evaluate('viewerHolds.size') == 0
    page.get_by_role('button',name='打开论文预览').click()
    assert page.evaluate('buildCount') == before
    assert page.locator('#paperPreviewPanel canvas').count() == 0
    page.get_by_role('button',name='编译 PDF',exact=True).click()
    page.wait_for_function('buildCount>3 && document.querySelector("#paperPreviewPanel canvas")')
    # Overleaf uses the same selection/compile lifecycle, without invented host paths.
    before=page.evaluate('buildCount')
    page.get_by_role('button',name='选择主文档',exact=True).click()
    page.get_by_role('button',name='连接 Overleaf',exact=True).click()
    submit_bounds=page.get_by_role('button',name='使用此项目',exact=True).bounding_box()
    drawer_bounds=panel.locator('.paper-preview-drawer').bounding_box()
    assert submit_bounds and drawer_bounds
    assert submit_bounds['y']+submit_bounds['height'] <= drawer_bounds['y']+drawer_bounds['height']
    page.screenshot(path=str(artifact/'overleaf-selection.png'))
    assert page.get_by_label('连接',exact=True).input_value()=='papers'
    project=page.get_by_label('项目链接或 ID',exact=True)
    project.fill('not-a-project')
    page.get_by_role('button',name='使用此项目',exact=True).click()
    assert project.evaluate('(el)=>!el.validity.valid') and page.evaluate('savedRemote') is None
    project.fill('https://www.overleaf.com/project/'+'A'*24)
    page.get_by_role('button',name='使用此项目',exact=True).click()
    page.wait_for_function("savedDocument?.source==='overleaf' && !document.querySelector('#paperPreviewPanel canvas')")
    assert page.evaluate('buildCount')==before
    page.get_by_role('button',name='编译 PDF',exact=True).click()
    page.wait_for_function("document.querySelector('#paperPreviewPanel')?.dataset.documentId==='e'.repeat(64)")
    request=page.evaluate('lastBuild')
    assert request['source']=='overleaf' and request['mcp_server']=='papers' and request['project_id']=='a'*24
    assert not {'path','project_root','main_file'}.intersection(request)
    page.get_by_role('button',name='更多操作').click()
    assert page.get_by_role('button',name='跟随',exact=True).is_disabled()
    page.get_by_role('button',name='关闭',exact=True).click()
    page.screenshot(path=str(artifact/'overleaf-mobile.png'))
    # An unrelated local write cannot silently switch away from the remote selection.
    discovered=page.evaluate('discoveryCount')
    page.evaluate("edit('local-while-reading-overleaf')")
    page.wait_for_function(f'discoveryCount>{discovered}')
    assert page.evaluate('buildCount')==before+1
    assert page.locator('.paper-preview-status').inner_text()=='版本未验证'
    assert page.evaluate('viewerHolds.size') == 1
    # Expired server reservations cannot remain a silently "live" preview.
    page.evaluate("expireViewer=true;document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_function("document.querySelector('.paper-preview-status')?.textContent==='预览已过期'")
    assert page.locator('.paper-preview-notice').inner_text() == '预览已过期，请重新编译。'
    assert panel.locator('canvas').count() > 0
    assert page.evaluate('viewerHolds.size') == 0
    page.locator('.paper-preview-viewport').press('Escape')
    page.get_by_role('button',name='打开论文预览').click()
    page.wait_for_function("document.querySelector('.paper-preview-status')?.textContent==='可编译'")
    assert page.evaluate('buildCount')==before+1 and page.evaluate('savedDocument.source')=='overleaf'
    page.evaluate("activeConversation='conv-b';preview.activateConversation(activeConversation)")
    assert page.locator('#paperPreviewPanel').count()==0
    assert page.locator('canvas').count()==0
    page.evaluate('preview.destroy()')
    assert page.locator('.chat-container').get_attribute('role') is None
    assert page.locator('.chat-container').get_attribute('id') is None
    assert page.locator('.chat-wrapper').get_attribute('data-paper-preview-reading') is None
    assert not errors
