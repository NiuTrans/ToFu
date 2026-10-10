const {test}=require('node:test');
const assert=require('node:assert/strict');
const {load}=require('./office-runtime-loader.cjs');
const {stepWalker,STRIDE_DISTANCE,seatPose,SEAT_TRANSITION_MS}=load('office-motion');
const {authoredFrame,spriteGeometry}=load('office-sprite-geometry');
const {GAME_FRAMES}=load('office-game-frames');
const {findOfficePath,walkableSegment}=load('office-navigation');
const {createPodMotion,targetPodMotion,stepPodMotion}=load('office-pod-motion');
const {createOfficeCanvasSurface}=load('office-canvas');
const walker=path=>({x:0,y:0,path,direction:0,frame:1,strideDistance:0});
const near=(a,b)=>assert.ok(Math.abs(a-b)<1e-7,`${a} ~= ${b}`);
test('all authored models repair the reverse-facing third side frame without mirroring costumes',()=>{
  for(const model of Object.keys(GAME_FRAMES))for(let direction=0;direction<4;direction++)for(let frame=0;frame<4;frame++){
    const source=authoredFrame(direction,frame);
    const actual=source.column===2&&[1,2].includes(source.row)?3-source.row:source.row;
    assert.equal(actual,direction,`${model} direction ${direction}, frame ${frame}`);
    assert.equal(source.column,frame);
    assert.ok(GAME_FRAMES[model][source.row][source.column][3]>0);
  }
});
test('gait depends on travelled distance, consumes corner remainder and stops on neutral',()=>{
  const route=[{x:10,y:0},{x:10,y:100}];
  const a=walker(structuredClone(route)),b=walker(structuredClone(route));
  stepWalker(a,200);
  stepWalker(b,100);stepWalker(b,100);
  near(a.x,10);near(a.y,9);near(a.strideDistance,19);
  near(a.x,b.x);near(a.y,b.y);assert.equal(a.frame,b.frame);
  const phase=a.strideDistance,frame=a.frame;stepWalker(a,0);near(a.strideDistance,phase);assert.equal(a.frame,frame);
  assert.equal(stepWalker(a,10000),true);assert.equal(a.frame,1);
  const end={x:a.x,y:a.y,distance:a.strideDistance};
  for(let i=0;i<100;i++)stepWalker(a,1000);
  assert.deepEqual({x:a.x,y:a.y,distance:a.strideDistance},end);
  const cycle=walker([{x:100,y:0}]);stepWalker(cycle,STRIDE_DISTANCE/95*1000);
  near(cycle.strideDistance,0);assert.equal(cycle.frame,1);
});
test('all authored frames preserve model scale and crop/lower continuously at a fixed foot anchor',()=>{
  for(const rows of Object.values(GAME_FRAMES)){
    const reference=rows[0][1][3];
    for(const row of rows)for(const bounds of row){
      const box=spriteGeometry(bounds,reference,205,334,76,0);
      near(box.y+box.height,334);
      near(box.width/bounds[2],76/reference);
      const halfway=spriteGeometry(bounds,reference,205,334,76,.5);
      const seated=spriteGeometry(bounds,reference,205,334,76,1);
      near(halfway.y,(box.y+seated.y)/2);near(halfway.height,(box.height+seated.height)/2);
    }
  }
  near(seatPose(0,1,0),0);near(seatPose(0,1,SEAT_TRANSITION_MS),1);
  near(seatPose(.4,0,0),.4);
});
test('routes retain exact endpoints and every segment avoids blocked corners',()=>{
  const origins=[{x:895,y:190},{x:430,y:355},{x:205,y:288},{x:750,y:288}];
  const destinations=[{x:205,y:334},{x:750,y:334},{x:245,y:350},{x:740,y:270},{x:178,y:330},{x:895,y:190}];
  for(const from of origins)for(const to of destinations){
    const route=findOfficePath(from,to);
    if(from.x===to.x&&from.y===to.y)continue;
    assert.deepEqual(route.at(-1),to);
    let previous=from;
    for(const point of route){assert.ok(walkableSegment(previous,point),JSON.stringify([previous,point]));previous=point;}
  }
  assert.deepEqual(findOfficePath({x:205,y:334},{x:207,y:334}),[{x:207,y:334}]);
  assert.deepEqual(findOfficePath({x:NaN,y:334},{x:205,y:334}),[]);
});
function finishPod(motion){
  for(let i=0;motion.current&&i<2000;i++){
    const before={x:motion.x,y:motion.y,seat:motion.seatAmount};stepPodMotion(motion,20);
    assert.ok(Math.hypot(motion.x-before.x,motion.y-before.y)<=3.5,'bounded foot delta');
    assert.ok(Math.abs(motion.seatAmount-before.seat)<=.12,'bounded pose delta');
    if(!motion.path.length)assert.equal(motion.frame,1);
  }
  assert.equal(motion.current,undefined);
}
test('pod approach, turn, sit, roll and stand preserve position even on mid-beat reversal',()=>{
  const motion=createPodMotion('pending');targetPodMotion(motion,'running');
  const phases=new Set();
  for(let i=0;motion.current&&i<2000;i++){phases.add(motion.current.kind);stepPodMotion(motion,20);}
  assert.deepEqual([...phases].sort(),['chair','face','seat','walk']);
  assert.equal(motion.seatAmount,1);near(motion.x,480);near(motion.y,478);assert.equal(motion.frame,1);
  targetPodMotion(motion,'done');stepPodMotion(motion,100);
  const before={x:motion.x,y:motion.y,seat:motion.seatAmount,chair:motion.chairY};
  targetPodMotion(motion,'running');
  assert.deepEqual({x:motion.x,y:motion.y,seat:motion.seatAmount,chair:motion.chairY},before);
  finishPod(motion);near(motion.y,478);
  targetPodMotion(motion,'done');finishPod(motion);near(motion.x,675);near(motion.y,610);assert.equal(motion.seatAmount,0);
  targetPodMotion(motion,'running',false);assert.equal(motion.current,undefined);assert.equal(motion.seatAmount,1);
});
test('dirty canvas skips identical paints, bounds DPR/resizes and refuses late paints after disposal',()=>{
  let clears=0,draws=0;const context={setTransform(){},clearRect(){clears++;}};
  const canvas={width:0,height:0,getContext:()=>context};
  const surface=createOfficeCanvasSurface(canvas);
  assert.equal(surface.resize(960,8),true);assert.equal(surface.resize(960,8),false);
  assert.equal(canvas.width*canvas.height,1440*1080);
  for(let i=0;i<1000;i++)surface.paint('same',()=>draws++);
  assert.equal(draws,1);assert.equal(clears,1);assert.equal(surface.metrics().skippedPaints,999);
  surface.invalidate();surface.paint('same',()=>draws++);assert.equal(draws,2);
  surface.dispose();surface.dispose();surface.paint('late',()=>draws++);surface.resize(960,2);
  assert.equal(draws,2);assert.equal(canvas.width*canvas.height,0);
});
