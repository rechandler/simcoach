"""Optional local web overlay: http://127.0.0.1:8765/

Shows the latest callouts and per-corner deltas. Add it as an OBS browser
source, open it on a second screen/tablet, or embed it in any dashboard tool
that can show a web page. ``/state.json`` exposes the raw data.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from simcoach.coach.engine import CoachEngine, fmt_laptime

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>simcoach</title>
<style>
body{font:16px system-ui,sans-serif;background:#111;color:#eee;margin:12px}
h1{font-size:18px;margin:0 0 8px}.msg{padding:6px 8px;margin:4px 0;background:#222;border-radius:4px}
table{border-collapse:collapse;margin-top:10px}td,th{padding:3px 8px;text-align:right}
.up{color:#4c4}.down{color:#e55}
</style></head><body><h1 id="h">simcoach</h1><div id="m"></div><table id="t"></table>
<script>
async function tick(){
 try{const s=await (await fetch('state.json')).json();
 document.getElementById('h').textContent=s.session+'  ref '+s.reference;
 document.getElementById('m').innerHTML=s.callouts.slice(-4).reverse().map(c=>'<div class="msg">'+c+'</div>').join('');
 let rows='<tr><th>Corner</th><th>Last lap</th><th>Avg (5)</th></tr>';
 for(const c of s.corners){const cl=v=>v==null?'':(v>0?'down':'up');
  const f=v=>v==null?'-':(v>0?'+':'')+v.toFixed(2);
  rows+='<tr><td>'+c.name+'</td><td class="'+cl(c.last)+'">'+f(c.last)+'</td><td class="'+cl(c.avg)+'">'+f(c.avg)+'</td></tr>';}
 document.getElementById('t').innerHTML=rows;}catch(e){}
}
setInterval(tick,1000);tick();
</script></body></html>"""


def state(engine: CoachEngine) -> dict:
    snap = engine.snapshot()
    track, refm = snap["track"], snap["ref_metrics"]
    valid = [r for r in snap["history"] if r.lap.valid][-5:]
    corners = []
    for c in track.corners if track else []:
        rm = refm.get(c.number)
        vals = [r.metrics[c.number].segment_time - rm.segment_time for r in valid if rm and c.number in r.metrics]
        corners.append({"name": c.name, "last": vals[-1] if vals else None,
                        "avg": sum(vals) / len(vals) if vals else None})
    return {
        "session": snap["session"].display if snap["session"] else "",
        "reference": fmt_laptime(snap["reference"].lap_time) if snap["reference"] else "-",
        "laps": [{"lap": r.lap.lap_number, "time": r.lap.lap_time, "valid": r.lap.valid} for r in snap["history"]],
        "callouts": [f.message for _, _, f in snap["spoken"]],
        "corners": corners,
    }


def serve(engine: CoachEngine, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/state.json"):
                body, ctype = json.dumps(state(engine)).encode(), "application/json"
            else:
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=httpd.serve_forever, name="overlay", daemon=True).start()
    return httpd
