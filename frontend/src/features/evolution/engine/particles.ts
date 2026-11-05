/** Short-lived world-space particle effects for discrete game events. Each
 * burst spawns a fixed authored set whose members expire, so the pool is
 * bounded by the events that created it. Visible-time stepping only.
 * Entry: createParticles. No DOM, timers or assets.
 */
import { PALETTE } from './paint';

export type BurstKind = 'sparkle'|'paper'|'puff';
type Particle = {x:number;y:number;vx:number;vy:number;age:number;life:number;size:number;color:string;spin:number;kind:BurstKind};
const BURSTS:Record<BurstKind,{count:number;life:number;speed:number;gravity:number;colors:string[];size:number}> = {
  sparkle:{count:14,life:900,speed:70,gravity:-20,colors:[PALETTE.gold,PALETTE.cream,'#ffe39a'],size:3},
  paper:{count:8,life:1100,speed:45,gravity:40,colors:[PALETTE.paper,'#efe2c4'],size:4},
  puff:{count:7,life:1000,speed:22,gravity:-18,colors:['rgba(120,110,100,0.55)','rgba(160,150,140,0.45)'],size:6},
};

export function createParticles(random:()=>number=Math.random) {
  let particles:Particle[]=[];
  return {
    burst(kind:BurstKind,x:number,y:number){
      const spec=BURSTS[kind];
      for(let i=0;i<spec.count;i++){
        const angle=random()*Math.PI*2,speed=spec.speed*(.45+random()*.55);
        particles.push({x,y,vx:Math.cos(angle)*speed,vy:Math.sin(angle)*speed-spec.speed*.4,age:0,
          life:spec.life*(.7+random()*.3),size:spec.size*(.7+random()*.6),color:spec.colors[i%spec.colors.length],spin:random()*Math.PI,kind});
      }
    },
    step(dtMs:number){
      if(!particles.length)return;
      const dt=dtMs/1000;
      for(const particle of particles){
        particle.age+=dtMs;particle.x+=particle.vx*dt;particle.y+=particle.vy*dt;
        particle.vy+=BURSTS[particle.kind].gravity*dt;particle.vx*=.985;particle.spin+=dt*4;
      }
      particles=particles.filter(particle=>particle.age<particle.life);
    },
    draw(ctx:CanvasRenderingContext2D){
      for(const particle of particles){
        const t=particle.age/particle.life;
        ctx.save();ctx.globalAlpha=Math.max(0,1-t);ctx.fillStyle=particle.color;ctx.translate(particle.x,particle.y);
        if(particle.kind==='sparkle'){
          ctx.rotate(particle.spin);const s=particle.size*(1-t*.4);
          ctx.beginPath();ctx.moveTo(0,-s*1.8);ctx.lineTo(s*.5,0);ctx.lineTo(0,s*1.8);ctx.lineTo(-s*.5,0);ctx.closePath();ctx.fill();
          ctx.beginPath();ctx.moveTo(-s*1.8,0);ctx.lineTo(0,s*.5);ctx.lineTo(s*1.8,0);ctx.lineTo(0,-s*.5);ctx.closePath();ctx.fill();
        }else if(particle.kind==='paper'){
          ctx.rotate(particle.spin);ctx.fillRect(-particle.size/2,-particle.size*.7,particle.size,particle.size*1.4);
        }else{
          ctx.beginPath();ctx.arc(0,0,particle.size*(1+t*1.5),0,Math.PI*2);ctx.fill();
        }
        ctx.restore();
      }
    },
    active(){return particles.length>0;},
    clear(){particles=[];},
  };
}
