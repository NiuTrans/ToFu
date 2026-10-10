/* Actual reader/parse/sanitize/highlight/layout and native reading interaction. */
globalThis.createDomainReader = function (caseName) {
  const parser = new marked.Marked(); installMarkdownPolicy(parser);
  const renderer = createMarkdownRenderer({maximumCacheEntries: 30,
    getEnvironment: () => ({document, parser, sanitizer: DOMPurify, highlighter: readerHighlighter,
      mathRenderer: null, basePath: '', projectActive: false}), ensureMath() {}, reportFailure(error) {throw error;} });
  const box = document.createElement('div'); box.style.height='320px';box.style.overflow='auto';
  const node=document.createElement('section');node.className='conversation-text-block';box.append(node);document.body.append(box);
  const prefix=Array.from({length:160},(_,i)=>`Paragraph ${i}: **reader evidence** 中文 😀 [source](/source).`).join('\n\n')+'\n\n';
  let finalSource='', terminal=false, frames=0;
  function source(index) {
    if(caseName==='streaming_code'||caseName==='terminal_render')return prefix+'```js\nconst value = '+index+';\n'+(caseName==='terminal_render'?'```':'');
    if(caseName==='tables_and_references')return prefix+'| Name | Value |\n| --- | --- |\n| evidence | '+index+' |\n\n[tail][ref]\n\n[ref]: /source/'+index;
    return prefix+'Growing tail '+index+' **rich prose** and evidence.';
  }
  function render(text,done) {
    renderTextBlock(node,{kind:'text',blockId:'domain-reader',markdown:text,terminal:done},
      {renderSafeMarkdownHtml:(value,options)=>renderer.render(value,options)});
    node.getBoundingClientRect();
  }
  render(source(-1),false);
  return {
    operation(index) {
      const text=source(index);const done=caseName==='terminal_render';
      let selected='',scroll=0;
      if(caseName==='selection_and_scroll') {
        const target=node.querySelector('.md-content p').firstChild;
        const range=document.createRange();range.setStart(target,0);range.setEnd(target,9);
        const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
        selected=selection.toString();box.scrollTop=120;scroll=box.scrollTop;
      }
      const start=performance.now();
      if(done)render(text,false);
      render(text,done);
      const wall=performance.now()-start;
      if(caseName==='selection_and_scroll'&&(getSelection().toString()!==selected||box.scrollTop!==scroll))throw Error('reader selection/scroll was lost');
      finalSource=text;terminal=done;
      return wall;
    },
    oracle() {
      const actual=node.querySelector('.md-content').innerHTML;
      renderer.clearCache();const expected=document.createElement('div');expected.innerHTML=renderer.render(finalSource,{streaming:!terminal});
      if(actual!==expected.innerHTML)throw Error('noncanonical reader DOM');frames++;
      return frames;
    },
    close() {getSelection().removeAllRanges();renderer.dispose();box.remove();},
    source:()=>finalSource,
  };
};
