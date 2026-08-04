"""Anti-detection stealth — ported from storm-auto's confirmed fingerprint-completeness setup.

DataDome WAF-blocks a vanilla headless Chromium on the portal's auth page. storm-auto gets past it
with (1) NEW headless mode (`--headless=new`, far less detectable than old) and (2) a coherent
fingerprint script: TRUE webdriver removal, matching navigator.platform/userAgentData, a real chrome
object, plugins, mediaDevices (headless returns empty — a tell), speechSynthesis voices, battery,
and WebGL/audio noise. This module supplies that script + the matching client hints.

Coherent story: Chrome 128 on Windows (matches session._UA).
"""
from __future__ import annotations

_CHROME_VER = "128"
_PLATFORM = "Win32"
_UAD_PLATFORM = "Windows"
_PLAT_VER = "10.0.0"
_GL_VENDOR = "Google Inc. (Intel)"
_GL_RENDERER = "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"


def sec_ch_ua_headers() -> dict:
    """Client-hint headers matching the spoofed UA (Chrome 128 / Windows)."""
    return {
        "sec-ch-ua": f'"Chromium";v="{_CHROME_VER}", "Google Chrome";v="{_CHROME_VER}", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


STEALTH_SCRIPT = f"""
// ── TRUE webdriver removal (property absent, not just false getter) ──
try {{ delete Navigator.prototype.webdriver; }} catch(e){{}}
try {{ delete navigator.webdriver; }} catch(e){{}}
try {{ Object.defineProperty(navigator,'webdriver',{{get:()=>false}}); }} catch(e){{}}

// ── platform + userAgentData coherent with the UA (Chrome {_CHROME_VER} / Windows) ──
try {{ Object.defineProperty(navigator,'platform',{{get:()=>'{_PLATFORM}'}}); }} catch(e){{}}
(function(){{
  const brands=[{{brand:'Chromium',version:'{_CHROME_VER}'}},{{brand:'Google Chrome',version:'{_CHROME_VER}'}},{{brand:'Not_A Brand',version:'24'}}];
  const full=[{{brand:'Chromium',version:'{_CHROME_VER}.0.0.0'}},{{brand:'Google Chrome',version:'{_CHROME_VER}.0.0.0'}},{{brand:'Not_A Brand',version:'24.0.0.0'}}];
  const uad={{brands:brands,mobile:false,platform:'{_UAD_PLATFORM}',
    getHighEntropyValues:(h)=>Promise.resolve({{architecture:'x86',bitness:'64',brands:brands,
      fullVersionList:full,mobile:false,model:'',platform:'{_UAD_PLATFORM}',platformVersion:'{_PLAT_VER}',
      uaFullVersion:'{_CHROME_VER}.0.0.0',wow64:false}}),
    toJSON:()=>({{brands:brands,mobile:false,platform:'{_UAD_PLATFORM}'}})}};
  try {{ Object.defineProperty(navigator,'userAgentData',{{get:()=>uad,configurable:true}}); }} catch(e){{}}
}})();

// ── real chrome object ──
window.chrome={{runtime:{{}},loadTimes:function(){{}},csi:function(){{}},app:{{isInstalled:false}}}};

// ── realistic plugin + mimeType list (headless has none) ──
(function(){{
  const mk=(n,f,d)=>({{name:n,filename:f,description:d,length:1}});
  const plugins=[mk('PDF Viewer','internal-pdf-viewer','Portable Document Format'),
                 mk('Chrome PDF Viewer','internal-pdf-viewer','Portable Document Format'),
                 mk('Chromium PDF Viewer','internal-pdf-viewer','Portable Document Format')];
  try {{ Object.defineProperty(navigator,'plugins',{{get:()=>plugins}}); }} catch(e){{}}
  try {{ Object.defineProperty(navigator,'pdfViewerEnabled',{{get:()=>true}}); }} catch(e){{}}
}})();

// ── languages ──
try {{ Object.defineProperty(navigator,'languages',{{get:()=>['en-US','en']}}); }} catch(e){{}}

// ── mediaDevices coherent set (empty = headless tell) ──
try {{ navigator.mediaDevices.enumerateDevices=()=>Promise.resolve([
  {{deviceId:'default',kind:'audioinput',label:'',groupId:'g1'}},
  {{deviceId:'default',kind:'audiooutput',label:'',groupId:'g1'}},
  {{deviceId:'cam1',kind:'videoinput',label:'',groupId:'g2'}}]); }} catch(e){{}}

// ── speechSynthesis voices + battery ──
(function(){{
  const voices=[{{name:'Google US English',lang:'en-US',default:true,localService:false,voiceURI:'Google US English'}}];
  try {{ if(window.speechSynthesis) speechSynthesis.getVoices=()=>voices; }} catch(e){{}}
}})();
try {{ navigator.getBattery=()=>Promise.resolve({{charging:true,chargingTime:0,dischargingTime:Infinity,
  level:1.0,addEventListener:()=>{{}},removeEventListener:()=>{{}}}}); }} catch(e){{}}

// ── WebGL vendor/renderer (SwiftShader = headless tell) ──
(function(){{
  const patch=(proto)=>{{ if(!proto) return; const g=proto.getParameter;
    proto.getParameter=function(p){{ if(p===37445) return '{_GL_VENDOR}'; if(p===37446) return '{_GL_RENDERER}';
      return g.call(this,p); }}; }};
  try {{ patch(WebGLRenderingContext.prototype); }} catch(e){{}}
  try {{ patch(WebGL2RenderingContext.prototype); }} catch(e){{}}
}})();

// ── canvas noise (tiny, breaks exact-hash fingerprinting) ──
(function(){{
  const O=HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL=function(...a){{
    const ctx=this.getContext('2d');
    if(ctx){{ try{{ const d=ctx.getImageData(0,0,this.width,this.height);
      for(let i=0;i<d.data.length;i+=997){{ d.data[i]=(d.data[i]+1)&255; }}
      ctx.putImageData(d,0,0); }}catch(e){{}} }}
    return O.apply(this,a);
  }};
}})();

// ── permissions.query (notifications 'prompt' not 'denied' headless tell) ──
try {{
  const q=navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query=(p)=> p && p.name==='notifications'
    ? Promise.resolve({{state:Notification.permission==='default'?'prompt':Notification.permission}}) : q(p);
}} catch(e){{}}
"""
