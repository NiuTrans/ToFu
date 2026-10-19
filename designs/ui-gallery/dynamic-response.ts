/** Real response renderer with explicitly illustrative data; no task writes. */
import {renderRichResponse,disposeRichResponse} from '../../frontend/src/rich-response/renderer';
import {createButton} from '../../frontend/src/ui';
import {mountResourceLauncher} from '../../frontend/src/rich-response/resource-launcher';
import type {RichResponse} from '../../frontend/src/rich-response/contract.generated';
import {setLanguage,t,type I18nKey} from '../../frontend/src/i18n';

await setLanguage('zh');
const payload=await fetch('./dynamic-response.fixture.json').then(response=>response.json()) as RichResponse;
const host=document.querySelector<HTMLElement>('#rich-preview')!;
const actions=document.querySelector<HTMLElement>('#gallery-actions')!;
const runId=new URLSearchParams(location.search).get('runId');
const ports={surfaceContext:runId?{runId}:undefined,text:(key:string,fallback:string)=>t(key as I18nKey)||fallback};
function draw(){renderRichResponse(host,payload,ports);}
for(const [mode,label] of [['tabs','分页浏览'],['grid','并列比较'],['stack','顺序阅读']] as const){
  actions.append(createButton({label,size:'sm',onClick:()=>{payload.layout!.mode=mode;draw();}}));
}
const launcherHost=document.createElement('section');host.after(launcherHost);
const launcher=mountResourceLauncher(launcherHost,ports.text);launcher.update(ports.surfaceContext);
draw();window.addEventListener('pagehide',()=>{disposeRichResponse(host);launcher.dispose();},{once:true});
