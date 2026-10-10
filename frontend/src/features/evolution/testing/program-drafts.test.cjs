/** Browser-owned draft boundaries: identity, complete writes and late ACKs. */
const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
const exportsForDrafts={};
new Function('exports',ts.transpileModule(fs.readFileSync(path.join(__dirname,'../programs/drafts.ts'),'utf8'),
 {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText)(exportsForDrafts);
const {createProgramDraftStore,PROGRAM_DRAFT_LIMITS}=exportsForDrafts;
function fixture(){const values=new Map();let owner=7,throwWrite=false;
 const storage={getItem:key=>values.get(key)||null,setItem:(key,value)=>{if(throwWrite)throw Error('quota');values.set(key,value)},removeItem:key=>values.delete(key)};
 const open=(scope='/app/')=>createProgramDraftStore({owner:()=>owner,scope,storage:()=>storage});
 return {values,storage,open,owner:value=>owner=value,fail:value=>throwWrite=value};}
const saved={fields:['真实问题\n完整草稿'],attempt:{signature:'exact immutable body',key:'original-key',createdId:'original-program'}};

test('exact Unicode and ambiguous request identity survive a fresh store',()=>{
 const env=fixture();assert.equal(env.open().save(['create'],saved),'saved');
 const restored=env.open().read(['create']);assert.deepEqual(restored,saved);
 restored.fields[0]='Modified copy';assert.deepEqual(env.open().read(['create']),saved);
});
test('unknown identities never read or write another account and a mounted owner is pinned',()=>{
 const env=fixture(),current=env.open();current.save(['create'],saved);const raw=[...env.values.values()][0];
 for(const owner of [null,0,'7',8]){env.owner(owner);assert.equal(current.read(['create']),undefined);assert.equal(current.save(['create'],{fields:['Wrong owner']}),'unavailable');}
 assert.equal([...env.values.values()][0],raw);env.owner(8);assert.equal(env.open().read(['create']),undefined);
 env.owner(7);assert.deepEqual(current.read(['create']),saved);assert.equal(env.open('/other-app/').read(['create']),undefined);
});
test('shared storage merges different forms and rejects a late acknowledgement of changed input',()=>{
 const env=fixture(),first=env.open(),second=env.open();first.save(['create'],saved);second.save(['expand','a'],{fields:['Other input']});
 assert.deepEqual(first.read(['expand','a']),{fields:['Other input']});
 const newer={fields:['A later original objective'],attempt:{signature:'new body',key:'new-key'}};second.save(['create'],newer);
 assert.equal(first.clear(['create'],saved),false);assert.deepEqual(first.read(['create']),newer);
 assert.equal(first.clear(['expand','a'],{fields:['Other input']}),true);assert.equal(second.read(['expand','a']),undefined);
});
test('write failure retains the original bytes and exposes failure rather than saved',()=>{
 const env=fixture(),store=env.open();store.save(['create'],saved);const raw=[...env.values.values()][0];env.fail(true);
 assert.equal(store.save(['create'],{fields:['Uncommitted input']}),'unavailable');assert.equal(store.clear(['create']),false);
 assert.equal([...env.values.values()][0],raw);assert.deepEqual(env.open().read(['create']),saved);
});
test('invalid and oversized envelopes are preserved and never treated as restored input',()=>{
 for(const raw of ['not-json',JSON.stringify({version:2,drafts:{}}),JSON.stringify({version:1,drafts:{'not-an-identity':saved}}),' '.repeat(PROGRAM_DRAFT_LIMITS.bytes+1)]){
  const env=fixture();env.open().save(['create'],saved);const key=[...env.values.keys()][0];env.values.set(key,raw);
  const store=env.open();assert.equal(store.read(['create']),undefined);assert.equal(store.save(['create'],saved),'unavailable');assert.equal(env.values.get(key),raw);
 }
});
test('entry and byte bounds reject a whole write without truncating or evicting older unsent input',()=>{
 const env=fixture(),store=env.open();
 for(let n=0;n<PROGRAM_DRAFT_LIMITS.entries;n++)assert.equal(store.save(['expand',String(n)],{fields:['Original '+n]}),'saved');
 const raw=[...env.values.values()][0];assert.equal(store.save(['expand','overflow'],saved),'full');assert.equal([...env.values.values()][0],raw);
 assert.equal(store.read(['expand','0']).fields[0],'Original 0');
 const big=fixture(),large=big.open();const value='中'.repeat(10000);
 for(let n=0;n<17;n++)assert.equal(large.save(['expand',String(n)],{fields:[value]}),'saved');
 const before=[...big.values.values()][0];assert.equal(large.save(['expand','18'],{fields:[value]}),'full');assert.equal([...big.values.values()][0],before);
 assert.equal(large.read(['expand','0']).fields[0],value);
});
test('a missing capability or throwing storage getter fails soft',()=>{
 for(const storage of [()=>undefined,()=>{throw Error('storage disabled')}]){
  const store=createProgramDraftStore({owner:()=>7,scope:'/app/',storage});assert.equal(store.read(['create']),undefined);assert.equal(store.save(['create'],saved),'unavailable');
 }
});
