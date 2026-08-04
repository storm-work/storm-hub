"""Anti-detection — replicates storm-auto's PROVEN-passing fingerprint (A/B 2026-08-04).

storm-auto clears DataDome bd.js with a COHERENT Chrome-131 / Windows identity (UA + sec-ch-ua +
userAgentData + platform all telling ONE consistent story) applied via the context UA override —
which also removes the "HeadlessChrome" HTTP-header tell — plus playwright-stealth. My earlier
failure was (a) an incoherent UA (Chrome-128 spoof + wrong OS) and (b) --headless=new. This module
supplies the exact coherent story; session.py uses old headless + this UA/headers.
"""
from __future__ import annotations

# storm-auto _FP_POOL index 1 — the plain Chrome 131 / Windows fingerprint.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
SEC_CH_UA = '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'
VIEWPORT = {"width": 1920, "height": 1080}
_PLATFORM = "Win32"
_UAD_PLATFORM = "Windows"
_VER = "131"


def sec_ch_ua_headers() -> dict:
    return {"sec-ch-ua": SEC_CH_UA, "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'}


# Coherent script: platform + userAgentData say Windows/131 to MATCH the UA header + sec-ch-ua.
STEALTH_SCRIPT = f"""
try {{ Object.defineProperty(navigator,'webdriver',{{get:()=>false}}); }} catch(e){{}}
try {{ Object.defineProperty(navigator,'platform',{{get:()=>'{_PLATFORM}'}}); }} catch(e){{}}
(function(){{
  const brands=[{{brand:'Google Chrome',version:'{_VER}'}},{{brand:'Chromium',version:'{_VER}'}},{{brand:'Not_A Brand',version:'24'}}];
  const full=[{{brand:'Google Chrome',version:'{_VER}.0.0.0'}},{{brand:'Chromium',version:'{_VER}.0.0.0'}},{{brand:'Not_A Brand',version:'24.0.0.0'}}];
  const uad={{brands:brands,mobile:false,platform:'{_UAD_PLATFORM}',
    getHighEntropyValues:(h)=>Promise.resolve({{architecture:'x86',bitness:'64',brands:brands,
      fullVersionList:full,mobile:false,model:'',platform:'{_UAD_PLATFORM}',platformVersion:'10.0.0',
      uaFullVersion:'{_VER}.0.0.0',wow64:false}}),
    toJSON:()=>({{brands:brands,mobile:false,platform:'{_UAD_PLATFORM}'}})}};
  try {{ Object.defineProperty(navigator,'userAgentData',{{get:()=>uad,configurable:true}}); }} catch(e){{}}
}})();
if (!window.chrome) window.chrome={{}};
window.chrome.runtime = window.chrome.runtime || {{}};
try {{ Object.defineProperty(navigator,'plugins',{{get:()=>[{{name:'PDF Viewer'}},{{name:'Chrome PDF Viewer'}},{{name:'Chromium PDF Viewer'}}]}}); }} catch(e){{}}
try {{ Object.defineProperty(navigator,'pdfViewerEnabled',{{get:()=>true}}); }} catch(e){{}}
try {{ Object.defineProperty(navigator,'languages',{{get:()=>['en-US','en']}}); }} catch(e){{}}
try {{ navigator.mediaDevices.enumerateDevices=()=>Promise.resolve([
  {{deviceId:'default',kind:'audioinput',label:'',groupId:'g1'}},
  {{deviceId:'default',kind:'audiooutput',label:'',groupId:'g1'}},
  {{deviceId:'cam1',kind:'videoinput',label:'',groupId:'g2'}}]); }} catch(e){{}}
try {{ if(window.speechSynthesis) speechSynthesis.getVoices=()=>[
  {{name:'Google US English',lang:'en-US',default:true,localService:false,voiceURI:'Google US English'}}]; }} catch(e){{}}
try {{ navigator.getBattery=()=>Promise.resolve({{charging:true,chargingTime:0,dischargingTime:Infinity,
  level:1.0,addEventListener:()=>{{}},removeEventListener:()=>{{}}}}); }} catch(e){{}}
try {{
  const q=navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query=(p)=> p && p.name==='notifications'
    ? Promise.resolve({{state:Notification.permission==='default'?'prompt':Notification.permission}}) : q(p);
}} catch(e){{}}
(function(){{
  const O=HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL=function(...a){{
    try {{ const ctx=this.getContext('2d');
      if(ctx){{ const d=ctx.getImageData(0,0,this.width,this.height);
        for(let i=0;i<d.data.length;i+=997){{ d.data[i]=(d.data[i]+1)&255; }}
        ctx.putImageData(d,0,0); }} }} catch(e){{}}
    return O.apply(this,a);
  }};
}})();
"""
