"use strict";
const $ = (id) => document.getElementById(id);
const colors = ["#f4c864", "#52d8c5", "#b2b5ff", "#f0a4b4", "#a5d184", "#68b7ed", "#e4a8e0", "#ebac71"];
const state = {record: null, plot: null, visible: new Set(), busy: false, range: null, hover: null, drag: null, plotSequence: 0};
const canvas = $("wave-canvas");
const esc = (value) => String(value).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const number = (value, digits=3) => value == null ? "未建立" : new Intl.NumberFormat("en-US", {maximumFractionDigits: digits}).format(value);
const hz = (value) => value == null ? "未建立" : Math.abs(value) >= 1000 ? `${number(value/1000)} kHz` : `${number(value)} Hz`;
function voltageUnit(value) {
  const a=Math.abs(value);
  return a>0 && a<.001 ? {scale:1e6,label:"µV"} : a>0 && a<1 ? {scale:1000,label:"mV"} : {scale:1,label:"V"};
}
const volts = (value) => {if(value==null)return "未建立";const u=voltageUnit(value);return `${number(value*u.scale)} ${u.label}`;};
const ms = (value) => number(value * 1000, 6);
const verdicts = {pass:"通过", fail:"超出限值", inconclusive:"证据不足", not_evaluated:"未设限值", not_evaluated_no_user_limits:"未设限值"};
const toolNames = {analyze_record:"整段波形测量", evaluate_limits:"测试限值评估", compare_reference:"参考记录对比", inspect_interval:"原始采样选区复查", propose_capture:"下一步补测计划"};

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {cache:"no-store"} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求未完成，请重试。");
  return data;
}
function notify(message, success=false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("success", success);
  $("notice").hidden = !message;
}
function setBusy(busy, label="分析中") {
  state.busy = busy;
  for (const id of ["import-button", "demo-button", "model-settings", "analyze-button", "inspect-button", "export-button", "zoom-in", "zoom-out", "fit-view"]) {
    $(id).disabled = busy || (!state.record && !["import-button", "demo-button", "model-settings"].includes(id));
  }
  $("agent-status").textContent = busy ? label : "就绪";
  $("agent-body").setAttribute("aria-busy", String(busy));
}
function shortChannel(name, index) {
  if (state.record?.demo) return `CH ${index + 1}`;
  const label = name.replace(/_V$/, "");
  return label.length > 14 ? `${label.slice(0,14)}…` : label;
}
function showRecord(record, reset=false) {
  state.record = record;
  const names = Object.keys(record.channels);
  if (reset) state.visible = new Set(names);
  $("source-name").textContent = record.source.name;
  $("source-description").textContent = `${number(record.sample_count,0)} 点 · ${names.length} 通道 · ${ms(record.end_s-record.start_s)} ms${record.has_reference ? " · 有参考记录" : ""}`;
  $("source-badge").textContent = record.source.kind==="dynamic_synthetic_demo" ? "合成冻结帧" : record.source.kind==="instrument_capture_unverified_calibration" ? "仪器冻结帧" : record.demo ? "合成演示" : "导入 CSV";
  $("source-hash").textContent = `SHA-256 ${record.source.sha256.slice(0,12)}…`;
  const uniform = Object.values(record.channels).every(c => c.uniform_sampling_within_1pct);
  $("record-quality").textContent = uniform ? "✓ 均匀采样" : "! 非均匀采样 · 周期与 FFT 已停用";
  $("analysis-mode").options[1].disabled = !record.llm_available;
  $("analysis-mode").options[1].textContent = record.llm_available ? "模型分析" : "模型分析 · 未配置";
  if (!record.llm_available) $("analysis-mode").value = "local";
  const selectedChannel = $("inspect-channel").value;
  $("inspect-channel").innerHTML = names.map((name,i)=>`<option value="${esc(name)}">${esc(shortChannel(name,i))}</option>`).join("");
  if (!reset && names.includes(selectedChannel)) $("inspect-channel").value = selectedChannel;
  $("channel-toggles").innerHTML = names.map((name,i)=>`<button type="button" class="channel-toggle channel-color-${i}" data-channel="${esc(name)}" aria-pressed="${state.visible.has(name)}" title="${esc(name)}"><span class="channel-dot"></span>${esc(shortChannel(name,i))}</button>`).join("");
  $("measurement-rows").innerHTML = names.map((name,i)=>{
    const c=record.channels[name], timing=c.timing, status=c.pass_fail;
    return `<tr><td><div class="table-channel channel-color-${i}" title="${esc(name)}"><span class="channel-dot"></span>${esc(shortChannel(name,i))}</div></td><td>${hz(timing.frequency_hz)}</td><td>${volts(c.statistics.vpp_v)}</td><td>${volts(c.statistics.rms_v)}</td><td>${timing.duty_cycle_pct==null ? "未建立" : number(timing.duty_cycle_pct,2)+" %"}</td><td><span class="verdict ${esc(status)}">${verdicts[status]||esc(status)}</span></td></tr>`;
  }).join("");
  $("evidence-count").textContent = record.trace.length;
  $("evidence-list").innerHTML = record.trace.map(e=>`<details class="evidence-item"><summary><span class="evidence-id">${esc(e.evidence_id)}</span><span>${toolNames[e.tool]||esc(e.tool)}</span><small>${e.status==="ok" ? "已完成" : "未完成"}</small></summary><pre>${esc(JSON.stringify(e,null,2))}</pre></details>`).join("");
  renderAnalysis(record);
  updateModeHint();
}
function planText(text) {
  const pairs = [
    ["Confirm probe ratio", "核对探头倍率、耦合、带宽、触发和采集模式，确认电压单位。"],
    ["Retain raw samples", "保留原始采样与采集说明；屏幕点可能遗漏窄脉冲。"],
    ["Use hardware triggering", "罕见事件使用硬件触发；轮询未发现不能证明没有毛刺。"],
    ["Obtain a contiguous", "重新获取连续采样，不对当前记录的采样空档进行补点。"],
    ["Acquire more samples", "增加边沿附近采样点，并确认仪器与探头的模拟带宽。"]
  ];
  return pairs.find(([prefix])=>text.startsWith(prefix))?.[1] || text;
}
function renderAnalysis(record) {
  const result = record.outcome;
  const valid = record.limits.status;
  const checks = record.limits.checks.length;
  const intro = $("agent-body").querySelector(".agent-intro");
  const title = valid === "pass" ? "记录满足限值" : valid === "fail" ? "存在超限指标" : valid === "inconclusive" ? "部分指标无法判定" : "记录已就绪";
  intro.querySelector("h3").textContent = title;
  intro.querySelector("p").textContent = checks ? `已核对 ${checks} 项限值` : "未设置测试限值";
  const names = Object.keys(record.channels);
  let html = `<details class="analysis-details"><summary>信号详情<span>${names.length} 通道</span></summary><div class="analysis-detail-body">`;
  names.forEach((name,i)=>{
    const c = record.channels[name];
    const freq = c.timing.frequency_hz;
    const sampled = c.rise_time.status === "sample_limited";
    html += `<div class="finding"><strong>${esc(shortChannel(name,i))} · ${freq == null ? "周期未建立" : hz(freq)+" 周期候选"}</strong><p>峰峰值 ${volts(c.statistics.vpp_v)}${sampled ? " · 边沿受采样限制" : ""}</p></div>`;
  });
  html += "</div></details>";
  const selection = [...record.trace].reverse().find(e=>e.tool==="inspect_interval" && e.status==="ok");
  if (selection) {
    const r=selection.result, c=r.analysis;
    html += `<div class="selection-result"><strong>选区复查 <span class="evidence-id">${esc(selection.evidence_id)}</span></strong><p>${esc(r.channel)} · ${ms(r.actual_interval_s[0])}–${ms(r.actual_interval_s[1])} ms</p><div class="selection-metrics"><span>原始点数<b>${number(c.sample_count,0)}</b></span><span>峰峰值<b>${volts(c.statistics.vpp_v)}</b></span><span>周期候选<b>${hz(c.timing.frequency_hz)}</b></span><span>RMS<b>${volts(c.statistics.rms_v)}</b></span></div></div>`;
  }
  const comparisons=record.trace.filter(e=>e.tool==="compare_reference" && e.status==="ok");
  comparisons.forEach(e=>{
    html += `<div class="analysis-block"><div class="analysis-block-title">参考差值<span class="evidence-id">${esc(e.evidence_id)}</span></div><div class="finding"><strong>${esc(e.result.channel)} · 峰峰值变化 ${volts(e.result.metrics.vpp_v.delta)}</strong><p>差值不等于故障；需确认两次采集的探头和工况一致。</p></div></div>`;
  });
  if (result.mode === "llm") html += `<div class="analysis-block"><div class="analysis-block-title">模型解释 · ${result.status==="completed" ? "已返回" : "未完成"}</div><p class="model-narrative">${esc(result.summary)}</p><p class="mode-hint">叙述未经独立核实 · ${esc(result.evidence_ids.join(" / "))}</p></div>`;
  const plan = record.trace.find(e=>e.tool==="propose_capture" && e.status==="ok");
  if (plan) html += `<details class="analysis-details"><summary>补测建议<span>${plan.result.steps.length} 项</span></summary><ul class="plan-list analysis-detail-body">${plan.result.steps.map(s=>`<li>${esc(planText(s))}</li>`).join("")}</ul></details>`;
  $("analysis-content").innerHTML = html;
}
async function loadDemo() {
  if (state.busy) return;
  notify("");setBusy(true,"载入中");
  try {
    const record=await api("/api/demo");
    showRecord(record,true);
    const frequencies=Object.values(record.channels).map(c=>c.timing.frequency_hz).filter(f=>f>0);
    const duration=record.end_s-record.start_s;
    const initial=frequencies.length ? Math.min(duration, 2/Math.min(...frequencies)) : duration;
    await loadPlot(record.start_s,Math.min(record.end_s,record.start_s+initial));
  } catch(error) {notify(`演示载入失败：${error.message} 请确认本地服务仍在运行。`);$("plot-loading").textContent="波形未载入，可重新载入演示或导入 CSV。";}
  finally {setBusy(false);}
}
async function loadPlot(start, end) {
  const sequence=++state.plotSequence;
  $("plot-loading").hidden=false;
  $("scope-display").setAttribute("aria-busy","true");
  try {
    const plot=await api(`/api/plot?${new URLSearchParams({record_id:state.record.record_id,start_s:start,end_s:end})}`);
    if(sequence!==state.plotSequence) return;
    state.plot=plot;state.range=[start,end];state.hover=null;
    $("range-start").value=Number((start*1000).toPrecision(12));
    $("range-end").value=Number((end*1000).toPrecision(12));
    $("plot-samples").textContent=`${number(plot.sample_count,0)} 点`;
    $("time-division").textContent=`${ms((end-start)/10)} ms / div`;
    $("view-duration").textContent=`${ms(start)} → ${ms(end)} ms`;
    draw();
  } finally {
    if(sequence===state.plotSequence){$("plot-loading").hidden=true;$("scope-display").setAttribute("aria-busy","false");}
  }
}
function drawPlot(canvas, plot, {visible, range=null, hover=null}={}) {
  const ctx=canvas.getContext("2d"), width=canvas.clientWidth, height=canvas.clientHeight;
  const b={width,height,left:55,right:width-18,top:20,bottom:height-31}, ratio=window.devicePixelRatio||1;
  canvas.width=Math.round(b.width*ratio);canvas.height=Math.round(b.height*ratio);ctx.setTransform(ratio,0,0,ratio,0,0);
  ctx.clearRect(0,0,b.width,b.height);
  if(!plot || b.width<80) return;
  const series=plot.series.filter(s=>!visible || visible.has(s.name));
  let low=Infinity,high=-Infinity;
  for(const s of series) for(const [,v] of s.points) {low=Math.min(low,v);high=Math.max(high,v);}
  if(!Number.isFinite(low)) {low=-1;high=1;}
  const padding=(high-low)*.15 || Math.max(Math.abs(low)*.1,1);
  low-=padding;high+=padding;
  const x=(t)=>b.left+(t-plot.start_s)/(plot.end_s-plot.start_s)*(b.right-b.left);
  const y=(v)=>b.bottom-(v-low)/(high-low)*(b.bottom-b.top);
  const geometry={...b,low,high,x,y}, unit=voltageUnit(Math.max(Math.abs(low),Math.abs(high)));
  ctx.font='11px "Consolas",monospace';ctx.textBaseline="middle";
  for(let i=0;i<=50;i++) {
    const px=b.left+(b.right-b.left)*i/50;ctx.beginPath();ctx.moveTo(px,b.top);ctx.lineTo(px,b.bottom);ctx.strokeStyle=i%5===0?"#294154":"#1b3244";ctx.lineWidth=i%5===0?.7:.4;ctx.stroke();
  }
  for(let i=0;i<=8;i++) {
    const py=b.top+(b.bottom-b.top)*i/8;ctx.beginPath();ctx.moveTo(b.left,py);ctx.lineTo(b.right,py);ctx.strokeStyle="#294154";ctx.lineWidth=.7;ctx.stroke();
    if(i%2===0){ctx.fillStyle="#95aabd";ctx.textAlign="right";ctx.fillText(number((high-(high-low)*i/8)*unit.scale,2),b.left-10,py);}
  }
  ctx.fillStyle="#95aabd";ctx.textAlign="left";ctx.fillText(unit.label,9,9);
  for(let i=0;i<=5;i++){ctx.textAlign=i===0?"left":i===5?"right":"center";ctx.fillText(ms(plot.start_s+(plot.end_s-plot.start_s)*i/5),b.left+(b.right-b.left)*i/5,b.bottom+17);}
  ctx.save();ctx.beginPath();ctx.rect(b.left,b.top,b.right-b.left,b.bottom-b.top);ctx.clip();
  if(low<=0 && high>=0){ctx.setLineDash([3,5]);ctx.strokeStyle="#688092";ctx.lineWidth=.6;ctx.beginPath();ctx.moveTo(b.left,y(0));ctx.lineTo(b.right,y(0));ctx.stroke();ctx.setLineDash([]);}
  for(const s of series){const index=plot.series.indexOf(s);ctx.beginPath();s.points.forEach(([t,v],i)=>i?ctx.lineTo(x(t),y(v)):ctx.moveTo(x(t),y(v)));ctx.strokeStyle=colors[index%colors.length];ctx.lineWidth=1.4;ctx.lineJoin="round";ctx.stroke();}
  if(range){const [a,z]=range;ctx.fillStyle="#62d6c814";ctx.fillRect(x(a),b.top,x(z)-x(a),b.bottom-b.top);ctx.setLineDash([4,4]);ctx.strokeStyle="#8ac9c3";ctx.lineWidth=.8;for(const t of [a,z]){ctx.beginPath();ctx.moveTo(x(t),b.top);ctx.lineTo(x(t),b.bottom);ctx.stroke();}ctx.setLineDash([]);}
  if(hover){ctx.strokeStyle="#9cb3c3";ctx.lineWidth=.7;ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(hover.x,b.top);ctx.lineTo(hover.x,b.bottom);ctx.stroke();ctx.setLineDash([]);}
  ctx.restore();
  return geometry;
}
function draw() {state.geometry=drawPlot(canvas,state.plot,{visible:state.visible,range:state.range,hover:state.hover});}
function pointerTime(event) {
  const rect=canvas.getBoundingClientRect(),g=state.geometry;
  if(!g||!state.plot)return null;
  const px=Math.max(g.left,Math.min(g.right,event.clientX-rect.left));
  return {t:state.plot.start_s+(px-g.left)/(g.right-g.left)*(state.plot.end_s-state.plot.start_s),x:px,y:event.clientY-rect.top};
}
canvas.addEventListener("pointerdown",(event)=>{if(state.busy||event.button!==0)return;const p=pointerTime(event);if(!p)return;state.drag=p;canvas.setPointerCapture(event.pointerId);$("plot-tooltip").hidden=true;});
canvas.addEventListener("pointermove",(event)=>{
  const p=pointerTime(event);if(!p)return;
  if(state.drag){state.range=[Math.min(state.drag.t,p.t),Math.max(state.drag.t,p.t)];$("range-start").value=Number((state.range[0]*1000).toPrecision(10));$("range-end").value=Number((state.range[1]*1000).toPrecision(10));}
  else {
    state.hover=p;
    const readings=[`${ms(p.t)} ms`];
    for(const s of state.plot.series.filter(v=>state.visible.has(v.name))){let lo=0,hi=s.points.length-1;while(lo<hi){const mid=(lo+hi)>>1;if(s.points[mid][0]<p.t)lo=mid+1;else hi=mid;}const pt=s.points[lo];readings.push(`${shortChannel(s.name,Object.keys(state.record.channels).indexOf(s.name))} ≈ ${volts(pt[1])}`);}
    $("plot-tooltip").textContent=readings.join("\n");$("plot-tooltip").hidden=false;$("plot-tooltip").style.left=`${Math.max(5,Math.min(p.x+12,canvas.clientWidth-155))}px`;$("plot-tooltip").style.top=`${Math.max(6,Math.min(p.y-30,canvas.clientHeight-85))}px`;
  }draw();
});
function endDrag(event){if(state.drag){const p=pointerTime(event);if(p&&Math.abs(p.x-state.drag.x)<3){state.range=[state.plot.start_s,state.plot.end_s];$("range-start").value=state.plot.start_s*1000;$("range-end").value=state.plot.end_s*1000;}state.drag=null;draw();}}
canvas.addEventListener("pointerup",endDrag);canvas.addEventListener("pointercancel",endDrag);
canvas.addEventListener("pointerleave",()=>{state.hover=null;$("plot-tooltip").hidden=true;draw();});
new ResizeObserver(()=>draw()).observe($("scope-display"));
$("channel-toggles").addEventListener("click",(event)=>{const button=event.target.closest("[data-channel]");if(!button)return;const name=button.dataset.channel;state.visible.has(name)?state.visible.delete(name):state.visible.add(name);button.setAttribute("aria-pressed",String(state.visible.has(name)));draw();});
for(const id of ["range-start","range-end"])$(id).addEventListener("change",()=>{state.range=[$("range-start").valueAsNumber/1000,$("range-end").valueAsNumber/1000];draw();});
async function zoom(scale){if(!state.record||state.busy||!state.plot)return;const p=state.plot, r=state.record,center=(p.start_s+p.end_s)/2;const minimum=(r.end_s-r.start_s)/Math.max(1,r.sample_count-1)*4;const span=Math.min(r.end_s-r.start_s,Math.max(minimum,(p.end_s-p.start_s)*scale));const start=Math.max(r.start_s,Math.min(r.end_s-span,center-span/2));try{await loadPlot(start,Math.min(r.end_s,start+span));}catch(error){notify(error.message);}}
$("zoom-in").addEventListener("click",()=>zoom(.5));$("zoom-out").addEventListener("click",()=>zoom(2));
$("fit-view").addEventListener("click",async()=>{if(state.record)try{await loadPlot(state.record.start_s,state.record.end_s);}catch(error){notify(error.message);}});
function changeTab(evidence){for(const [id,selected] of [["measures",!evidence],["evidence",evidence]]){$(`tab-${id}`).setAttribute("aria-selected",String(selected));$(`tab-${id}`).tabIndex=selected?0:-1;$(`pane-${id}`).hidden=!selected;}}
$("tab-measures").addEventListener("click",()=>changeTab(false));$("tab-evidence").addEventListener("click",()=>changeTab(true));
for(const id of ["tab-measures","tab-evidence"])$(id).addEventListener("keydown",e=>{if(["ArrowLeft","ArrowRight","Home","End"].includes(e.key)){e.preventDefault();const evidence=e.key==="End"||(e.key!=="Home"&&id==="tab-measures");changeTab(evidence);$(evidence?"tab-evidence":"tab-measures").focus();}});
async function analyze(interval) {
  if(state.busy||!state.record)return;
  const question=$("question").value.trim()||"检查周期、幅度和采样限制，并提出补测建议。";
  const request={record_id:state.record.record_id,question,mode:interval?"local":$("analysis-mode").value};
  if(interval)request.interval=interval;
  setBusy(true,interval?"复查中":"分析中");notify("");
  try{const record=await api("/api/analyze",request);showRecord(record);if(record.outcome.status!=="completed")notify(record.outcome.error||"模型未在预算内完成，已有测量与工具证据仍可查看。");else notify(interval?"选区已使用原始采样复查，详细结果见右侧和证据。":"分析已更新，报告可导出。",true);}
  catch(error){notify(error.message);}
  finally{setBusy(false);}
}
$("question-form").addEventListener("submit",e=>{e.preventDefault();analyze();});
$("inspect-button").addEventListener("click",()=>{const start=$("range-start").valueAsNumber/1000,end=$("range-end").valueAsNumber/1000;if(!Number.isFinite(start)||!Number.isFinite(end)||start>=end){notify("请输入有效的开始和结束时间，开始必须小于结束。");return;}analyze({channel:$("inspect-channel").value,start_s:start,end_s:end});});
document.querySelectorAll("[data-question]").forEach(button=>button.addEventListener("click",()=>{$("question").value=button.dataset.question;$("question").focus();}));
function updateModeHint(){$("mode-hint").textContent=$("analysis-mode").value==="llm"?"发送问题与测量，可能产生费用。":"本地计算，无需模型。";}
$("analysis-mode").addEventListener("change",updateModeHint);
function applyModelStatus(status, select=false) {
  const ready=status.configured;
  if(state.record)state.record.llm_available=ready;
  $("analysis-mode").options[1].disabled=!ready;
  $("analysis-mode").options[1].textContent=ready?"模型分析":"模型分析 · 未配置";
  if(!ready)$("analysis-mode").value="local";
  else if(select)$("analysis-mode").value="llm";
  updateModeHint();
}
function modelBusy(busy) {
  for(const id of ["model-url","model-name","model-key","model-save","model-disable"])$(id).disabled=busy;
  $("model-form").setAttribute("aria-busy",String(busy));
}
$("model-settings").addEventListener("click",async()=>{
  $("model-key").value="";$("model-error").hidden=true;$("model-current").textContent="正在读取设置…";
  $("model-dialog").showModal();modelBusy(true);
  try {
    const status=await api("/api/model");applyModelStatus(status);
    $("model-url").value=status.base_url;$("model-name").value=status.model;
    $("model-current").textContent=status.configured?`已配置：${status.model}${status.has_api_key?" · 密钥已设置":" · 无密钥"}`:"尚未配置模型";
  } catch(error){$("model-error").textContent=error.message;$("model-error").hidden=false;$("model-current").textContent="无法读取设置，请检查本地服务。";}
  finally {modelBusy(false);}
});
$("model-dialog").addEventListener("close",()=>{$("model-key").value="";});
async function saveModel(body) {
  $("model-error").hidden=true;modelBusy(true);
  try {
    const status=await api("/api/model",body);applyModelStatus(status,status.configured);
    $("model-dialog").close();notify(status.configured?`已配置模型 ${status.model}。输入问题并点击「开始分析」才会请求服务。`:"已停用模型，切回本地规则分析。",true);
  } catch(error){$("model-key").value="";$("model-error").textContent=error.message;$("model-error").hidden=false;}
  finally {modelBusy(false);}
}
$("model-form").addEventListener("submit",e=>{
  e.preventDefault();saveModel({base_url:$("model-url").value.trim(),model:$("model-name").value.trim(),api_key:$("model-key").value});
});
$("model-disable").addEventListener("click",()=>saveModel({enabled:false}));
$("demo-button").addEventListener("click",loadDemo);
$("export-button").addEventListener("click",()=>{if(!state.record)return;const a=document.createElement("a");a.href=`/api/report?record_id=${encodeURIComponent(state.record.record_id)}`;a.download="scope-agent-report.zip";document.body.appendChild(a);a.click();a.remove();notify("已请求下载报告 ZIP，包含测量、工具证据与波形预览。",true);});
$("import-button").addEventListener("click",()=>{$("upload-error").hidden=true;$("import-dialog").showModal();});
for(const id of ["help-button","about-button"])$(id).addEventListener("click",()=>$("help-dialog").showModal());
document.querySelectorAll(".dialog-close").forEach(button=>button.addEventListener("click",()=>button.closest("dialog").close()));
$("wave-file").addEventListener("change",()=>{$("wave-file-label").textContent=$("wave-file").files[0]?.name||"选择或拖入波形 CSV";});
const drop=$("file-drop");
for(const event of ["dragenter","dragover"])drop.addEventListener(event,e=>{e.preventDefault();drop.classList.add("dragging");});
for(const event of ["dragleave","drop"])drop.addEventListener(event,e=>{e.preventDefault();drop.classList.remove("dragging");});
drop.addEventListener("drop",e=>{if(e.dataTransfer.files.length){$("wave-file").files=e.dataTransfer.files;$("wave-file").dispatchEvent(new Event("change"));}});
async function filePayload(file) {
  const bytes=new Uint8Array(await file.arrayBuffer());let binary="";
  for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
  return {name:file.name,content:btoa(binary)};
}
$("upload-form").addEventListener("submit",async e=>{
  e.preventDefault();if(state.busy)return;
  const inputs={csv:"wave-file",reference:"reference-file",limits:"limits-file",metadata:"metadata-file"};
  const files=Object.entries(inputs).map(([key,id])=>[key,$(id).files[0]]).filter(([,file])=>file);
  const errorNode=$("upload-error");errorNode.hidden=true;
  if(!$("wave-file").files[0]){errorNode.textContent="请选择波形 CSV。";errorNode.hidden=false;return;}
  if(files.reduce((sum,[,f])=>sum+f.size,0)>32*1024*1024||files.some(([k,f])=>["limits","metadata"].includes(k)&&f.size>131072)){errorNode.textContent="文件合计不能超过 32 MiB，每份 JSON 不能超过 128 KiB。";errorNode.hidden=false;return;}
  setBusy(true,"导入中");$("upload-submit").disabled=true;$("upload-submit").textContent="正在导入并分析…";
  try{
    const body={};for(const [key,file] of files)body[key]=await filePayload(file);
    const record=await api("/api/import",body);showRecord(record,true);await loadPlot(record.start_s,record.end_s);$("import-dialog").close();notify("波形已导入，测量、证据与补测计划已更新。",true);
  }catch(error){errorNode.textContent=`导入未完成：${error.message}`;errorNode.hidden=false;}
  finally{setBusy(false);$("upload-submit").disabled=false;$("upload-submit").innerHTML=`导入并分析 ${icon("arrow")}`;}
});
loadDemo();
