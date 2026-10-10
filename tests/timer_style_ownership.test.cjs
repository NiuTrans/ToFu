/** Timer styles own their paints and consume declared vocabulary, including geometry. */
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),postcss=require('postcss');
const root=path.resolve(__dirname,'../frontend/src/styles/application');
const manifest=JSON.parse(fs.readFileSync(path.join(root,'manifest.json'),'utf8'));
const rows=[...manifest.sections,...manifest.deferredSections];
const timerFiles=rows.filter(row=>row.path.startsWith('08-timer-receipt-')).map(row=>row.path);
const parse=name=>postcss.parse(fs.readFileSync(path.join(root,name),'utf8'),{from:name});

test('Timer owners consume declared shared tokens and have no per-theme patches',()=>{
  const defined=new Set();
  for(const row of manifest.sections)parse(row.path).walkDecls(decl=>{if(decl.prop.startsWith('--'))defined.add(decl.prop);});
  assert.equal(timerFiles.length,3);
  for(const name of timerFiles){
    const css=parse(name);
    css.walkDecls(decl=>{
      for(const match of decl.value.matchAll(/var\((--[\w-]+)/g))assert.ok(defined.has(match[1]),`${name}: undeclared ${match[1]}`);
    });
    css.walkRules(rule=>assert.ok(!rule.selector.includes('[data-theme'),`${name}: theme-specific patch ${rule.selector}`));
  }
});

test('Timer paints cannot regain a second owner through copied theme or other component rules',()=>{
  const allowed=new Set([...timerFiles,'23-reading-activity.css']); // Shared SVG/focus/disclosure geometry.
  for(const row of rows){
    if(allowed.has(row.path))continue;
    parse(row.path).walkRules(rule=>assert.ok(!/\.timer-(?:watcher|poll|meta|id-chip|id-txt|kind|next-)/.test(rule.selector),
      `${row.path}: Timer rule belongs to the receipt owner: ${rule.selector}`));
  }
});
