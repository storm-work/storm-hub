// Runs the /slots interceptor JS from apps/worker/ops_live.py OUTSIDE the portal, against a fake
// page, to prove two things the Python test suite cannot reach because the code is JavaScript:
//
//   1. clone() does not break the page. The interceptor reads the response body; if it consumed the
//      original stream instead of a clone, the portal's own handler would receive nothing and
//      discovery would fail everywhere. "page read from its own body" must be non-zero in every row.
//   2. The probe separates responses that leave IDENTICAL residue in window.data. A 200 carrying an
//      empty object, a 429, and an HTML interstitial served as 200 are indistinguishable downstream
//      -- that ambiguity is what made 206 of 214 rounds on the 2026-09-01 run undiagnosable.
//
// Usage (needs node, and the rendered JS written by the extractor below):
//   python3 probes/slots_probe_render.py && node probes/slots_probe_harness.mjs
//
// This is a probe, not a pytest: it exercises browser JS, so it is run by hand when the delivery JS
// changes rather than on every suite run.

import fs from 'fs';
const body = fs.readFileSync('/tmp/claude-1001/-srv-projects-storm-hub/7609ba5c-b27c-43a3-ac46-d6222e36f6d2/scratchpad/slots.js','utf8');

async function run(label, serverBody, status, ct) {
  const window = {};
  globalThis.window = window;
  globalThis.XMLHttpRequest = function(){}; 
  globalThis.XMLHttpRequest.prototype = {open(){}, send(){}};
  // the "network": returns a real Response the page will read
  window.fetch = async (u) => new Response(serverBody, {status, headers:{'content-type':ct}});
  // the portal's own handler: fetches /slots and populates window.data FROM THE BODY
  window.onCaptchaSuccess = (t) => {
    window.fetch('/VistosOnline/slots?posto_id=3059', {method:'POST'})
      .then(r => r.text())
      .then(b => { window.pageSawBytes = b.length; try { window.data = JSON.parse(b); } catch(e){ window.data = {}; } });
  };
  const res = await eval(body);
  console.log(`\n== ${label}`);
  console.log('   page read from its own body :', window.pageSawBytes, 'bytes  (0 or undefined = clone() BROKE the page)');
  console.log('   probe                       :', JSON.stringify(res.probe));
  console.log('   outcome keys                :', Object.keys(res).filter(k=>k!=='probe').join(','));
  return {res, window};
}

const good = JSON.stringify([{date:'2026-09-08', periods:[{id:3,description:'8-9'}]}]);
await run('healthy 200 with a real slot array', good, 200, 'application/json');
await run('the 206-case: 200 with an empty object', JSON.stringify({data:{}}), 200, 'application/json');
await run('a 429 the residue could never reveal', 'Too Many Requests', 429, 'text/plain');
await run('a DataDome/HTML interstitial served as 200', '<html><body>captcha-delivery</body></html>', 200, 'text/html');
