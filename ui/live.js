"use strict";
const live = {stream:null, status:null, frame:null, visible:new Set(), epoch:0, timer:null, pending:false, checkedAt:0, devices:[], device:null};
const liveCanvas=$("live-canvas");
const liveLabels={idle:"尚未开始",starting:"正在建立会话…",running:"正在监看",stopping:"正在断开，请稍候…",stopped:"已停止 · 保留最后一帧",error:"监看已中断"};
function liveError(message="") {$("live-error").textContent=message;$("live-error").hidden=!message;}
function liveConfig() {
  const hardware=$("live-source").value!=="demo", views=live.device?.views||[];
  $("live-view").options[0].disabled=hardware&&!views.includes("curve");
  $("live-view").options[1].disabled=!hardware||!views.includes("screen");
  if(!hardware||!views.includes($("live-view").value))$("live-view").value="curve";
  const screen=$("live-view").value==="screen";
  $("live-device-panel").hidden=!hardware;$("live-ack-field").hidden=!hardware;
  $("live-channel-field").hidden=!hardware||screen||!live.device?.channels.length;
  $("live-resource").required=hardware;$("live-ack").required=hardware;
  if(!live.frame)$("live-source-badge").textContent=hardware?(live.device?`${live.device.vendor} ${live.device.model}`:"待识别设备"):"动态合成演示";
  $("live-explanation").textContent=!hardware
    ? "动态合成演示由软件逐帧生成，幅度、频率和占空比会缓慢变化；不是仪器数据。冻结后使用该帧完整采样分析。"
    : !live.device?.supported ? "先扫描或输入地址识别实际设备，再按型号匹配适配器。识别身份不代表已经支持波形 / 截图读取。"
    : screen ? "原屏视图周期读取仪器截图，可看到网格、菜单与屏幕读数。截图不用于数值测量，也不提供触屏控制。首版实机兼容性待验证。"
    : "曲线视图读取所选通道的 NORMal 屏幕采样，最多 10,000 点 / 帧。冻结后使用完整帧分析；首版实机兼容性待验证。";
}
function liveButtons() {
  const active=live.status?.worker_alive||false, hardware=$("live-source").value!=="demo";
  for(const id of ["live-source","live-view","live-channel","live-resource","live-ack","live-interval"])$(id).disabled=active||live.pending;
  $("live-view").disabled=active||live.pending||(hardware&&!live.device?.supported);
  $("live-device").disabled=active||live.pending||!live.devices.length;
  $("device-scan").disabled=active||live.pending;
  $("device-identify").disabled=active||live.pending||!$("live-resource").value.trim();
  $("live-start").disabled=active||live.pending||(hardware&&(!live.device?.supported||live.device.resource!==$("live-resource").value));
  $("live-stop").disabled=!active||live.pending||live.status?.state==="stopping";
  $("live-freeze").disabled=live.pending||!live.frame?.has_waveform||live.status?.state==="stopping";
}
function clearLiveFrame() {
  live.frame=null;live.visible.clear();
  $("live-state").textContent="尚未开始";
  $("live-screen").hidden=true;$("live-screen").removeAttribute("src");liveCanvas.hidden=false;
  $("live-empty").hidden=false;$("live-empty").textContent="等待开始监看并接收第一帧。";
  $("live-channels").replaceChildren();$("live-metrics").replaceChildren();
  $("live-frame-number").textContent="等待第一帧";drawPlot(liveCanvas,null);
  $("live-rate").textContent="接收帧率 —";$("live-updated").textContent="尚未接收";
  $("live-timebase").textContent="TIME / ms";liveAge();
}
function deviceOptions() {
  $("live-device").innerHTML='<option value="">选择已识别的设备</option>'+live.devices.map(d=>`<option value="${esc(d.resource)}">${esc([d.vendor,d.model,d.serial].filter(Boolean).join(" · "))}${d.supported?"":" · 未支持"} — ${esc(d.resource)}</option>`).join("");
}
function selectDevice(device) {
  live.device=device||null;clearLiveFrame();
  $("live-device").value=device?.resource||"";
  if(device)$("live-resource").value=device.resource;
  const previous=$("live-channel").value;
  $("live-channel").innerHTML=(device?.channels||[1]).map(n=>`<option value="${n}">CH${n}</option>`).join("");
  if(device?.channels.includes(Number(previous)))$("live-channel").value=previous;
  $("device-info").textContent=device?`${device.vendor} ${device.model} · ${device.supported?"可读取":"暂未适配，可导入 CSV"}`:"扫描设备，或填写地址后识别。";
  $("device-details").textContent=device?`序列号 ${device.serial||"未提供"} · 固件 ${device.firmware||"未提供"}。${device.message}`:"识别后显示型号、序列号和固件。";
  $("device-info").classList.toggle("unsupported",!!device&&!device.supported);
  liveConfig();liveButtons();
}
async function queryDevices(resource) {
  if(live.pending||live.status?.worker_alive)return;
  live.pending=true;liveError();liveButtons();
  $("device-info").textContent=resource?"正在读取这台设备的身份…":"正在枚举 VISA 资源并读取身份，可能需要数秒…";
  try {
    const result=await api("/api/devices",resource?{resource}:{});
    const known=new Map(resource?live.devices.map(d=>[d.resource,d]):[]);
    result.devices.forEach(d=>known.set(d.resource,d));live.devices=[...known.values()].slice(-16);deviceOptions();
    selectDevice(result.devices.find(d=>d.resource===live.device?.resource)||result.devices.find(d=>d.supported)||result.devices[0]);
    $("device-details").textContent+=` ${result.message}${result.truncated?" 本次最多检查 8 个资源，其余可手动识别。":""}`;
  } catch(error){selectDevice(null);liveError(error.message);$("device-info").textContent="设备识别未完成。请检查依赖、地址和连接；演示仍可使用。";}
  finally {live.pending=false;liveButtons();}
}
function liveAge() {
  const age=live.frame?live.status?.age_ms:null;
  const stale=age!=null&&live.status?.state==="running"&&age+performance.now()-live.checkedAt>Math.max(3000,(live.status.config?.interval_ms||500)*3);
  $("live-age").textContent=age==null?"数据年龄 —":`${stale?"数据陈旧 · ":""}数据年龄 ${number((age+performance.now()-live.checkedAt)/1000,1)} 秒`;
  $("live-age").classList.toggle("stale",stale);
}
function renderLive(status) {
  live.status=status;live.checkedAt=performance.now();
  $("live-state").textContent=liveLabels[status.state]||"状态未知";
  $("live-state").classList.toggle("live-running",status.state==="running");
  $("live-rate").textContent=`接收帧率 ${status.received_fps==null?"—":number(status.received_fps,1)+" fps"}`;
  const frame=status.frame;
  if(frame && frame.sequence!==live.frame?.sequence) {
    live.frame=frame;
    $("live-frame-number").textContent=`帧 #${frame.sequence}${status.config.view==="screen"?" · 原屏截图":" · "+number(frame.plot.sample_count,0)+" 点 / 通道"}`;
    const instrument=frame.metadata.instrument;
    $("live-source-badge").textContent=frame.synthetic?"动态合成演示":`${instrument?.vendor||""} ${instrument?.model||"仪器数据"}`.trim();
    $("live-updated").textContent=`接收于 ${new Date(frame.received_at_utc).toLocaleTimeString("zh-CN",{hour12:false})}`;
    $("live-empty").hidden=true;
    const screen=frame.view==="screen";liveCanvas.hidden=screen;$("live-screen").hidden=!screen;
    if(screen) {
      $("live-screen").src=`data:image/png;base64,${frame.image_base64}`;
      $("live-metrics").replaceChildren();$("live-channels").replaceChildren();
      $("live-timebase").textContent="INSTRUMENT SCREEN";
    } else {
      const names=Object.keys(frame.channels);
      if(!$("live-channels").children.length) {
        live.visible=new Set(names);
        $("live-channels").innerHTML=names.map((name,i)=>`<button type="button" class="channel-toggle channel-color-${i}" data-live-channel="${esc(name)}" aria-pressed="true"><span class="channel-dot"></span>${esc(name.replace(/_V$/,""))}</button>`).join("");
      }
      $("live-timebase").textContent=`${ms((frame.plot.end_s-frame.plot.start_s)/10)} ms / div`;
      drawPlot(liveCanvas,frame.plot,{visible:live.visible});
      $("live-metrics").innerHTML=names.map((name,i)=>{
        const c=frame.channels[name];
        return `<div class="live-metric channel-color-${i}"><strong class="channel-label-${i}">${esc(name.replace(/_V$/,""))}</strong><dl><div><dt>周期候选</dt><dd>${hz(c.timing.frequency_hz)}</dd></div><div><dt>峰峰值</dt><dd>${volts(c.statistics.vpp_v)}</dd></div><div><dt>RMS</dt><dd>${volts(c.statistics.rms_v)}</dd></div><div><dt>阈值占空比</dt><dd>${c.timing.duty_cycle_pct==null?"未建立":number(c.timing.duty_cycle_pct,1)+" %"}</dd></div></dl></div>`;
      }).join("");
    }
  }
  if(status.error)liveError(status.error);
  $("instrument-status").textContent=status.worker_alive
    ? status.config?.source==="demo"?"动态合成演示 · 未连接仪器":status.state==="running"?"仪器会话正在读取":"仪器会话建立 / 关闭中"
    : "当前未连接仪器";
  liveAge();liveButtons();
}
async function pollLive(epoch) {
  try {
    const status=await api("/api/live/frame");
    if(epoch!==live.epoch)return;
    if(status.stream_id!==live.stream)throw new Error("监看会话已被另一页面更改，请重新开始。");
    renderLive(status);
    if(status.worker_alive)live.timer=setTimeout(()=>pollLive(epoch),Math.max(200,Math.min(status.config.interval_ms,500)));
  } catch(error) {
    if(epoch!==live.epoch)return;
    liveError(`无法更新监看：${error.message} 请停止监看或恢复本地服务后重试。`);
    // Keep Stop available when the server may still own an instrument session.
    liveButtons();
  }
}
async function stopLive() {
  if(!live.stream||live.pending)return;
  ++live.epoch;clearTimeout(live.timer);live.pending=true;liveButtons();
  try {renderLive(await api("/api/live/stop",{stream_id:live.stream}));pollLive(live.epoch);}
  catch(error){liveError(error.message);}
  finally {live.pending=false;liveButtons();}
}
function showLiveWorkspace(show) {
  $("live-workspace").hidden=!show;$("offline-workspace").hidden=show;
  $("live-nav").classList.toggle("active",show);$("offline-nav").classList.toggle("active",!show);
  if(show){$("live-nav").setAttribute("aria-current","page");$("offline-nav").removeAttribute("aria-current");}
  else{$("offline-nav").setAttribute("aria-current","page");$("live-nav").removeAttribute("aria-current");draw();}
  document.querySelector(".breadcrumb strong").textContent=show?"实时示波":"离线分析";
  if(show&&live.frame?.plot)drawPlot(liveCanvas,live.frame.plot,{visible:live.visible});
}
$("live-nav").addEventListener("click",()=>showLiveWorkspace(true));
for(const id of ["offline-nav","evidence-nav"])$(id).addEventListener("click",e=>{if(live.pending){e.preventDefault();return;}if(live.status?.worker_alive)stopLive();showLiveWorkspace(false);});
for(const id of ["live-source","live-view"])$(id).addEventListener("change",()=>{liveError();clearLiveFrame();liveConfig();liveButtons();});
$("device-scan").addEventListener("click",()=>queryDevices());
$("device-identify").addEventListener("click",()=>queryDevices($("live-resource").value.trim()));
$("live-device").addEventListener("change",()=>{liveError();selectDevice(live.devices.find(d=>d.resource===$("live-device").value));});
$("live-resource").addEventListener("input",()=>{liveError();selectDevice(null);if($("live-resource").value.trim())$("device-info").textContent="地址已修改，请点击「识别这台设备」重新确认型号。";});
$("live-form").addEventListener("submit",async e=>{
  e.preventDefault();if(live.pending||live.status?.worker_alive)return;
  if($("live-source").value!=="demo"&&!live.device?.supported){liveError("请先识别并选择有可用适配器的设备。");return;}
  ++live.epoch;clearTimeout(live.timer);live.pending=true;liveError();clearLiveFrame();liveButtons();
  $("live-state").textContent="正在开始…";
  const request={source:$("live-source").value,view:$("live-view").value,channel:Number($("live-channel").value),interval_ms:Number($("live-interval").value)};
  if(request.source!=="demo"){request.resource=$("live-resource").value.trim();request.allow_configuration=$("live-ack").checked;}
  $("live-source-badge").textContent=request.source==="demo"?"动态合成演示":"等待仪器数据";
  try {const status=await api("/api/live/start",request);live.stream=status.stream_id;renderLive(status);pollLive(live.epoch);}
  catch(error){liveError(error.message);$("live-state").textContent="未能开始";}
  finally {live.pending=false;liveButtons();}
});
$("live-stop").addEventListener("click",stopLive);
$("live-freeze").addEventListener("click",async()=>{
  if(live.pending||!live.frame?.has_waveform)return;
  const sequence=live.frame.sequence;++live.epoch;clearTimeout(live.timer);live.pending=true;liveError();liveButtons();
  try {
    const record=await api("/api/live/snapshot",{stream_id:live.stream,sequence});
    showRecord(record,true);showLiveWorkspace(false);await loadPlot(record.start_s,record.end_s);
    notify(`已冻结帧 #${sequence}，使用完整采样完成本地分析。需要保存原始采样时，请导出报告。`,true);
  } catch(error){liveError(error.message);}
  finally {live.pending=false;pollLive(live.epoch);liveButtons();}
});
$("live-channels").addEventListener("click",e=>{
  const button=e.target.closest("[data-live-channel]");if(!button||!live.frame?.plot)return;
  const name=button.dataset.liveChannel;live.visible.has(name)?live.visible.delete(name):live.visible.add(name);
  button.setAttribute("aria-pressed",String(live.visible.has(name)));drawPlot(liveCanvas,live.frame.plot,{visible:live.visible});
});
new ResizeObserver(()=>{if(live.frame?.plot)drawPlot(liveCanvas,live.frame.plot,{visible:live.visible});}).observe($("live-display"));
setInterval(liveAge,500);
window.addEventListener("pagehide",()=>{if(live.stream)fetch("/api/live/stop",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({stream_id:live.stream}),keepalive:true}).catch(()=>{});});
liveConfig();liveButtons();
// Reloading the page reattaches to an existing session; it never starts a connection.
api("/api/live/frame").then(status=>{
  if(live.pending||live.stream||!status.stream_id||!status.worker_alive)return;
  const config=status.config;
  $("live-source").value=config.source==="demo"?"demo":"instrument";$("live-view").value=config.view;
  if(config.device){live.devices=[config.device];deviceOptions();selectDevice(config.device);}
  $("live-interval").value=String(config.interval_ms);$("live-channel").value=String(config.channel);
  $("live-resource").value=config.resource||"";$("live-ack").checked=config.source!=="demo";
  liveConfig();
  live.stream=status.stream_id;renderLive(status);
  if(status.worker_alive){showLiveWorkspace(true);pollLive(live.epoch);}
}).catch(()=>{});
