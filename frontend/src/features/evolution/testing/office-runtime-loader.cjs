/** Asset-free loader for retained scene modules; no browser or decoded sheets. */
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
const cache=new Map();
function load(name,parent=path.resolve(__dirname,'..','index.ts')){
  if(/\.(png|webp)$/.test(name))return {default:'/test-art.webp'};

  if(name==='./components/ui')return {esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')};
  const full=path.resolve(path.dirname(parent),name+'.ts');
  if(cache.has(full))return cache.get(full);
  const exports={};cache.set(full,exports);
  const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  new Function('require','exports',js)(name=>load(name,full),exports);
  return exports;
}
module.exports={load};
