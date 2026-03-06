async function jget(url){const r=await fetch(url,{headers:{'Accept':'application/json'}});return r.json();}
async function jpost(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return r.json();}
async function refresh(){
  const [cfg, ups, tel] = await Promise.all([jget('/admin/api/config'), jget('/admin/api/upstreams'), jget('/admin/api/telemetry')]);
  document.getElementById('configEditor').value = JSON.stringify(cfg,null,2);
  document.getElementById('upstreams').textContent = JSON.stringify(ups,null,2);
  document.getElementById('telemetry').textContent = JSON.stringify(tel,null,2);
  document.getElementById('dashboard').textContent = JSON.stringify({upstream_status:ups, telemetry_status:tel},null,2);
  loadLogs();
}
async function validateConfig(){const cfg=JSON.parse(document.getElementById('configEditor').value);const out=await jpost('/admin/api/config/validate',{config:cfg});document.getElementById('configResult').textContent=JSON.stringify(out,null,2);}
async function previewDiff(){const cfg=JSON.parse(document.getElementById('configEditor').value);const out=await jpost('/admin/api/config',{config:cfg,dry_run:true});document.getElementById('configResult').textContent=JSON.stringify(out,null,2);}
async function applyConfig(){const cfg=JSON.parse(document.getElementById('configEditor').value);const out=await jpost('/admin/api/config',{config:cfg});document.getElementById('configResult').textContent=JSON.stringify(out,null,2);refresh();}
async function restartUpstream(){const name=document.getElementById('restartName').value;const out=await jpost('/admin/api/restart',{name});alert(JSON.stringify(out));refresh();}
async function sendTelemetry(){const out=await jpost('/admin/api/telemetry',{event:{event:'test_from_ui'}});alert(JSON.stringify(out));refresh();}
async function loadLogs(){const level=document.getElementById('logLevel').value; const upstream=document.getElementById('logUpstream').value;const q=new URLSearchParams();if(level)q.set('level',level);if(upstream)q.set('upstream',upstream);const out=await jget('/admin/api/logs?'+q.toString());document.getElementById('logs').textContent=JSON.stringify(out,null,2)}
refresh();
