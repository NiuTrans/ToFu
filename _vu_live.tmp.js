const fs=require('fs');
global.escapeHtml=(s)=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t=(k,d)=>(d||k); global.Icon=()=>'<I>'; global.renderMarkdown=(s)=>s;
global._shortUrl=(u)=>u; global.formatNumber=(n)=>String(n);
global.window={location:{href:'http://x/'},addEventListener(){},removeEventListener(){}};
global.document={addEventListener(){},removeEventListener(){},createElement:()=>({style:{},setAttribute(){},appendChild(){}})};
eval(fs.readFileSync('static/js/ui/tool_rounds.js','utf8'));
// A run_command STILL RUNNING (device-code login blocks waiting for the scan),
// with the QR already streamed into _partialOutput AND qrImages attached.
const art = Array.from({length:20},()=>'██  ██  ██████').join('\n');
const running={roundNum:5,toolName:'run_command',status:'searching',query:'gh auth login',
  _partialOutput:'Scan the QR:\n'+art+'\nWaiting for confirmation...',
  results:[{toolName:'run_command',command:'gh auth login',
            qrImages:[{uri:'data:image/png;base64,QQQQ',format:'png',filename:'qr.png'}]}]};
const html=_renderUnifiedToolLine(running,true);
console.log('LIVE(running) has qr strip? ->', html.includes('ptool-qr-strip'));
console.log('LIVE(running) has <img>?    ->', html.includes('<img'));
console.log('LIVE pane class present?    ->', html.includes('ptool-cmd-output-live'));
