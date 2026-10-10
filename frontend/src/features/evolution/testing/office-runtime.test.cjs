/** Executable specifications for the pixel-office runtime's pure primitives. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
const cache=new Map();
function load(name,parent=path.resolve(__dirname,'..','index.ts')){
 const full=path.resolve(path.dirname(parent),name+'.ts');
 if(cache.has(full))return cache.get(full);
 const exports={};cache.set(full,exports);
 const js=ts.transpileModule(fs.readFileSync(full,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('require','exports',js)(name=>load(name,full),exports);return exports;
}
const {stepWalker,seatPose,STRIDE_DISTANCE,WALK_FRAMES,SEAT_TRANSITION_MS}=load('office-motion');
const {authoredFrame,spriteGeometry}=load('office-sprite-geometry');
const {GAME_FRAMES}=load('office-game-frames');
const {findOfficePath,walkableSegment}=load('office-navigation');
const {createOfficeCanvasSurface}=load('office-canvas');
const near=(a,b)=>assert.ok(Math.abs(a-b)<1e-7,`${a} != ${b}`);
const walker=()=>({x:430,y:350,path:[{x:450,y:350},{x:450,y:470},{x:600,y:470}],direction:0,frame:1,strideDistance:0});
test('distance gait preserves cardinal direction and consumes corner leftovers independently of frame cadence',()=>{
 const a=walker(),b=walker();stepWalker(a,1000);
 for(let i=0;i<20;i++)stepWalker(b,50);
 for(const key of ['x','y','direction','frame','strideDistance'])near(a[key],b[key]);
 near(a.x,450);near(a.y,425);assert.equal(a.direction,0);
 for(const [dx,dy,direction] of [[-100,0,1],[100,0,2],[0,-100,3],[0,100,0]]){
   const w={...walker(),x:0,y:0,path:[{x:dx,y:dy}]};stepWalker(w,100);assert.equal(w.direction,direction);
 }
 const w=walker(),seen=[];
 for(let i=0;i<4;i++){stepWalker(w,STRIDE_DISTANCE/4*1000/95);seen.push(w.frame);}
 assert.deepEqual(seen,[WALK_FRAMES[1],WALK_FRAMES[2],WALK_FRAMES[3],WALK_FRAMES[0]]);
});
test('arrival fires once and zero/invalid elapsed time preserves a walking frame',()=>{
 const w=walker();stepWalker(w,200);const snapshot=structuredClone(w);
 for(const dt of [0,-10,NaN,Infinity]){assert.equal(stepWalker(w,dt),false);assert.deepEqual(w,snapshot);}
 assert.equal(stepWalker(w,100000),true);assert.deepEqual([w.x,w.y,w.frame],[600,470,1]);
 assert.equal(stepWalker(w,100000),false);
});
test('all authored model frames keep one scale and planted foot anchor, without side-facing frame reversal',()=>{
 for(const frames of Object.values(GAME_FRAMES))for(let direction=0;direction<4;direction++)for(let frame=0;frame<4;frame++){
   const {row,column}=authoredFrame(direction,frame);
   assert.equal(column,frame);assert.equal(row,frame===2&&[1,2].includes(direction)?3-direction:direction);
   const bounds=frames[row][column],height=76,reference=frames[0][1][3];
   const box=spriteGeometry(bounds,reference,205,288,height);
   near(box.y+box.height,288);near(box.x+box.width/2,205);
   near(box.width/bounds[2],height/reference);near(box.height/bounds[3],height/reference);
   const seated=spriteGeometry(bounds,reference,205,288,height,1);
   near(seated.width,box.width);near(seated.y-box.y,height*.10);
   near(seated.height/box.height,.64);
 }
});
test('sit, stand and interrupted reversal are continuous eased transitions',()=>{
 for(const [from,to] of [[0,1],[1,0],[.43,0],[.43,1]]){
   near(seatPose(from,to,0),from);near(seatPose(from,to,SEAT_TRANSITION_MS),to);
   near(seatPose(from,to,-10),from);near(seatPose(from,to,10000),to);
   const samples=Array.from({length:27},(_,i)=>seatPose(from,to,i*10));
   for(let i=1;i<samples.length;i++){
     assert.ok((samples[i]-samples[i-1])*(to-from)>=0);
     assert.ok(Math.abs(samples[i]-samples[i-1])<.06);
   }
   const midway=seatPose(from,to,110);near(seatPose(midway,from,0),midway);
 }
});
test('routes preserve exact non-grid endpoints and every interpolated segment clears furniture',()=>{
 const points=[{x:430,y:355},{x:895,y:190},{x:205,y:288},{x:750,y:288},{x:178,y:330},
   {x:245,y:350},{x:205,y:334},{x:790,y:350},{x:750,y:334},{x:740,y:270},
   {x:250,y:270},{x:230,y:350},{x:810,y:290},{x:440,y:590},{x:490,y:590},{x:530,y:590},
   {x:480,y:470},{x:560,y:470},{x:400,y:470},{x:480,y:530},{x:560,y:530},{x:650,y:450},{x:300,y:360},{x:700,y:350}];
 for(const from of points)for(const to of points){
   const route=findOfficePath(from,to);
   if(from===to){assert.deepEqual(route,[]);continue;}
   assert.ok(route.length);assert.deepEqual(route.at(-1),to);
   let previous=from;for(const point of route){assert.ok(walkableSegment(previous,point),JSON.stringify([previous,point]));previous=point;}
 }
 for(const to of [{x:630,y:500},{x:NaN,y:300},{x:500,y:Infinity}])assert.deepEqual(findOfficePath(points[0],to),[]);
 assert.equal(walkableSegment({x:430,y:350},{x:450,y:370}),false);
});
test('dirty canvas invalidates on decode/resize, caps backing pixels and disposes idempotently',()=>{
 let clears=0,draws=0;
 const ctx={setTransform(){},clearRect(){clears++;},imageSmoothingEnabled:true};
 const canvas={width:300,height:150,getContext:()=>ctx};
 const surface=createOfficeCanvasSurface(canvas);
 assert.equal(surface.resize(4000,4),true);assert.equal(canvas.width*canvas.height,1440*1080);
 assert.equal(ctx.imageSmoothingEnabled,false);assert.equal(surface.resize(4000,4),false);
 const draw=()=>draws++;
 assert.equal(surface.paint('stationary',draw),true);assert.equal(surface.paint('stationary',draw),false);
 surface.invalidate();assert.equal(surface.paint('stationary',draw),true);
 assert.equal(surface.resize(390,1),true);assert.equal(surface.paint('stationary',draw),true);
 assert.equal(clears,3);assert.equal(draws,3);
 assert.deepEqual(surface.metrics(),{paintRequests:4,paints:3,skippedPaints:1,resizes:2,backingPixels:390*293});
 surface.dispose();surface.dispose();assert.equal(canvas.width+canvas.height,0);
 assert.equal(surface.paint('new',draw),false);assert.equal(surface.resize(640,2),false);
 assert.equal(surface.metrics().backingPixels,0);assert.equal(draws,3);
});
