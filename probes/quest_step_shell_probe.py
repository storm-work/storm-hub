"""Why is QuestNextQuestion step 1 answered with the bd.js shell? One login, no booking."""
import json, sys, time
from pathlib import Path
ROOT = Path("/srv/projects/storm-hub"); sys.path.insert(0, str(ROOT))
from probes.hybrid_booking_probe import _arg, _load_env, _warm
from urllib.parse import urlencode
_load_env()
from apps.hub.db import SessionLocal
from sqlalchemy import select
from libs.db import models as M
db = SessionLocal(); bk = db.scalar(select(M.Booking).where(M.Booking.name == "W-main"))
posto, nat, res = str(bk.posto_id), bk.nationality, bk.residence; db.close()
out = ROOT / "var" / "step_shell_probe.jsonl"
def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)
def rec(**k):
    with out.open("a") as f: f.write(json.dumps(k) + "\n")
ops, quest_steps, res = _warm("soax", "DE", posto, nat, res, log, login_only=True)
if ops is None: log("NO LOGIN"); sys.exit(1)
from apps.worker.ops_live import HOME_URL, QUEST_NEXT, QUEST_URL, _is_challenge_body
S = ops.session
steps = quest_steps.get(res if res != nat else nat, [])
url = f"{QUEST_NEXT}?{urlencode({'lang': 'PT', 'nacionalidade': nat, **steps[0]})}"
SHELL = lambda b: _is_challenge_body(b)
def reload():
    S.browser_eval("(async()=>{const r=await fetch('%s',{method:'POST',credentials:'include',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'lang=PT'});return r.status;})()" % HOME_URL)
    f = S.browser_nav(QUEST_URL, timeout=35, skip_cookie_sync=True)
    return len(ops._keepalive_read_page()), f
def report(tag, body, status=None, **x):
    r = dict(tag=tag, status=status, len=len(body or ""), shell=SHELL(body), head=(body or "")[:160].replace("\n", " "), **x)
    rec(**r); log(json.dumps(r))
log(f"reload -> {reload()}")
rec(ev="cookies", names=S.browser_eval("document.cookie.split(';').map(c=>c.split('=')[0].trim())"))
JSA = "(async()=>{const r=await fetch(%r,{method:'GET',credentials:'include',headers:{'Accept':'text/plain, */*; q=0.01','X-Requested-With':'XMLHttpRequest','Referer':%r}});const b=await r.text();return {status:r.status,body:b.substring(0,3000)};})()"
a = S.browser_eval(JSA % (url, QUEST_URL)); report("A fetch as keepalive", a["body"], a["status"])
JSB = "new Promise(res=>{const x=new XMLHttpRequest();x.open('GET',%r);x.setRequestHeader('X-Requested-With','XMLHttpRequest');x.onload=()=>res({status:x.status,body:x.responseText.substring(0,3000)});x.onerror=()=>res({status:0,body:'xhr error'});x.send();})"
b = S.browser_eval(JSB % url); report("B XHR", b["body"], b["status"])
# G: fetch with EXACTLY the headers the portal's own page sent in the HARs (data/ref/*.har):
#    Accept */*, Content-Type text/plain, no X-Requested-With, Referer, browser-set Sec-Fetch-*.
JSG = "(async()=>{const r=await fetch(%r,{method:'GET',credentials:'include',headers:{'Accept':'*/*','Content-Type':'text/plain'},referrer:%r});const b=await r.text();return {status:r.status,body:b.substring(0,3000)};})()"
g = S.browser_eval(JSG % (url, QUEST_URL)); report("G fetch with the HAR header set", g["body"], g["status"])
# F: the page's OWN function (what a human's select-change calls); the request, headers and initiator are the page's.
JSF = "new Promise(res=>{if(typeof goNext!=='function'){res({status:-1,body:'no goNext on this page'});return;}const seen=[];const ox=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.send=function(){this.addEventListener('load',()=>{seen.push({status:this.status,body:(this.responseText||'').substring(0,3000)});});return ox.apply(this,arguments);};const of=window.fetch;window.fetch=function(){return of.apply(this,arguments).then(async r=>{const c=r.clone();seen.push({status:r.status,body:(await c.text()).substring(0,3000)});return r;});};try{goNext(%s,%s);}catch(e){res({status:-2,body:String(e)});return;}setTimeout(()=>res(seen[0]||{status:0,body:'no request seen'}),6000);})"
import json as _j
f0 = steps[0]; f = S.browser_eval(JSF % (_j.dumps(f0.get("id_pergunta")), _j.dumps(f0.get("valor")))); report("F page's own goNext()", f["body"], f["status"])
# C: what does the page itself do?
info = S.browser_eval("(()=>{const q=[...document.querySelectorAll('input,select,button,a[onclick]')].slice(0,12).map(e=>({t:e.tagName,type:e.type,id:e.id,name:e.name,oc:(e.getAttribute('onclick')||e.getAttribute('onchange')||'').slice(0,80)}));return {title:document.title,len:document.documentElement.outerHTML.length,els:q,scripts:[...document.scripts].map(s=>s.src).filter(Boolean)};})()")
rec(ev="page", **info); log("page: " + json.dumps(info)[:700])
S.browser_eval("(()=>{window.__net=[];const of=window.fetch;window.fetch=function(){window.__net.push(['fetch',String(arguments[0]).slice(0,160)]);return of.apply(this,arguments).then(r=>{window.__net.push(['fetch-resp',r.status]);return r;});};const ox=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){window.__net.push(['xhr',m,String(u).slice(0,160)]);this.addEventListener('load',()=>window.__net.push(['xhr-resp',this.status,(this.responseText||'').length,/bd\\.js/.test(this.responseText||'')]));return ox.apply(this,arguments);};return 1;})()")
click = S.browser_eval("(()=>{const e=document.querySelector('input[type=radio],select');if(!e)return 'no control';if(e.tagName=='SELECT'){e.selectedIndex=Math.min(1,e.options.length-1);e.dispatchEvent(new Event('change',{bubbles:true}));return 'select '+e.id;}e.click();return 'radio '+e.id+'/'+e.name;})()")
log(f"UI action: {click}"); time.sleep(5)
net = S.browser_eval("window.__net"); rec(ev="ui_net", net=net); log("UI network: " + json.dumps(net)[:600])
# D/E: does the shell clear by itself, or after a fresh navigation?
time.sleep(8)
a2 = S.browser_eval(JSA % (url, QUEST_URL)); report("D fetch after 8s wait", a2["body"], a2["status"])
log(f"re-reload -> {reload()}")
a3 = S.browser_eval(JSA % (url, QUEST_URL)); report("E fetch after fresh reload", a3["body"], a3["status"])
try: ops._close_session_quietly()
except Exception: pass
log("DONE")
