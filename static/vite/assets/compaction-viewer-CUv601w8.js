import{f as L,k as re}from"./application-B_fNhrwW.js";import{_ as O,t as F}from"./index-ClLXN_Gt.js";import{i as ae}from"./runtime-bridge-ENj8TeFq.js";import"./main-BkwjM1xd.js";/* empty css              */import"./transport-gA3NJcwc.js";import"./marked.esm-CiNnXsiK.js";import"./action-registry-BG2lWHJ2.js";function de(e,t){return e.role==="user"&&e._isMeta===!0&&e._compactionHandoff?.version==="tofu.compaction-handoff/v1"&&["pre_turn","mid_turn"].includes(e._compactionHandoff?.phase)&&typeof e.content=="string"?t:e.role==="assistant"&&e._isCompactionSummary===!0?"summary":e._isMeta===!0&&e._retainedUserHistoryVersion===2&&Array.isArray(e._retainedUserHistory)?"history":e._isMeta===!0?"runtime":"unknown"}function le(e,t={}){const o=t.receipt?.implementation==="deterministic_recovery_receipt"?"recovery":"summary",i=e.map(s=>de(s||{},o));for(let s=0;s<e.length;s++){const r=e[s],d=r?.tool_calls;if(r?.role!=="assistant"||!Array.isArray(d)||!d.length)continue;let p=s+1;for(;p<e.length&&e[p]?.role==="tool";)p++;let y=d.length===p-s-1;const f=new Map;if(y)for(const m of d){const w=typeof m?.id=="string"?m.id.trim():"";if(!w||m?.function?.name!=="context_compact"){y=!1;break}f.set(w,(f.get(w)||0)+1)}for(let m=s+1;y&&m<p;m++){const w=e[m].tool_call_id,v=typeof w=="string"?w.trim():"",h=f.get(v)||0;h?f.set(v,h-1):y=!1}if(y)for(let m=s+1;m<p;m++)i[m]=o;s=p-1}return i}function K(e,t,o){return{archive:e,view:"post",postSnapshot:o??{stage:"compaction_result",status:Array.isArray(t)?"captured":"unavailable",includesRuntimeRecomposition:!1,includesProviderAdaptation:!1},postMessages:Array.isArray(t)?t:null}}function pe(e){const t=e.querySelector('[data-compaction-kind="summary"], [data-compaction-kind="recovery"]');t&&(t.tabIndex=-1,t.focus({preventScroll:!0}),t.scrollIntoView?.({block:"nearest",behavior:"auto"}))}const T=L.Api;if(!T||typeof T!="object")throw new Error("compaction-viewer-presenters runtime dependency is unavailable: Api");const E=L.CompactionHistoryState;if(!E||typeof E!="object")throw new Error("compaction-viewer-presenters runtime dependency is unavailable: CompactionHistoryState");const $=L.showToast;if(typeof $!="function")throw new Error("compaction-viewer-presenters runtime dependency is unavailable: showToast");const n=re,M=e=>(e=Number(e)||0,e>=1e6?(e/1e6).toFixed(2)+"M":e>=1e3?(e/1e3).toFixed(1)+"k":String(e)),C=e=>(e=Number(e)||0,e>=1024*1024?(e/1024/1024).toFixed(1)+" MB":e>=1024?(e/1024).toFixed(1)+" KB":e+" B"),z=e=>{try{const t=Number(e);if(!Number.isFinite(t))return String(e);const o=t<1e12?t*1e3:t;return new Date(o).toLocaleString()}catch{return String(e)}},_=e=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px">${e}</svg>`,c=(e,t)=>typeof F=="function"?F(e,t):t?e.replace(/\{(\w+)\}/g,(o,i)=>i in t?t[i]:"{"+i+"}"):e,me={working_set:_('<path d="M4 6h16M4 12h16M4 18h10"/><circle cx="18" cy="18" r="2"/>'),window:_('<rect width="18" height="14" x="3" y="5" rx="2"/><path d="M3 9h18"/>'),force:_('<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>'),reactive:_('<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>'),manual:_('<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/>')},J=e=>{const t=me[e];return t?t+" "+c("compactionViewer.trigger."+e):n(e)},ue=2,P=8*1024*1024,g=new Map;let A=0,D=null;function ye(e){const t=g.get(e);return t?(g.delete(e),g.set(e,t),t.payload):null}function fe(e,t,o){Number(t?.archive?.payloadSize);const i=JSON.stringify(t||{}).length*2;if(i>P)return;const s=g.get(e);for(s&&(A-=s.bytes,g.delete(e));g.size>=ue||A+i>P;){const r=g.keys().next().value;if(r===void 0)break;const d=g.get(r);A-=d?.bytes||0,g.delete(r)}g.set(e,{payload:t,bytes:i}),A+=i}function k(){let e=document.getElementById("compactionViewerDrawer");return e||(e=document.createElement("div"),e.id="compactionViewerDrawer",e.className="compaction-drawer",e.setAttribute("role","dialog"),e.setAttribute("aria-labelledby","compactionViewerTitle"),e.setAttribute("aria-hidden","true"),e.innerHTML=`
      <div class="compaction-drawer-backdrop" data-close></div>
      <aside class="compaction-drawer-panel">
        <header class="compaction-drawer-header">
          <div class="compaction-drawer-title-row">
            <h3 id="compactionViewerTitle" data-i18n="compactionViewer.title">压缩前的上下文快照</h3>
            <button type="button" class="compaction-drawer-close" data-close
                    aria-label="Close">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" stroke-width="2"
                   stroke-linecap="round" stroke-linejoin="round">
                <path d="M18 6 6 18M6 6l12 12"/></svg>
            </button>
          </div>
          <p class="compaction-drawer-subtitle" data-i18n-html="compactionViewer.subtitle">
            这里展示的是已存储的<strong>压缩结果阶段</strong>消息，尚不包含后续运行时重组和供应商适配；不是首次 API 请求或最终传输内容。
          </p>
          <div class="compaction-drawer-meta"></div>
          <div class="compaction-drawer-tabs" role="tablist">
            <button type="button" class="compaction-tab is-active"
                    data-tab="messages" role="tab" data-i18n="compactionViewer.tabMessages">上下文消息</button>
            <button type="button" class="compaction-tab"
                    data-tab="summary"  role="tab" data-i18n="compactionViewer.tabSummary">压缩结果摘要</button>
            <button type="button" class="compaction-tab"
                    data-tab="history"  role="tab" data-i18n="compactionViewer.tabHistory">该会话全部快照</button>
          </div>
        </header>
        <div class="compaction-drawer-body">
          <div class="compaction-drawer-loading" data-i18n="compactionViewer.loading">加载中…</div>
          <div class="compaction-drawer-content"></div>
        </div>
        <footer class="compaction-drawer-footer">
          <button type="button" class="compaction-drawer-btn" data-action="copy-json" data-i18n="compactionViewer.copyJson">
            复制原始 JSON
          </button>
          <button type="button" class="compaction-drawer-btn" data-action="download" data-i18n="compactionViewer.download">
            下载完整快照
          </button>
        </footer>
      </aside>
    `,document.body.appendChild(e),typeof O=="function"&&O(),e.addEventListener("click",t=>{const o=t.target;o&&(o.dataset&&o.dataset.close!==void 0||o.closest&&o.closest("[data-close]"))&&R()}),e.querySelectorAll(".compaction-tab").forEach(t=>{t.addEventListener("click",async()=>{e.querySelectorAll(".compaction-tab").forEach(o=>o.classList.remove("is-active")),t.classList.add("is-active"),await B()})}),document.addEventListener("keydown",t=>{t.key==="Escape"&&e.classList.contains("is-open")&&R()}),e.querySelector('[data-action="copy-json"]').addEventListener("click",_e),e.querySelector('[data-action="download"]').addEventListener("click",Ae),e)}let a=null;async function we(e){return D!==e&&(g.clear(),A=0,D=e),E.list(e,{force:!0})}async function W(e,t,o="summary"){const i=`${e}:${t}:${o}`,s=ye(i);if(s)return s;const r=o==="post"?await T.compactions.getPost(e,t):await T.compactions.getSummary(e,t);return fe(i,r),r}async function x(){const e=a;if(!e||!e.convId||!e.archiveId)return null;if(Array.isArray(e.activeMessages)||e.postMissing)return e.activeMessages;const t=e.selectionVersion,o=await W(e.convId,e.archiveId,"post");return a!==e||e.selectionVersion!==t?null:(e.activeArchive=Object.assign({},e.activeArchive||{},o.archive||{}),e.postMissing=!Array.isArray(o.postMessages),e.activeMessages=e.postMissing?null:o.postMessages,e.postSnapshot=o.postSnapshot,j(),a.activeMessages)}function j(){if(!a||!a.activeArchive)return;const e=a.activeArchive,t=k().querySelector(".compaction-drawer-meta"),o=e.trigger||"force",i=J(o),s=e.tokenCountKind==="estimated"?"≈":"",r=e.tokensBefore>0&&e.tokensAfter>0?`-${Math.round((1-e.tokensAfter/e.tokensBefore)*100)}%`:"—",d=e.reason?`<div class="cd-meta-row cd-meta-reason"><span class="cd-meta-k">${n(c("compactionViewer.metaReason"))}</span><span class="cd-meta-v">${n(e.reason)}</span></div>`:"";t.innerHTML=`
      <div class="cd-meta-grid">
        <div class="cd-meta-row">
          <span class="cd-meta-k">${n(c("compactionViewer.metaType"))}</span>
          <span class="cd-meta-v cd-meta-trigger cd-meta-trigger-${n(o)}">${i}</span>
        </div>
        <div class="cd-meta-row">
          <span class="cd-meta-k">${n(c("compactionViewer.metaTime"))}</span>
          <span class="cd-meta-v">${n(z(e.createdAt))}</span>
        </div>
        <div class="cd-meta-row">
          <span class="cd-meta-k">${n(c("compactionViewer.metaMsgs"))}</span>
          <span class="cd-meta-v">${e.msgsBefore||"?"} → ${e.msgsAfter||"?"}</span>
        </div>
        <div class="cd-meta-row">
          <span class="cd-meta-k">Token</span>
          <span class="cd-meta-v">${s}${M(e.tokensBefore)} → ${e.tokensAfter>0?s+M(e.tokensAfter):"—"} <em>(${r})</em></span>
        </div>
        <div class="cd-meta-row">
          <span class="cd-meta-k">${n(c("compactionViewer.metaModel"))}</span>
          <span class="cd-meta-v">${n(e.taskModel||e.model||"—")}</span>
        </div>
        <div class="cd-meta-row">
          <span class="cd-meta-k">${n(c("compactionViewer.metaRound"))}</span>
          <span class="cd-meta-v">#${e.roundNum||"?"}${e.taskId?` · task ${n(String(e.taskId).slice(0,8))}`:""}</span>
        </div>
        ${d}
      </div>
    `}function ve(){const e=k().querySelector(".compaction-drawer-content"),t=a&&a.activeMessages||[];if(a&&a.postMissing){e.innerHTML=`<div class="cd-empty">${n(c("compactionViewer.postMissing"))}</div>`;return}if(!t.length){e.innerHTML=`<div class="cd-empty">${n(c("compactionViewer.emptySnapshot"))}</div>`;return}const o=le(t,a.activeArchive),i=t.map((r,d)=>ge(r,d,o[d]));e.innerHTML=`<p class="cd-empty">${n(c("compactionViewer.stageCaption"))}</p>
      <button type="button" class="compaction-drawer-btn" data-jump-summary>${n(c("compactionViewer.jumpSummary"))}</button>
      <ol class="compaction-msg-list">${i.join("")}</ol>`;const s=e.querySelector("[data-jump-summary]");s.disabled=!e.querySelector('[data-compaction-kind="summary"], [data-compaction-kind="recovery"]'),s.addEventListener("click",()=>pe(e)),e.querySelectorAll("[data-reveal-image]").forEach(r=>{r.addEventListener("click",()=>{const d=r.dataset.imgUrl,p=r.parentElement;p.innerHTML=`<img src="${n(d)}" alt="compacted image" />`})}),e.querySelectorAll("[data-reveal-content]").forEach(r=>{r.addEventListener("click",()=>{const d=Number(r.dataset.messageIndex),p=a?.activeMessages?.[d],y=r.parentElement?.querySelector("code");!y||typeof p?.content!="string"||(y.textContent=p.content,r.remove())})})}function ge(e,t,o){const i=e&&e.role||"unknown",r={system:"SYSTEM",user:"USER",assistant:"ASSISTANT",tool:"TOOL RESULT",function:"FUNCTION"}[i]||i.toUpperCase();let d="";if(Array.isArray(e.tool_calls)&&e.tool_calls.length){const f=e.tool_calls.map(m=>{const w=m&&m.function||{},v=typeof w.arguments=="string"?w.arguments:JSON.stringify(w.arguments||{}),h=v.length>2e3?v.slice(0,2e3)+`
… (${v.length.toLocaleString()} chars total)`:v;return`<div class="cd-toolcall">
          <div class="cd-toolcall-name">→ ${n(w.name||"?")}<span class="cd-toolcall-id">${n(m.id||"")}</span></div>
          <pre class="cd-toolcall-args"><code>${n(h)}</code></pre>
        </div>`}).join("");d+=f}if(typeof e.content=="string")if(e.content.length>16e3){const m=e.content.slice(0,12e3)+`

… (${e.content.length.toLocaleString()} chars total; full content not rendered) …

`+e.content.slice(-2e3);d+=`<div class="cd-large-content"><pre class="cd-content"><code>${n(m)}</code></pre>
          <button type="button" class="compaction-drawer-btn" data-reveal-content
                  data-message-index="${t}">Reveal full content · ${e.content.length.toLocaleString()} chars</button></div>`}else d+=`<pre class="cd-content"><code>${n(e.content)}</code></pre>`;else if(Array.isArray(e.content)){const f=e.content.map(m=>he(m)).join("");d+=`<div class="cd-content-blocks">${f}</div>`}else e.content!=null&&(d+=`<pre class="cd-content"><code>${n(JSON.stringify(e.content,null,2))}</code></pre>`);e.thinking&&(d+=`<details class="cd-thinking"><summary>reasoning · ${e.thinking.length.toLocaleString()} chars</summary><pre><code>${n(e.thinking)}</code></pre></details>`);const p=[];e.name&&p.push(`name=${n(e.name)}`),e.tool_call_id&&p.push(`tool_call_id=${n(String(e.tool_call_id).slice(0,20))}`);const y=p.length?`<span class="cd-msg-meta">${p.join(" · ")}</span>`:"";return`<li class="cd-msg cd-msg-${n(i)}" data-compaction-kind="${o}">
      <div class="cd-msg-head">
        <span class="cd-msg-idx">#${t+1}</span>
        <span class="cd-msg-role">${n(c("compactionViewer.role"))}: ${n(r)}</span>
        <span class="cd-msg-meta">${n(c("compactionViewer.source"))}: ${n(c("compactionViewer.source."+o))}</span>
        ${y}
      </div>
      <div class="cd-msg-body">${d||'<em class="cd-empty-body">(empty)</em>'}</div>
    </li>`}function he(e){if(!e||typeof e!="object")return`<pre class="cd-content"><code>${n(JSON.stringify(e))}</code></pre>`;const t=e.type;if(t==="text")return`<pre class="cd-content"><code>${n(e.text||"")}</code></pre>`;if(t==="image_url"){const o=e.image_url&&e.image_url.url||"",i=C(o.length),s=o.startsWith("data:");return`<div class="cd-image-block">
        <div class="cd-image-head"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg> image_url · ${i}${s?" (base64)":""}</div>
        <div class="cd-image-placeholder">
          <button type="button" data-reveal-image
                  data-img-url="${n(o)}">${n(c("compactionViewer.revealImage"))}</button>
        </div>
      </div>`}return`<pre class="cd-content"><code>${n(JSON.stringify(e,null,2))}</code></pre>`}const l=(e,t)=>`
    <div class="cd-receipt-metric">
      <span>${n(e)}</span><strong>${n(String(t))}</strong>
    </div>`;function $e(e){if(!e||typeof e!="object"||!e.schemaVersion)return"";const t=e.retention||{},o=e.summary||{},i=o.usage||{},s=e.economics||null,r=e.evidence||null,d=e.recovery||null,p=e.contextBudget||null,y=c("compactionViewer.receiptUnknown"),f=u=>typeof u=="number"&&Number.isFinite(u)&&u>=0,m=u=>f(u)?M(u):y,w=u=>f(u)?u.toLocaleString():y,v=u=>typeof u=="boolean"?c(u?"compactionViewer.yes":"compactionViewer.no"):y,h=(u,se)=>se===!0&&f(u)?u>0&&u<1e-6?"< $0.000001":`$${u.toFixed(6)}`:y,S=o.route||{},V=o.cost||{},H={safety:"compactionViewer.decision.safety",economic:"compactionViewer.decision.economic",manual:"compactionViewer.decision.manual"},G=Object.prototype.hasOwnProperty.call(H,p?.decisionKind)?c(H[p.decisionKind]):y,I={main:"compactionViewer.policy.main","qualified-cheapest":"compactionViewer.policy.qualifiedCheapest"},X=Object.prototype.hasOwnProperty.call(I,S.policy)?c(I[S.policy]):S.policy||y,Q=p?`
      <section class="cd-receipt-group">
        <h4>${n(c("compactionViewer.receiptBudget"))}</h4>
        <div class="cd-receipt-metrics">
          ${l(c("compactionViewer.receiptDecision"),G)}
          ${l(c("compactionViewer.receiptWindow"),m(p.routeWindowTokens))}
          ${l(c("compactionViewer.receiptCeiling"),m(p.hardCeilingTokens))}
          ${l(c("compactionViewer.receiptRendered"),m(p.renderedPromptTokens))}
          ${l(c("compactionViewer.receiptReserve"),m(p.reserveTokens))}
          ${l(c("compactionViewer.receiptEconomicTarget"),m(p.economicTargetTokens))}
        </div>
        <p class="cd-receipt-outcome">${n(c("compactionViewer.receiptBudgetCaveat"))}</p>
      </section>`:"",q=Array.isArray(t.recentFiles)?t.recentFiles.slice(0,8):[],U=e.status||"legacy",Z=String(U).replace(/[^a-z0-9_-]/gi,""),ee=[l(c("compactionViewer.receiptModel"),S.modelId||y),l(c("compactionViewer.receiptProvider"),S.providerId||y),l(c("compactionViewer.receiptRoutePolicy"),X),l(c("compactionViewer.receiptRouteReason"),S.reason||y),l(c("compactionViewer.receiptComplete"),v(o.complete)),l(c("compactionViewer.receiptAccepted"),v(o.accepted)),l(c("compactionViewer.receiptPricingKnown"),v(V.known)),l(c("compactionViewer.receiptUsd"),h(V.costUsd,V.known)),V.known===!1&&f(V.knownCostUsd)?l(c("compactionViewer.receiptKnownSubtotal"),h(V.knownCostUsd,!0)):"",l(c("compactionViewer.receiptDuration"),f(o.durationMs)?`${w(o.durationMs)} ms`:y),l(c("compactionViewer.receiptUsage"),`${m(i.inputTokens)} + ${m(i.outputTokens)}`),l(c("compactionViewer.receiptProjectedUsage"),m(o.projectedUsageTokens)),l(c("compactionViewer.receiptSummaryChars"),w(o.chars))].join(""),N=Array.isArray(V.calls)?V.calls.slice(0,16).filter(u=>u&&typeof u=="object"):[],te=N.length?`<details class="cd-receipt-files">
      <summary>${n(c("compactionViewer.receiptAttempts"))}</summary>
      <ul>${N.map(u=>`<li><code>${n(u.model||y)}</code> · <code>${n(u.providerId||y)}</code> · ${n(h(u.costUsd,u.known))}${u.reason?` · ${n(u.reason)}`:""}</li>`).join("")}</ul>
    </details>`:"",oe=s?`
      <section class="cd-receipt-group">
        <h4>${n(c("compactionViewer.receiptEconomics"))}</h4>
        <div class="cd-receipt-metrics">
          ${l(c("compactionViewer.receiptDroppedTokens"),m(s.droppedTokens))}
          ${l(c("compactionViewer.receiptCacheRewrite"),m(s.cacheRewriteTokens))}
          ${l(c("compactionViewer.receiptPricingKnown"),v(s.pricingKnown))}
          ${l(c("compactionViewer.receiptSummaryCost"),h(s.summaryCostUsd,s.pricingKnown))}
          ${l(c("compactionViewer.receiptCacheRewriteUsd"),h(s.cacheRewriteCostUsd,s.pricingKnown))}
          ${l(c("compactionViewer.receiptPayback"),s.pricingKnown===!0?f(s.paybackRounds)?c("compactionViewer.receiptRounds",{n:s.paybackRounds}):s.paybackRounds===null?c("compactionViewer.receiptNoFinitePayback"):y:y)}
        </div>
      </section>`:"",ce=r?`
      <section class="cd-receipt-group">
        <h4>${n(c("compactionViewer.receiptEvidence"))}</h4>
        <div class="cd-receipt-metrics">
          ${l(c("compactionViewer.receiptEvidenceRetained"),r.retainedCount||0)}
          ${l(c("compactionViewer.receiptEvidenceLost"),r.lostCount||0)}
        </div>
      </section>`:"",ne=d?`
      <section class="cd-receipt-group">
        <h4>${n(c("compactionViewer.receiptRecovery"))}</h4>
        <div class="cd-receipt-metrics">
          ${l(c("compactionViewer.receiptStrippedImages"),d.strippedImages||0)}
          ${l(c("compactionViewer.receiptTruncatedChars"),Number(d.truncatedChars||0).toLocaleString())}
          ${l(c("compactionViewer.receiptDroppedMessages"),d.droppedMessages||0)}
          ${l(c("compactionViewer.receiptWireBytes"),`${C(d.wireBytesBefore)} → ${C(d.wireBytesAfter)}`)}
        </div>
      </section>`:"",ie=q.length?`
      <div class="cd-receipt-files">
        <span>${n(c("compactionViewer.receiptRecentFiles"))}</span>
        <ul>${q.map(u=>`<li><code>${n(u)}</code></li>`).join("")}</ul>
      </div>`:"";return`
      <section class="cd-receipt" data-receipt-version="${n(e.schemaVersion)}">
        <div class="cd-receipt-head">
          <h3>${n(c("compactionViewer.receiptTitle"))}</h3>
          <span class="cd-receipt-status cd-receipt-status-${n(Z)}">${n(U)}</span>
        </div>
        <div class="cd-receipt-identity">
          <code>${n(e.strategy||"—")}</code>
          <span>·</span><code>${n(e.implementation||"—")}</code>
          ${e.mode?`<span>·</span><code>${n(e.mode)}</code>`:""}
          <span>·</span><code>${n(e.continuation?.format||"none")}</code>
        </div>
        ${e.outcomeReason?`<p class="cd-receipt-outcome">${n(e.outcomeReason)}</p>`:""}
        <div class="cd-receipt-groups">
          <section class="cd-receipt-group">
            <h4>${n(c("compactionViewer.receiptRetention"))}</h4>
            <div class="cd-receipt-metrics">
              ${l(c("compactionViewer.receiptSummarized"),t.summarizedMessages||0)}
              ${l(c("compactionViewer.receiptPreservedTurns"),t.preservedTurns||0)}
              ${l(c("compactionViewer.receiptFoldedRounds"),t.foldedToolRounds||0)}
              ${l(c("compactionViewer.receiptVerbatimUsers"),t.retainedUserMessages||0)}
            </div>
            <div class="cd-receipt-flags">
              <span class="${t.objectiveAnchored?"is-on":""}">${n(c("compactionViewer.receiptObjectiveAnchor"))}</span>
              <span class="${t.turnDiffIncluded?"is-on":""}">${n(c("compactionViewer.receiptTurnDiff"))}</span>
            </div>
            ${ie}
          </section>
          ${e.summary?`<section class="cd-receipt-group"><h4>${n(c("compactionViewer.receiptSummaryRun"))}</h4><div class="cd-receipt-metrics">${ee}</div>${te}${o.rejectionReason?`<p class="cd-receipt-outcome">${n(o.rejectionReason)}</p>`:""}</section>`:""}
          ${Q}${oe}${ce}${ne}
          <p class="cd-receipt-outcome">${n(c("compactionViewer.receiptCostCaveat"))}</p>
        </div>
      </section>`}function be(){const e=k().querySelector(".compaction-drawer-content"),t=a&&a.activeArchive;if(!t)return;const o=$e(t.receipt);if(t.receipt?.summary?.complete===!1&&t.summary){e.innerHTML=o+`<div class="cd-empty">${n(c("compactionViewer.incompleteSummaryHidden"))}</div>`;return}if(!t.summary){e.innerHTML=o+`<div class="cd-empty">
        <p>${n(c("compactionViewer.noSummary1"))}</p>
        <p>${n(c("compactionViewer.noSummary2"))}</p>
      </div>`;return}e.innerHTML=o+`<h3 class="cd-summary-title">${n(c("compactionViewer.receiptSummaryText"))}</h3><pre class="cd-summary"><code>${n(t.summary)}</code></pre>`}function Ve(){const e=k().querySelector(".compaction-drawer-content"),t=a&&a.listData&&a.listData.compactions||[];if(!t.length){e.innerHTML=`<div class="cd-empty">${n(c("compactionViewer.noHistory"))}</div>`;return}const o=t.map(i=>{const s=a.archiveId===i.id,r=i.trigger||"force",d=i.tokenCountKind==="estimated"?"≈":"",p=i.tokensBefore>0&&i.tokensAfter>0?`-${Math.round((1-i.tokensAfter/i.tokensBefore)*100)}%`:"—";return`<li class="cd-history-item ${s?"is-active":""}"
                  data-archive-id="${n(String(i.id))}">
        <div class="cd-history-head">
          <span class="cd-history-trigger cd-history-trigger-${n(r)}">${J(r)}</span>
          ${i.resultStatus?`<span class="cd-history-result cd-receipt-status-${n(String(i.resultStatus).replace(/[^a-z0-9_-]/gi,""))}">${n(i.resultStatus)}</span>`:""}
          <span class="cd-history-time">${n(z(i.createdAt))}</span>
        </div>
        <div class="cd-history-stats">
          <span>${d}${M(i.tokensBefore)} → ${i.tokensAfter>0?d+M(i.tokensAfter):"—"} <em>(${p})</em></span>
          <span>·</span>
          <span>${i.msgsBefore||"?"} → ${i.msgsAfter||"?"} msgs</span>
          <span>·</span>
          <span>${C(i.payloadSize)}</span>
        </div>
        ${i.reason?`<div class="cd-history-reason">${n(i.reason)}</div>`:""}
      </li>`}).join("");e.innerHTML=`<ul class="compaction-history-list">${o}</ul>`,e.querySelectorAll(".cd-history-item").forEach(i=>{i.addEventListener("click",async()=>{const s=i.dataset.archiveId||"";s&&a&&a.convId&&await Y(a.convId,s)})})}async function B(){const e=k(),t=e.querySelector(".compaction-tab.is-active"),o=t?t.dataset.tab:"messages",i=a,s=i?.selectionVersion,r=()=>a===i&&i?.selectionVersion===s&&e.querySelector(".compaction-tab.is-active")===t;if(o==="messages"){b(!0);try{await x(),r()&&ve()}catch(d){if(!r())return;console.error("[compaction-viewer] post load failed:",d);const p=e.querySelector(".compaction-drawer-content");p.innerHTML=`<div class="cd-empty cd-error">${n(c("compactionViewer.loadFailed",{err:d.message||String(d)}))}</div>`}finally{r()&&b(!1)}}else o==="summary"?(b(!1),be()):o==="history"&&(b(!1),Ve())}function b(e){const t=k();t.querySelector(".compaction-drawer-loading").style.display=e?"block":"none",t.querySelector(".compaction-drawer-content").style.display=e?"none":"block"}async function Y(e,t){const o=a;if(!o||o.convId!==e)return;o.selectionVersion=Number(o.selectionVersion||0)+1;const i=o.selectionVersion;b(!0);try{const s=await W(e,t,"summary");if(a!==o||o.selectionVersion!==i)return;a.archiveId=t,a.activeArchive=s.archive||{},a.activeMessages=null,a.postMissing=!1,a.postSnapshot=null,j(),await B()}catch(s){if(a!==o||o.selectionVersion!==i)return;console.error("[compaction-viewer] load failed:",s);const r=k().querySelector(".compaction-drawer-content");r.innerHTML=`<div class="cd-empty cd-error">${n(c("compactionViewer.loadFailed",{err:s.message||String(s)}))}</div>`}finally{a===o&&o.selectionVersion===i&&b(!1)}}async function ke(e,t){if(!e){console.warn("[compaction-viewer] openCompactionViewer: missing convId");return}const o=k(),i={convId:e,archiveId:null,listData:null,activeArchive:null,activeMessages:null,selectionVersion:0,postMissing:!1};a=i,document.body.classList.add("compaction-drawer-open"),o.classList.add("is-open"),o.setAttribute("aria-hidden","false"),b(!0),o.querySelectorAll(".compaction-tab").forEach(s=>s.classList.remove("is-active")),o.querySelector('.compaction-tab[data-tab="messages"]').classList.add("is-active");try{const s=await we(e);if(a!==i)return;a.listData=s;const r=s.compactions||[];let d=t;if(!d&&r.length&&(d=r[r.length-1].id),!d){b(!1);const p=o.querySelector(".compaction-drawer-content");p.innerHTML=`<div class="cd-empty">${n(c("compactionViewer.noCompaction"))}</div>`,o.querySelector(".compaction-drawer-meta").innerHTML="";return}await Y(e,d)}catch(s){if(a!==i)return;console.error("[compaction-viewer] list failed:",s),b(!1);const r=o.querySelector(".compaction-drawer-content");r.innerHTML=`<div class="cd-empty cd-error">${n(c("compactionViewer.historyFailed",{err:s.message||String(s)}))}</div>`}}function R(){const e=document.getElementById("compactionViewerDrawer");e&&(e.classList.remove("is-open"),e.setAttribute("aria-hidden","true"),document.body.classList.remove("compaction-drawer-open"),a=null)}function Se(){const e=document.getElementById("compactionViewerDrawer");!e||!e.classList.contains("is-open")||!a||(a.activeArchive&&j(),B())}async function _e(){const e=a,t=e?.selectionVersion;if(!e)return;try{await x()}catch(i){typeof $=="function"&&$(c("compactionViewer.copyFailed",{err:i.message}),"error");return}if(a!==e||e.selectionVersion!==t)return;const o=JSON.stringify(K(e.activeArchive,e.activeMessages,e.postSnapshot),null,2);navigator.clipboard.writeText(o).then(()=>{typeof $=="function"&&$("✅ "+c("compactionViewer.copied"),"info")},i=>{console.error("[compaction-viewer] copy failed:",i),typeof $=="function"&&$(c("compactionViewer.copyFailed",{err:i.message}),"error")})}async function Ae(){const e=a,t=e?.selectionVersion;if(!(!e||!e.archiveId))try{if(await x(),a!==e||e.selectionVersion!==t)return;const o=new Blob([JSON.stringify(K(e.activeArchive,e.activeMessages,e.postSnapshot),null,2)],{type:"application/json; charset=utf-8"}),i=URL.createObjectURL(o),s=document.createElement("a");s.href=i,s.download=`compaction-${e.convId.slice(0,8)}-${e.archiveId}-post.json`,document.body.appendChild(s),s.click(),s.remove(),setTimeout(()=>URL.revokeObjectURL(i),500)}catch(o){console.error("[compaction-viewer] download failed:",o),typeof $=="function"&&$(c("compactionViewer.loadFailed",{err:o.message}),"error")}}window.addEventListener("tofu:language-change",Se);L.openCompactionViewer=ke;L.closeCompactionViewer=R;async function Be(e,t,o){return ae("compaction-viewer-presenters",e,t,o)}export{Be as invoke};
//# sourceMappingURL=compaction-viewer-CUv601w8.js.map
