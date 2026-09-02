"""Extract the /slots delivery JS from ops_live.py and render it exactly as the worker does.

Kept separate from the harness so the harness always tests the REAL string the worker sends, not a
copy that can silently drift out of sync with apps/worker/ops_live.py.
"""
import json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = "/tmp/claude-1001/-srv-projects-storm-hub/7609ba5c-b27c-43a3-ac46-d6222e36f6d2/scratchpad/slots.js"

src = open(os.path.join(ROOT, "apps/worker/ops_live.py")).read()
m = re.search(r'js = """\n(.*?)\n        """ % \(', src, re.S)
if not m:
    sys.exit("could not find the delivery JS in ops_live.py -- did its shape change?")
rendered = m.group(1) % (json.dumps("TOKENVALUE"), json.dumps("/slots"), 25000, 1500)
open(OUT, "w").write(rendered)
print(f"rendered {len(rendered)} chars -> {OUT}")
