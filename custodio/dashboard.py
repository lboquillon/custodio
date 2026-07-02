# Copyright (c) 2026 Leonardo Boquillon
# SPDX-License-Identifier: MIT
"""The Custodio observability dashboard (single self-contained HTML document).

Zero build step, zero external assets, zero UI chrome. It is a monospace,
black-on-white *report*: one typeface, a hairline grid, and a single functional
red for your real data. No cards, pills, gradients, shadows, or rounded corners.
It stays live via Server-Sent Events from ``GET /custodio/stream`` — new requests
appear instantly and the open detail refreshes in place, with no polling.
"""

from __future__ import annotations

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Custodio</title>
<style>
/* Self-hosted (OFL) — deliberately NOT Inter/Geist/system defaults, and NOT
   fetched from a font CDN. Space Grotesk (display) + Space Mono (data). */
@font-face{font-family:"Space Mono";font-weight:400;font-display:swap;
  src:url("/custodio/assets/space-mono-400.woff2") format("woff2")}
@font-face{font-family:"Space Mono";font-weight:700;font-display:swap;
  src:url("/custodio/assets/space-mono-700.woff2") format("woff2")}
@font-face{font-family:"Space Grotesk";font-weight:500;font-display:swap;
  src:url("/custodio/assets/space-grotesk-500.woff2") format("woff2")}
@font-face{font-family:"Space Grotesk";font-weight:700;font-display:swap;
  src:url("/custodio/assets/space-grotesk-700.woff2") format("woff2")}
:root{
  --bg:#ffffff; --ink:#111111; --dim:#6f6f6f; --faint:#9b9b9b;
  --line:#e8e8e8; --line2:#d0d0d0;
  --red:#b21f13; --red-bg:#fbecea;
  --mono:"Space Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --disp:"Space Grotesk","Space Mono",ui-monospace,Menlo,monospace;
}
*{box-sizing:border-box;border-radius:0}
html,body{height:100%}
body{margin:0;font:13px/1.65 var(--mono);color:var(--ink);background:var(--bg);
  display:flex;flex-direction:column;height:100vh;overflow:hidden;
  -webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
a,button,input{font:inherit;color:inherit}

header{flex:none;display:flex;align-items:baseline;gap:18px;
  padding:16px 26px;border-bottom:2px solid var(--ink)}
.title{font-family:var(--disp);font-weight:700;letter-spacing:.18em;text-transform:uppercase;font-size:14px}
.title .sub{color:var(--dim);font-weight:500;letter-spacing:.16em}
.spacer{flex:1}
.status{color:var(--dim);font-size:12px;letter-spacing:.02em}
.status b{color:var(--ink);font-weight:700}
.status .sep{color:var(--line2);margin:0 8px}
#live{letter-spacing:.14em}
#live.on{color:var(--ink)} #live.off{color:var(--red)}

main{display:flex;flex:1;min-height:0}
#list{width:300px;flex:none;border-right:1px solid var(--line2);overflow:auto}
.listhead{position:sticky;top:0;z-index:2;background:var(--bg);
  border-bottom:1px solid var(--line2);padding:12px 18px}
.listhead input{width:100%;border:0;border-bottom:1px solid var(--line2);
  background:transparent;padding:5px 2px;font-size:12px;outline:none;color:var(--ink)}
.listhead input:focus{border-color:var(--ink)}
.listhead input::placeholder{color:var(--faint)}
.count{display:block;margin-top:10px;font-family:var(--disp);font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--faint)}

.row{padding:12px 18px;border-bottom:1px solid var(--line);cursor:pointer;
  border-left:3px solid transparent}
.row:hover{background:#f7f7f7}
.row.sel{background:#f2f2f2;border-left-color:var(--ink)}
.row.fresh{animation:flash 1.2s ease-out}
@keyframes flash{from{background:var(--red-bg)}to{background:transparent}}
.row .r1{display:flex;justify-content:space-between;color:var(--dim);font-size:11.5px}
.row .model{margin:5px 0 5px;color:var(--ink);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.row .types{color:var(--dim);font-size:11.5px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.row .types .m{color:var(--red)}
.ok{color:var(--ink)} .warn{color:var(--red)} .err{color:var(--red)}

#detail{flex:1;overflow:auto;padding:26px 40px}
.wrap{max-width:1040px}
.empty{color:var(--faint);margin-top:16vh;text-align:center;letter-spacing:.02em}
.metaline{color:var(--dim);font-size:12px;border-bottom:1px solid var(--line);
  padding-bottom:14px;letter-spacing:.01em}
.metaline b{color:var(--ink);font-weight:700}
.metaline .sep{color:var(--line2);margin:0 9px}

.sec{margin:30px 0 12px;display:flex;align-items:baseline;gap:12px;
  border-bottom:1px solid var(--ink);padding-bottom:6px}
.sec h2{margin:0;font-family:var(--disp);font-size:12px;letter-spacing:.2em;text-transform:uppercase;font-weight:700}
.sec .n{color:var(--faint);font-size:11px;letter-spacing:.1em}
.sec .act{margin-left:auto;font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--dim);cursor:pointer;border-bottom:1px solid var(--line2)}
.sec .act:hover{color:var(--ink);border-color:var(--ink)}

table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;font-family:var(--disp);color:var(--faint);font-weight:500;font-size:10.5px;
  letter-spacing:.12em;text-transform:uppercase;padding:0 14px 8px 0;border-bottom:1px solid var(--line)}
td{padding:8px 14px 8px 0;border-bottom:1px solid var(--line);vertical-align:baseline}
td.type{color:var(--dim);white-space:nowrap}
td.orig{color:var(--red)}
td.arrow{color:var(--faint);text-align:center;width:1em}
td.ph{color:var(--ink)}
td.score{color:var(--faint);text-align:right;white-space:nowrap;width:3em}
td.none,.emptyline{color:var(--faint)}

.cols{display:grid;grid-template-columns:1fr 1fr;gap:0}
@media(max-width:900px){.cols{grid-template-columns:1fr}}
.col{padding:0 22px}
.col.before{padding-left:0} .col.after{border-left:1px solid var(--line2)}
.col .cap{font-family:var(--disp);color:var(--dim);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;
  padding:2px 0 10px}
.doc{white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.85;
  color:var(--ink);max-height:360px;overflow:auto}
.pii{color:var(--red)}
.ph{border-bottom:1px dotted var(--faint)}
.note{color:var(--faint);font-size:11.5px;margin-top:10px}
.tags{font-size:12px;line-height:1.9}
.tags .miss{color:var(--red)}
.tags .rest .ph{border:0}
::-webkit-scrollbar{width:12px;height:12px}
::-webkit-scrollbar-thumb{background:#dcdcdc;border:4px solid var(--bg)}
::-webkit-scrollbar-track{background:transparent}
</style></head>
<body>
<header>
  <span class="title">Custodio <span class="sub">/ PII anonymization audit</span></span>
  <span class="spacer"></span>
  <span class="status">
    <b id="s_req">0</b> requests<span class="sep">/</span>
    <b id="s_ent">0</b> masked<span class="sep">/</span>
    <b id="s_miss">0</b> misses<span class="sep">/</span>
    <span id="live" class="off">connecting</span>
  </span>
</header>
<main>
  <div id="list">
    <div class="listhead">
      <input id="filter" placeholder="filter: model, type, endpoint">
      <span class="count" id="count"></span>
    </div>
    <div id="rows"></div>
  </div>
  <div id="detail"><div class="empty">select a request<br>&nbsp;<br>your real data never reaches Anthropic — only placeholders do</div></div>
</main>
<script>
const EVENTS = new Map();
let SEL = null, showOrig = true, filter = "";
// If the audit surface is token-protected, the dashboard is opened once with
// ?token=... . We move it into a cookie (so the SSE stream authenticates without
// the token ever appearing in the URL or access logs), scrub it from the address
// bar, and send it as an Authorization header on fetches.
let TOKEN = new URLSearchParams(location.search).get("token");
if(TOKEN){
  document.cookie = "custodio_token=" + encodeURIComponent(TOKEN) + "; path=/custodio; SameSite=Strict";
  const p = new URLSearchParams(location.search); p.delete("token");
  history.replaceState(null, "", location.pathname + (p.toString() ? "?" + p : "") + location.hash);
}else{
  const m = document.cookie.match(/(?:^|; )custodio_token=([^;]*)/);
  if(m) TOKEN = decodeURIComponent(m[1]);
}
const authHeaders = TOKEN ? {Authorization: "Bearer " + TOKEN} : {};
const $ = id => document.getElementById(id);
const esc = s => (s==null?"":String(s)).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const phRe = /<[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_\d+>/g;
const pad2 = n => String(n).padStart(2,"0");
const fmtTime = ts => { const d=new Date(ts*1000); return pad2(d.getHours())+":"+pad2(d.getMinutes())+":"+pad2(d.getSeconds()); };
const fmtScore = s => (s==null?"":Number(s).toFixed(2));

function typeCounts(ev){
  if(ev.entities_by_type) return ev.entities_by_type;
  const by={}; (ev.entities||[]).forEach(e=>by[e.entity_type]=(by[e.entity_type]||0)+1); return by;
}
const missCount = ev => ev.possible_miss_count!=null?ev.possible_miss_count
  :(ev.possible_misses?ev.possible_misses.length:0);
const statusClass = s => s==null?"" : s<300?"ok" : "err";

function renderStats(){
  let req=0,ent=0,miss=0;
  for(const ev of EVENTS.values()){ req++; ent+=ev.entity_count||0; miss+=missCount(ev); }
  $("s_req").textContent=req; $("s_ent").textContent=ent; $("s_miss").textContent=miss;
}
function matches(ev){
  if(!filter) return true;
  return [ev.model,ev.endpoint,Object.keys(typeCounts(ev)).join(" ")].join(" ").toLowerCase().includes(filter);
}
function renderList(){
  const all=[...EVENTS.values()].sort((a,b)=>b.ts-a.ts);
  const shown=all.filter(matches);
  $("count").textContent=shown.length+(all.length!==shown.length?" / "+all.length:"")+(shown.length===1?" request":" requests");
  $("rows").innerHTML=shown.map(ev=>{
    const types=Object.entries(typeCounts(ev)).map(([k,v])=>`${esc(k)}·${v}`).join("  ");
    const mc=missCount(ev);
    const miss=mc?`<span class="m">  ${mc} miss</span>`:"";
    const line=(types||`<span style="color:var(--faint)">no PII</span>`)+miss;
    const ep=(ev.endpoint||"").includes("count_tokens")?" [count]":"";
    const st=ev.status!=null?`<span class="${statusClass(ev.status)}">${ev.status}</span>`:"·";
    return `<div class="row ${ev.id===SEL?'sel':''} ${ev.__fresh?'fresh':''}" onclick="select('${ev.id}')">
      <div class="r1"><span>${fmtTime(ev.ts)}${ep}</span>
        <span>${ev.stream?'stream ':''}${st} ${ev.latency_ms!=null?ev.latency_ms+'ms':'…'}</span></div>
      <div class="model">${esc(ev.model)||'—'}</div>
      <div class="types">${line}</div></div>`;
  }).join("") || `<div class="row" style="cursor:default;color:var(--faint)">no requests yet</div>`;
}

// Escape AROUND placeholder matches so the < > tokens survive.
function markup(t,fn){ t=t||""; let out="",last=0;
  for(const m of t.matchAll(phRe)){ out+=esc(t.slice(last,m.index))+fn(m[0]); last=m.index+m[0].length; }
  return out+esc(t.slice(last)); }
const hlPlaceholders=t=>markup(t,m=>`<span class="ph">${esc(m)}</span>`);
const revealOriginals=(t,map)=>markup(t,m=>map[m]!==undefined?`<span class="pii">${esc(map[m])}</span>`:`<span class="ph">${esc(m)}</span>`);

function sec(title,n,act){ return `<div class="sec"><h2>${title}</h2>${n!=null?`<span class="n">${n}</span>`:""}${act||""}</div>`; }

function renderDetail(ev){
  if(!ev) return;
  const ents=ev.entities||[];
  const map={}; ents.forEach(e=>{ if(e.original!=null) map[e.placeholder]=e.original; });
  const haveOrig=Object.keys(map).length>0;
  const rows=ents.map(e=>{
    const shown=e.original!=null?e.original:e.original_masked;
    return `<tr><td class="type">${esc(e.entity_type)}</td><td class="orig">${esc(shown)}</td>
      <td class="arrow">&rarr;</td><td class="ph">${esc(e.placeholder)}</td>
      <td class="score">${fmtScore(e.score)}</td></tr>`;
  }).join("")||`<tr><td class="none" colspan="5">no PII detected in this request</td></tr>`;
  const misses=(ev.possible_misses||[]).map(m=>`<span class="miss">${esc(m.entity_type)} ${esc(m.text_masked)} ${fmtScore(m.score)}</span>`).join("&nbsp;&nbsp;·&nbsp;&nbsp;")
    ||`<span class="emptyline">none</span>`;
  const restored=(ev.response_placeholders||[]).map(p=>`<span class="rest"><span class="ph">${esc(p)}</span></span>`).join("&nbsp;&nbsp;")
    ||`<span class="emptyline">none echoed back</span>`;
  const prev=ev.anonymized_preview||"";
  const st=ev.status!=null?`<b class="${statusClass(ev.status)}">${ev.status}</b>`:"–";
  const sp='<span class="sep">/</span>';
  const act=haveOrig?`<span class="act" onclick="toggleView()">${showOrig?'hide your data':'reveal your data'}</span>`:"";
  $("detail").innerHTML=`<div class="wrap">
    <div class="metaline"><b>${new Date(ev.ts*1000).toLocaleString()}</b>${sp}${esc(ev.model)||'—'}${sp}${esc(ev.endpoint||'')}${sp}status ${st}${sp}${ev.latency_ms!=null?ev.latency_ms+' ms':'…'}${sp}${ev.stream?'streaming':'non-stream'}${sp}${ev.chars_in}&rarr;${ev.chars_out} B</div>
    ${sec("Replacements",ents.length)}
    <table><thead><tr><th>type</th><th>original</th><th></th><th>placeholder</th><th style="text-align:right">score</th></tr></thead><tbody>${rows}</tbody></table>
    ${haveOrig?'':'<div class="note">originals masked — set CUSTODIO_STORE_FULL_PII=true to reveal (debug only)</div>'}
    ${sec("What left your machine",null,act)}
    <div class="cols">
      ${haveOrig?`<div class="col before" id="beforeWrap"><div class="cap">before &middot; your data (never sent)</div><div class="doc">${revealOriginals(prev,map)}</div></div>`:''}
      <div class="col after" ${haveOrig?'':'style="padding-left:0;border-left:0"'}><div class="cap">after &middot; sent to Anthropic</div><div class="doc">${hlPlaceholders(prev)}</div></div>
    </div>
    ${sec("Possible PII not anonymized",null)}<div class="tags">${misses}</div>
    ${sec("Placeholders restored in response",null)}<div class="tags">${restored}</div>
  </div>`;
  if(haveOrig){ const bw=$("beforeWrap"); if(bw) bw.style.display=showOrig?"":"none"; }
}

function toggleView(){ showOrig=!showOrig; renderDetail(EVENTS.get(SEL)); }

async function select(id){
  SEL=id; renderList();
  let ev=EVENTS.get(id);
  if(!ev||!ev.__full){
    try{ const full=await(await fetch("/custodio/events/"+id,{headers:authHeaders})).json();
      if(full&&!full.error){ full.__full=true; EVENTS.set(id,full); ev=full; } }catch(e){}
  }
  renderDetail(EVENTS.get(id));
}

function applyEvent(ev,fresh){
  const prev=EVENTS.get(ev.id);
  ev.__full=("anonymized_preview" in ev); ev.__fresh=fresh&&!prev;
  EVENTS.set(ev.id,ev);
  renderStats(); renderList();
  if(SEL===ev.id) renderDetail(ev);
  if(ev.__fresh) setTimeout(()=>{ const e=EVENTS.get(ev.id); if(e) e.__fresh=false; },1300);
}

async function bootstrap(){
  try{ const list=await(await fetch("/custodio/events?limit=200",{headers:authHeaders})).json();
    list.reverse().forEach(s=>{ if(!EVENTS.has(s.id)) EVENTS.set(s.id,s); }); }catch(e){}
  renderStats(); renderList();
}
function connect(){
  const es=new EventSource("/custodio/stream");  // authenticates via the cookie
  es.onopen=()=>{ $("live").className="on"; $("live").textContent="live"; };
  es.onerror=()=>{ $("live").className="off"; $("live").textContent="reconnecting"; };
  es.addEventListener("event",e=>{ try{ applyEvent(JSON.parse(e.data).event,true); }catch(err){} });
}
$("filter").addEventListener("input",e=>{ filter=e.target.value.trim().toLowerCase(); renderList(); });
const _wanted=new URLSearchParams(location.search).get("event");
bootstrap().then(()=>{ if(_wanted) select(_wanted); connect(); });
</script>
</body></html>"""
