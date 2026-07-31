const fs=require('fs');
global.escapeHtml=(s)=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t=(k,d)=>(d||k); global.Icon=()=>'<I>'; global.renderMarkdown=(s)=>s;
global._shortUrl=(u)=>u; global.formatNumber=(n)=>String(n);
global.window={location:{href:'http://x/'},addEventListener(){},removeEventListener(){}};
global.document={addEventListener(){},removeEventListener(){},createElement:()=>({style:{},setAttribute(){},appendChild(){}})};
eval(fs.readFileSync('static/js/ui/tool_rounds.js','utf8'));
const real=JSON.parse(fs.readFileSync('/tmp/live_qr.json','utf8'));
const running={roundNum:7,toolName:'run_command',status:'searching',query:'gh auth login',
  _partialOutput:'Waiting for you to scan...', qrImages:real, results:[]};
const html=_renderUnifiedToolLine(running,true);
console.log('RUNNING has qr strip  ->', html.includes('ptool-qr-strip'));
console.log('RUNNING has <img>     ->', html.includes('<img'));
const qrAt=html.indexOf('ptool-qr-strip'), liveAt=html.indexOf('ptool-cmd-output-live');
console.log('QR BEFORE live pane   ->', qrAt>=0 && liveAt>=0 && qrAt<liveAt);
console.log('QR outside live <pre> ->', !html.slice(liveAt).includes('ptool-qr'));
// extract the src the browser would load, and decode it for real
const m=html.match(/<img src="(data:image\/png;base64,[^"]+)"/);
fs.writeFileSync('/tmp/fe_src.txt', m?m[1]:'');
console.log('img src extracted     ->', !!m);
// no-QR running round must stay byte-identical to before
const plain={roundNum:7,toolName:'run_command',status:'searching',query:'ls',_partialOutput:'a\nb',results:[]};
console.log('no-QR running clean   ->', !_renderUnifiedToolLine(plain,true).includes('ptool-qr'));
