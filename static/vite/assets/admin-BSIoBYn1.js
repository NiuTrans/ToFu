import{i as P}from"./action-registry-BG2lWHJ2.js";import{a as f}from"./transport-gA3NJcwc.js";import{r as T,_ as C,t as $,s as I}from"./index-CiKhKHfW.js";function t(e){return String(e??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}function n(e,a){return $(e,a)}function y(e){window.alert(e)}function B(e){return Promise.resolve(window.confirm(e))}function A(e,a){return Promise.resolve(window.prompt(e,a&&a.defaultValue||""))}function h(e,a){return((e||0)/1e6).toFixed(4)}function p(e){if(!e)return"—";try{return new Date(e*1e3).toLocaleString()}catch{return String(e)}}async function c(e,a){a=a||{};const o={method:a.method||"GET",parse:"response",headers:{"Content-Type":"application/json"}};a.body!==void 0&&(o.body=a.body);const i=await f.request(e,o);let r=null;try{r=await i.json()}catch{}if(!i.ok){const d=r&&(r.error||r.message)||"HTTP "+i.status;throw new Error(d)}return r}async function v(){try{if((await c("/api/v4/auth/mode")).mode!=="multi-user")return!1;const a=await c("/api/v4/users/me");return(a.principal&&a.principal.scopes||[]).includes("admin")}catch(e){return console.warn("[RelayAdmin] visibility check failed:",e),!1}}async function M(){const e=await v();document.querySelectorAll(".relay-admin-tab").forEach(a=>{a.style.display=e?"":"none"})}async function g(){const e=document.getElementById("relayUsersBlock");if(e)try{const o=(await c("/api/v4/users")).users||[],i=u,r=i?7:6;if(e.innerHTML=`
        <div class="settings-row" style="gap:8px; margin-bottom:12px;">
          <input type="text" id="newUserEmail" placeholder="email@example.com" class="settings-input" style="flex:1">
          <input type="password" id="newUserPassword" placeholder="${t(n("relayAdmin.pwPlaceholder"))}" class="settings-input" style="flex:1">
          <select id="newUserRole" class="settings-input">
            <option value="user">user</option>
            <option value="admin">admin</option>
          </select>
          <button class="btn btn-primary btn-sm" data-tofu-action="relayAdminCreateUser">${t(n("relayAdmin.create"))}</button>
        </div>
        <table class="settings-table">
          <thead><tr>
            <th>${t(n("relayAdmin.colEmail"))}</th><th>${t(n("relayAdmin.colRole"))}</th><th>${t(n("relayAdmin.colStatus"))}</th>${i?"<th>"+t(n("relayAdmin.colBalance"))+"</th>":""}
            <th>${t(n("relayAdmin.colCreated"))}</th><th>${t(n("relayAdmin.colLastLogin"))}</th><th>${t(n("relayAdmin.colActions"))}</th>
          </tr></thead>
          <tbody>${o.map(d=>`
            <tr data-uid="${t(d.id)}">
              <td>${t(d.email)}</td>
              <td>${t(d.role)}</td>
              <td>${t(d.status)}</td>
              ${i?'<td class="balance-cell">…</td>':""}
              <td>${t(p(d.created_at))}</td>
              <td>${t(p(d.last_login_at))}</td>
              <td>
                ${i?`<button class="btn btn-secondary btn-sm" data-tofu-action="relayAdminTopup" data-user-id="${t(d.id)}">${t(n("relayAdmin.topup"))}</button>`:""}
                ${i?`<button class="btn btn-secondary btn-sm" data-tofu-action="relayAdminViewPayments" data-user-id="${t(d.id)}" data-user-email="${t(d.email)}">${t(n("relayAdmin.payments"))}</button>`:""}
                <button class="btn btn-secondary btn-sm" data-tofu-action="relayAdminToggleStatus" data-user-id="${t(d.id)}" data-user-status="${t(d.status)}">${d.status==="active"?t(n("relayAdmin.disable")):t(n("relayAdmin.enable"))}</button>
              </td>
            </tr>
            <tr class="pay-drill" data-drill="${t(d.id)}" style="display:none"><td colspan="${r}" style="background:var(--panel-2,#232a3a);padding:0;"></td></tr>`).join("")||`<tr><td colspan="${r}" style="text-align:center;opacity:0.5">${t(n("relayAdmin.noUsers"))}</td></tr>`}</tbody>
        </table>`,i)for(const d of o)try{const s=await c("/api/v4/billing/wallet?user_id="+encodeURIComponent(d.id)),l=e.querySelector(`tr[data-uid="${d.id}"] .balance-cell`);l&&(l.textContent=h(s.balance_micro)+" c")}catch{}}catch(a){e.innerHTML='<span style="color:var(--accent-danger,#e25)">'+t(n("relayAdmin.loadFailed",{err:a.message}))+"</span>"}}async function F(){const e=document.getElementById("newUserEmail").value.trim(),a=document.getElementById("newUserPassword").value,o=document.getElementById("newUserRole").value;if(!e||!a){y(n("relayAdmin.needEmailPw"));return}try{await c("/api/v4/users",{method:"POST",body:JSON.stringify({email:e,password:a,role:o})}),document.getElementById("newUserEmail").value="",document.getElementById("newUserPassword").value="",g()}catch(i){y(n("relayAdmin.createFailed",{err:i.message}))}}async function L(e){const a=e.dataset.userId,o=await A(n("relayAdmin.topupAmount"),{defaultValue:"100"});if(!o)return;const i=parseFloat(String(o));if(!isFinite(i)||i<=0){y(n("relayAdmin.invalidAmount"));return}const r=await A(n("relayAdmin.topupNote"),{defaultValue:n("relayAdmin.topupNoteDefault")});try{await c("/api/v4/billing/deposit",{method:"POST",body:JSON.stringify({user_id:a,amount_micro:Math.round(i*1e6),kind:"adjust_credit",note:r||""})}),g()}catch(d){y(n("relayAdmin.topupFailed",{err:d.message}))}}async function R(e){const a=e.dataset.userId,o=e.dataset.userEmail,i=document.querySelector(`tr.pay-drill[data-drill="${CSS.escape(a)}"]`);if(!i)return;const r=i.querySelector("td");if(i.style.display!=="none"){i.style.display="none";return}i.style.display="",r.innerHTML='<div style="padding:10px;opacity:.6;">'+t(n("relayAdmin.loadingPayments"))+"</div>";try{const s=(await c("/api/v4/billing/payments?limit=100&user_id="+encodeURIComponent(a))).payments||[];r.innerHTML=`
        <div style="padding:10px 14px;">
          ${t(n("relayAdmin.paymentsOf",{email:o,n:s.length}))}
          <table class="settings-table" style="margin-top:6px;">
            <thead><tr>
              <th>${t(n("relayAdmin.colTime"))}</th><th>${t(n("relayAdmin.colProvider"))}</th><th>${t(n("relayAdmin.colAmount"))}</th><th>${t(n("relayAdmin.colCurrency"))}</th>
              <th>${t(n("relayAdmin.colCredited"))}</th><th>${t(n("relayAdmin.colStatus"))}</th><th>${t(n("relayAdmin.colExtId"))}</th>
            </tr></thead>
            <tbody>${s.map(l=>`
              <tr>
                <td>${t(p(l.created_at))}</td>
                <td>${t(l.provider)}</td>
                <td>${(l.amount_minor/100).toFixed(2)}</td>
                <td>${t(l.currency)}</td>
                <td>${h(l.credit_micro)} c</td>
                <td>${t(l.status)}</td>
                <td><code style="font-size:11px;">${t(l.provider_id||"—")}</code></td>
              </tr>`).join("")||'<tr><td colspan="7" style="text-align:center;opacity:0.5">'+t(n("relayAdmin.noPayments"))+"</td></tr>"}</tbody>
          </table>
        </div>`}catch(d){r.innerHTML='<div style="padding:10px;color:var(--danger,#e25)">'+t(n("relayAdmin.loadFailed",{err:d.message}))+"</div>"}}async function U(e){const a=e.dataset.userId,i=e.dataset.userStatus==="active"?"suspended":"active";if(await B(n("relayAdmin.toggleConfirm",{status:i})))try{await c("/api/v4/users/"+encodeURIComponent(a),{method:"PATCH",body:JSON.stringify({status:i})}),g()}catch(r){y(n("relayAdmin.updateFailed",{err:r.message}))}}async function k(){const e=document.getElementById("relayPricingBlock");if(e)try{const a=await c("/api/v4/billing/pricing"),o=a.models||{},i=a.default_margin||0,r=(s,l)=>s==null?"—":`${Number(s).toFixed(4)} ${t(l||"USD")}/M`,d=(s,l)=>(l=l||{},l.kind==="tiered"?(l.contextTiers||[]).map(m=>`<tr><td><code>${t(s)}</code><br><small>${t(m.id||"")}</small></td><td>${r(m.input,m.currency)}</td><td>${r(m.output,m.currency)}</td><td>${Number(m.cacheReadMul||0).toFixed(2)}× input</td><td>${Number(m.cacheWriteMul||0).toFixed(2)}× input</td><td>≤ ${Number(m.maxPromptTokens||0).toLocaleString()}</td></tr>`).join(""):`<tr><td><code>${t(s)}</code></td><td>${r(l.input,l.currency)}</td><td>${r(l.output,l.currency)}</td><td>${Number(l.cacheReadMul||0).toFixed(2)}× input</td><td>${Number(l.cacheWriteMul||0).toFixed(2)}× input</td><td>flat</td></tr>`);e.innerHTML=`
        <div class="settings-row" style="gap:8px;margin-bottom:8px;align-items:center;">
          <label>${t(n("relayAdmin.marginLabel"))}</label>
          <input type="number" id="priceMargin" min="0" max="10000" step="1"
                 class="settings-input" style="width:100px" value="${i*100}">
          <button class="btn btn-primary btn-sm" data-tofu-action="relayAdminSaveMargin">${t(n("relayAdmin.saveMargin"))}</button>
        </div>
        <span class="settings-desc" style="display:block;margin:0 0 12px;">${t(n("relayAdmin.marginDesc"))}</span>
        <div id="pricingSaveResult"></div>
        <p class="settings-desc" style="margin:12px 0 6px;">
          ${n("relayAdmin.ratesReadonly")}
        </p>
        <table class="settings-table">
          <thead><tr>
            <th>${t(n("relayAdmin.colModel"))}</th><th>${t(n("relayAdmin.colInput"))}</th><th>${t(n("relayAdmin.colOutput"))}</th>
            <th>${t(n("relayAdmin.colCacheHit"))}</th><th>${t(n("relayAdmin.colCacheWrite"))}</th><th>Prompt threshold</th>
          </tr></thead>
          <tbody>
            ${Object.entries(o).map(([s,l])=>d(s,l)).join("")}
          </tbody>
        </table>`}catch(a){e.innerHTML='<span style="color:var(--accent-danger,#e25)">'+t(n("relayAdmin.loadFailed",{err:a.message}))+"</span>"}}async function H(){const e=parseFloat(document.getElementById("priceMargin").value);if(!isFinite(e)||e<0){y(n("relayAdmin.invalidMargin"));return}const a=document.getElementById("pricingSaveResult");try{await c("/api/v4/billing/pricing",{method:"PUT",body:JSON.stringify({default_margin:e/100})}),a&&(a.innerHTML='<p style="color:var(--accent,#5a8);margin:8px 0 0;">'+t(n("relayAdmin.marginSaved"))+"</p>")}catch(o){a&&(a.innerHTML='<p style="color:var(--danger,#e25);margin:8px 0 0;">'+t(n("relayAdmin.saveFailed",{err:o.message}))+"</p>")}}async function w(){const e=document.getElementById("relayCodesBlock");if(e)try{const o=(await c("/api/v4/billing/redeem-codes?limit=50")).codes||[];e.innerHTML=`
        <div class="settings-row" style="gap:8px; margin-bottom:12px; flex-wrap:wrap;">
          <input type="number" id="codeBatchCount" min="1" max="1000" value="10" class="settings-input" style="width:80px" placeholder="${t(n("relayAdmin.codeCountPh"))}">
          <input type="number" id="codeBatchAmount" min="1" value="100" class="settings-input" style="width:120px" placeholder="${t(n("relayAdmin.codeAmountPh"))}">
          <input type="number" id="codeExpiresIn" min="0" max="365" value="30" class="settings-input" style="width:100px" placeholder="${t(n("relayAdmin.codeExpiresPh"))}">
          <input type="text" id="codeBatchName" class="settings-input" style="flex:1" placeholder="${t(n("relayAdmin.codeBatchPh"))}">
          <button class="btn btn-primary btn-sm" data-tofu-action="relayAdminMintCodes">${t(n("relayAdmin.generate"))}</button>
        </div>
        <div id="mintResult"></div>
        <table class="settings-table" style="margin-top:14px;">
          <thead><tr>
            <th>${t(n("relayAdmin.colCode"))}</th><th>${t(n("relayAdmin.colAmount"))}</th><th>${t(n("relayAdmin.colBatch"))}</th><th>${t(n("relayAdmin.colCreated"))}</th>
            <th>${t(n("relayAdmin.colStatus"))}</th><th>${t(n("relayAdmin.colRedeemer"))}</th>
          </tr></thead>
          <tbody>${o.map(i=>`
            <tr>
              <td><code>${t(i.code)}</code></td>
              <td>${h(i.amount_micro)} c</td>
              <td>${t(i.batch||"—")}</td>
              <td>${t(p(i.created_at))}</td>
              <td>${i.redeemed_by?t(n("relayAdmin.codeRedeemed")):t(n("relayAdmin.codeUnredeemed"))}</td>
              <td>${t(i.redeemed_by||"—")}</td>
            </tr>`).join("")||'<tr><td colspan="6" style="text-align:center;opacity:0.5">'+t(n("relayAdmin.noCodes"))+"</td></tr>"}</tbody>
        </table>`}catch(a){e.innerHTML='<span style="color:var(--accent-danger,#e25)">'+t(n("relayAdmin.loadFailed",{err:a.message}))+"</span>"}}async function O(){const e=parseInt(document.getElementById("codeBatchCount").value,10),a=parseFloat(document.getElementById("codeBatchAmount").value),o=parseInt(document.getElementById("codeExpiresIn").value,10),i=document.getElementById("codeBatchName").value.trim();if(!isFinite(e)||e<1||!isFinite(a)||a<=0){y(n("relayAdmin.invalidParams"));return}try{const r=await c("/api/v4/billing/redeem-codes",{method:"POST",body:JSON.stringify({count:e,amount_micro:Math.round(a*1e6),expires_in_days:o||0,batch:i})}),d=document.getElementById("mintResult"),s=(r.codes||[]).join(`
`);d.innerHTML=`
        <div style="margin-top:8px;padding:10px;background:rgba(80,180,160,0.06);border-left:3px solid var(--accent,#5a8);">
          <div style="margin-bottom:6px;">${n("relayAdmin.codesGenerated",{n:r.codes.length,amount:h(r.amount_micro)})}
            <button class="btn btn-secondary btn-sm" data-tofu-action="relayAdminCopyCodes">${t(n("relayAdmin.copyAll"))}</button>
          </div>
          <pre style="font-family:monospace;font-size:12px;background:#000;color:#9f9;padding:8px;border-radius:4px;max-height:200px;overflow:auto;">${t(s)}</pre>
        </div>`,w()}catch(r){y(n("relayAdmin.generateFailed",{err:r.message}))}}async function j(e){const a=e.closest("div")?.parentElement?.querySelector("pre")?.textContent||"";await navigator.clipboard.writeText(a)}async function N(){const e=document.getElementById("relayPaymentsBlock");if(e)try{const o=(await c("/api/v4/billing/payments?limit=100")).payments||[];e.innerHTML=`
        <p class="settings-desc">${t(n("relayAdmin.paymentsDesc"))}</p>
        <table class="settings-table">
          <thead><tr>
            <th>${t(n("relayAdmin.colTime"))}</th><th>${t(n("relayAdmin.colProvider"))}</th><th>${t(n("relayAdmin.colAmount"))}</th><th>${t(n("relayAdmin.colCurrency"))}</th>
            <th>${t(n("relayAdmin.colCredited"))}</th><th>${t(n("relayAdmin.colStatus"))}</th><th>${t(n("relayAdmin.colExtId"))}</th>
          </tr></thead>
          <tbody>${o.map(i=>`
            <tr>
              <td>${t(p(i.created_at))}</td>
              <td>${t(i.provider)}</td>
              <td>${(i.amount_minor/100).toFixed(2)}</td>
              <td>${t(i.currency)}</td>
              <td>${h(i.credit_micro)} c</td>
              <td>${t(i.status)}</td>
              <td><code style="font-size:11px;">${t(i.provider_id||"—")}</code></td>
            </tr>`).join("")||'<tr><td colspan="7" style="text-align:center;opacity:0.5">'+t(n("relayAdmin.noPaymentsYet"))+"</td></tr>"}</tbody>
        </table>`}catch(a){e.innerHTML='<span style="color:var(--accent-danger,#e25)">'+t(n("relayAdmin.loadFailed",{err:a.message}))+"</span>"}}function x(e){const a=typeof e=="string"?e:e.dataset.pane;document.querySelectorAll(".pane").forEach(o=>o.classList.toggle("active",o.id==="pane-"+a)),document.querySelectorAll(".nav-tabs button").forEach(o=>o.classList.toggle("active",o.dataset.pane===a)),a==="relayUsers"&&g(),a==="relayPricing"&&k(),a==="relayCodes"&&w(),a==="relayPayments"&&N()}let u=!0,b=!0;async function q(){try{const e=await c("/api/v4/capabilities"),a=e&&(e.relay||e.data&&e.data.relay)||{};typeof a.billing_enabled=="boolean"&&(u=a.billing_enabled),typeof a.model_relay_enabled=="boolean"&&(b=a.model_relay_enabled)}catch(e){console.warn("[RelayAdmin] relay policy load failed:",e)}}async function _(){await T(),C();const e=document.getElementById("adminGate"),a=document.getElementById("adminShell");if(!await v()){e&&(e.style.display=""),a&&(a.style.display="none");return}if(e&&(e.style.display="none"),a&&(a.style.display=""),await q(),u||["relayPricing","relayCodes","relayPayments"].forEach(i=>{const r=document.querySelector(`.nav-tabs button[data-pane="${i}"]`);r&&(r.style.display="none")}),!u||!b){const i=document.getElementById("agentOnlyBanner"),r=document.getElementById("agentOnlyBannerText");if(r){const d=[];b||d.push(n("relayAdmin.bannerByoOnly")),u||d.push(n("relayAdmin.bannerNoBilling")),r.innerHTML=d.join(" ")}i&&(i.style.display="")}x("relayUsers")}const z=Object.freeze(Object.defineProperty({__proto__:null,bootAdmin:_,refreshTabVisibility:M,relayAdminCopyCodes:j,relayAdminCreateUser:F,relayAdminMintCodes:O,relayAdminSaveMargin:H,relayAdminSwitch:x,relayAdminToggleStatus:U,relayAdminTopup:L,relayAdminViewPayments:R},Symbol.toStringTag,{value:"Module"})),D=z;function E(e){const a=D[e];return typeof a=="function"?a:void 0}const S=window;S.Api=f;S.TofuModules=Object.freeze({version:3,apiTransport:f,resolveAction:E,t:$,setLanguage:I});P(E);async function V(){document.readyState==="loading"&&await new Promise(e=>{document.addEventListener("DOMContentLoaded",()=>e(),{once:!0})}),await _(),window.dispatchEvent(new CustomEvent("tofu:app-ready",{detail:{version:3,entry:"admin"}}))}V().catch(e=>{console.error("[admin] application initialization failed",e),window.dispatchEvent(new CustomEvent("tofu:app-failed",{detail:{error:e}}))});
//# sourceMappingURL=admin-BSIoBYn1.js.map
