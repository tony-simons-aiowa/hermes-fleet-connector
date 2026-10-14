"""Self-contained Fleet viewer: no remote assets, storage, cookies or secret URLs."""
STYLE = """*{box-sizing:border-box}body{margin:0;background:#101827;color:#e9eef6;font:16px system-ui,-apple-system,sans-serif}main{max-width:780px;margin:32px auto;padding:24px}header{display:flex;gap:16px;align-items:center;margin-bottom:24px}header img{width:64px;height:64px}h1{font-size:32px;margin:0}p{line-height:1.55;color:#b9c6d6}.card{padding:28px;background:#192639;border:1px solid #344963;border-radius:18px}#qr{display:block;width:280px;height:280px;image-rendering:pixelated;margin:24px auto;border:14px solid white;background:white}button{font:inherit;cursor:pointer;padding:12px 18px;border-radius:9px;border:1px solid #58718d;background:#243a53;color:white}#continue{background:#c8e7fa;color:#102131;font-weight:650}label{display:block;margin:18px 0;line-height:1.5}input{margin-right:10px}#address{overflow-wrap:anywhere;color:#c8e7fa}.actions{display:flex;gap:12px;margin-top:24px}small{color:#9aabc0}.hidden{display:none!important}#status{font-weight:650;color:#c8e7fa}"""

SCRIPT = """'use strict';
const cfg=JSON.parse(document.getElementById('config').textContent);
const qr=document.getElementById('qr'), status=document.getElementById('status');
const checks=document.getElementById('checks'), clock=document.getElementById('clock');
let stopped=false, lastPhase='', currentPhase='', lastSuccess=performance.now(), awaiting=false;
document.getElementById('address').textContent=cfg.origin+'/fleet/health';
async function call(path,body){
 const opts={method:body?'POST':'GET',cache:'no-store',credentials:'omit',referrerPolicy:'no-referrer',
  headers:{'Authorization':'Viewer '+cfg.session}};
 if(body){opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body);}
 const res=await fetch(path,opts);if(!res.ok)throw new Error('refused');return res.json();
}
function clear(message){stopped=true;qr.removeAttribute('src');qr.classList.add('hidden');
 checks.classList.add('hidden');clock.textContent='';status.textContent=message;}
function closing(){if(stopped)return;clear('Closed. No QR remains on this page.');
 navigator.sendBeacon('/close',new Blob([JSON.stringify({session:cfg.session})],{type:'application/json'}));}
// A same-document entry makes the first Back cancel immediately, avoiding POST-resubmit dialogs.
history.pushState({fleet:true},'', '/display');
window.addEventListener('popstate',closing);
document.getElementById('close').addEventListener('click',closing);
window.addEventListener('pagehide',closing);
document.addEventListener('visibilitychange',()=>{
 if(stopped)return;
 if(document.visibilityState==='hidden'){
  if(currentPhase==='qr'||lastPhase==='qr')closing();
  else status.textContent='Bring this private window to the foreground to continue.';
  return;
 }
 if(currentPhase==='preflight'&&!awaiting)update();
});
document.getElementById('continue').addEventListener('click',async()=>{
 if(!document.getElementById('phone').checked||!document.getElementById('private').checked)return;
 document.getElementById('continue').disabled=true;
 status.textContent='Checking the private connection…';
 try{await call('/observe',{phone:true,private:true});if(stopped)return;status.textContent='Checks confirmed. Preparing your QR…';checks.classList.add('hidden');}
 catch{if(!stopped)clear('The checks could not be recorded. No QR was confirmed.');}
});
async function update(){
 if(stopped||awaiting)return;awaiting=true;
 try{
  const f=await call('/frame');if(stopped)return;lastSuccess=performance.now();
  if(!['preflight','qr'].includes(f.phase)){
   const messages={redeemed:'Invitation accepted. Finish connecting on your phone.',expired:'This attempt expired. Request a fresh QR when ready.',cancelled:'This attempt was cancelled.',disconnected:'Display disconnected. This attempt has stopped.',display_failed:'The display failed. No QR was confirmed.',interrupted:'The display stopped.'};
   clear(messages[f.phase]||'This attempt has stopped.');return;
  }
  currentPhase=f.phase;
  if(lastPhase!==f.phase){
   qr.src='data:image/png;base64,'+f.png;qr.classList.remove('hidden');await qr.decode();
   await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
   if(stopped)return;
   if(document.visibilityState!=='visible'){
    if(f.phase==='qr'){closing();return;}
    status.textContent='Bring this private window to the foreground to continue.';return;
   }
   const ack=await call('/event',{phase:f.phase,visible:true,width:qr.naturalWidth,height:qr.naturalHeight});
   if(stopped||!ack.ok)return;lastPhase=f.phase;
  }
  if(f.phase==='preflight'){
   status.textContent='Private display check — this QR cannot pair a phone.';
   checks.classList.toggle('hidden',f.observed);clock.textContent='Complete the checks below to show your pairing QR.';
  }else{status.textContent='Scan in Hermes Fleet DEV → Add Server.';checks.classList.add('hidden');clock.textContent=f.remaining+' seconds remaining';}
 }catch{clear('The private display disconnected. No QR remains on this page.');
  navigator.sendBeacon('/close',new Blob([JSON.stringify({session:cfg.session})],{type:'application/json'}));
 }finally{awaiting=false;}
}
setInterval(update,400);setInterval(()=>{if(!stopped&&performance.now()-lastSuccess>1800)closing();},250);update();"""

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hermes Fleet · Connect your phone</title><style>@@STYLE@@</style></head><body><main><header><img src="@@LOGO@@" alt="Fleet wing"><div><h1>Hermes Fleet</h1><p>Connect your phone from this private screen.</p></div></header><section class="card"><div id="status" role="status">Opening your private display…</div><img id="qr" class="hidden" alt="Private QR code"><p id="clock"></p><div id="checks" class="hidden"><p>On your iPhone, connect Tailscale and open this address. Check that it shows Hermes Fleet without a certificate warning.</p><p id="address"></p><label><input type="checkbox" id="phone">I checked the address on my phone and it opened without a certificate warning.</label><label><input type="checkbox" id="private">This screen is private, and I can see the display check above.</label><button id="continue">Show my pairing QR</button></div><p>On your phone, review the host, TLS trust and broad <strong>fleet:operator</strong> access, then tap <strong>Connect this phone</strong>. Your existing servers stay saved.</p><div class="actions"><button id="close">Close and cancel this attempt</button></div><p><small>Closing or navigating away stops the pending attempt. A hidden preflight waits for you to return; hiding a pairing QR cancels the attempt. This page uses no external assets and sends no QR link through Telegram.</small></p></section></main><script id="config" type="application/json">@@CONFIG@@</script><script>@@SCRIPT@@</script></body></html>"""
