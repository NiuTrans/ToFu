import { describe, expect, it } from 'vitest';
import { projectRestartHealth, restartPhaseProgress } from './restart-observation';

describe('restart health authority', () => {
  it('does not mistake the accepting process or an early listener for completion', () => {
    expect(projectRestartHealth({ok:true, bootId:'old', boot:{status:'ready'}}, 'old')).toEqual({kind:'old'});
    expect(projectRestartHealth({ok:true, bootId:'new', boot:{status:'running'}}, 'old')).toMatchObject({kind:'booting'});
    expect(projectRestartHealth({ok:true, bootId:'new', boot:{status:'ready'}}, 'old')).toMatchObject({kind:'ready'});
  });
  it('surfaces a new boot failure and adopts a boot already running on click', () => {
    const boot = {status:'failed', error:'storage corrupt'};
    expect(projectRestartHealth({ok:true, bootId:'new', boot}, 'old')).toEqual({kind:'failed',boot});
    expect(projectRestartHealth({ok:true, bootId:'new', boot:{status:'running'}}, null)).toMatchObject({kind:'booting'});
    expect(projectRestartHealth(null, 'old')).toEqual({kind:'unavailable'});
  });
  it('keeps older health endpoints usable', () => {
    expect(projectRestartHealth({ok:true}, null)).toMatchObject({kind:'ready'});
  });
  it('projects the running phase without extrapolating to a later phase', () => {
    expect(restartPhaseProgress({status:'running',phaseTotal:9,currentPhase:{index:3,key:'storage_sidecar'}})).toEqual({key:'storage_sidecar',pct:20+76*2/9});
    expect(restartPhaseProgress({status:'running',phaseTotal:9,currentPhase:{index:10,key:'readiness'}})).toBeNull();
  });
});
