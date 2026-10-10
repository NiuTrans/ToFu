/** Run: node frontend/src/features/evolution/testing/office-motion.bench.cjs
 * CPU timings are diagnostic, not flaky CI thresholds. Deterministic operation
 * counts are asserted by office-motion.test.cjs. No images/browser/server.
 */
const {performance}=require('node:perf_hooks');
const {load}=require('./office-runtime-loader.cjs');
const {stepWalker}=load('office-motion');
const {findOfficePath}=load('office-navigation');
const {createOfficeCanvasSurface}=load('office-canvas');
function bench(name,iterations,fn){
  for(let i=0;i<100;i++)fn();
  const start=performance.now();for(let i=0;i<iterations;i++)fn();
  const ms=performance.now()-start;
  return {name,iterations,ms:+ms.toFixed(3),microsecondsPerOperation:+(ms*1000/iterations).toFixed(3)};
}
const actors=Array.from({length:11},()=>({x:0,y:0,path:[{x:1e9,y:0}],direction:2,frame:1,strideDistance:0}));
const context={setTransform(){},clearRect(){}};
const surface=createOfficeCanvasSurface({width:0,height:0,getContext:()=>context});surface.resize(960,2);
const results=[
  bench('11 actors: distance gait tick',100000,()=>actors.forEach(actor=>stepWalker(actor,1000/60))),
  bench('constrained door-to-seat route',5000,()=>findOfficePath({x:895,y:190},{x:250,y:334})),
  bench('unchanged snapshot paint gate',100000,()=>surface.paint('stable',()=>{})),
];
console.log(JSON.stringify({node:process.version,results,renderMetrics:surface.metrics(),bounds:{maxActors:11,maxBackingPixels:1440*1080,idleTimers:0,idleFrames:0,walkingFrameRateCap:30},scope:'CPU bookkeeping only; excludes image decode, rasterization and browser layout.'},null,2));
