"""Tiny web app: click anywhere in the GTA to see how far transit gets you, using live delays."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .feeds import FEEDS
from .isochrone import isochrones
from .network import TZ, Network
from .realtime import refresh, vehicles
from .router import plan
from .viz import band_colors


class State:
    def __init__(self, feeds, refresh_s):
        self.feeds = feeds
        self.refresh_s = refresh_s
        self.lock = threading.RLock()
        self.net = None
        self.rt_stats = {}
        self.rt_time = None
        self._load()

    def _load(self):
        today = dt.datetime.now(TZ).date()
        net = Network(today, feeds=self.feeds)
        with self.lock:
            self.net = net
        self.update_rt()

    def update_rt(self):
        try:
            with self.lock:
                self.rt_stats = refresh(self.net, log=print)
                self.rt_time = dt.datetime.now(TZ)
        except Exception:
            traceback.print_exc()

    def loop(self):
        while True:
            time.sleep(self.refresh_s)
            if dt.datetime.now(TZ).date() != self.net.date and dt.datetime.now(TZ).hour >= 4:
                self._load()
            else:
                self.update_rt()


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GTA Transit Reach</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{--ink:#1f1f1d;--muted:#6b6a65;--bg:#fff;--line:#e4e3df}
@media (prefers-color-scheme:dark){:root{--ink:#ecebe7;--muted:#a3a29c;--bg:#1d1d1b;--line:#383835}}
html,body{margin:0;height:100%;font:13px/1.45 system-ui,sans-serif;color:var(--ink);background:var(--bg)}
#map{position:absolute;inset:0}
#panel{position:absolute;top:12px;left:12px;z-index:1000;background:var(--bg);padding:12px 14px;border-radius:8px;
box-shadow:0 1px 4px rgba(0,0,0,.3);width:300px;max-width:calc(100vw - 56px)}
h1{font-size:15px;margin:0 0 4px} .muted{color:var(--muted)} label{display:block;margin-top:8px}
input,select{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:3px 5px}
table{border-collapse:collapse;margin-top:8px;width:100%} td{padding:2px 0} td.n{text-align:right}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}
pre{white-space:pre-wrap;font-size:11px;margin:8px 0 0;max-height:40vh;overflow:auto}
</style></head><body><div id="map"></div>
<div id="panel"><h1>GTA Transit Reach</h1>
<div class="muted">Click the map to set an origin. Shift-click to plan a trip there.</div>
<label>Depart <input id="time" type="time"> <span class="muted">(blank = now)</span></label>
<label>Bands (min) <input id="bands" value="15,30,45,60" size="12"></label>
<label><input type="checkbox" id="veh"> Show live vehicles</label>
<div id="out"></div><div id="status" class="muted" style="margin-top:8px"></div></div>
<script>
const FEEDCOL = __FEEDCOL__;
const map = L.map('map').setView([43.70,-79.42],10);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
 {attribution:'&copy; OpenStreetMap contributors',maxZoom:19}).addTo(map);
let isoLayer=L.layerGroup().addTo(map), vehLayer=L.layerGroup(), tripLayer=L.layerGroup().addTo(map), origin=null;
const out=document.getElementById('out');
function q(o){return Object.entries(o).filter(([k,v])=>v!=='').map(([k,v])=>k+'='+encodeURIComponent(v)).join('&')}
async function iso(lat,lon){
  origin=[lat,lon]; out.innerHTML='<div class="muted" style="margin-top:8px">Computing…</div>';
  const r=await fetch('/api/isochrone?'+q({lat,lon,bands:bands.value,time:time.value})); const g=await r.json();
  if(g.error){out.textContent=g.error;return}
  const COLORS=g.colors;
  isoLayer.clearLayers(); tripLayer.clearLayers();
  const fs=g.features.slice().sort((a,b)=>b.properties.minutes-a.properties.minutes);
  const n=g.features.length;
  fs.forEach(f=>{const i=g.features.indexOf(f);
    L.geoJSON(f,{style:{fillColor:COLORS[i],color:'#fff',weight:2,fillOpacity:.55}})
     .bindTooltip(`Within ${f.properties.minutes} min: ${f.properties.area_km2.toLocaleString()} km²`).addTo(isoLayer)});
  L.circleMarker(origin,{radius:7,color:'#fff',weight:2,fillColor:'#1f1f1d',fillOpacity:1}).addTo(isoLayer);
  const p=g.features[0].properties;
  out.innerHTML=`<div style="margin-top:8px">Departing <b>${p.depart}</b>${g.realtime?' · live delays':''}</div><table>`+
   g.features.map((f,i)=>`<tr><td><span class="sw" style="background:${COLORS[i]}"></span>${f.properties.minutes} min</td>
   <td class="n">${Math.round(f.properties.area_km2).toLocaleString()} km²</td><td class="n">${f.properties.stops.toLocaleString()} stops</td></tr>`).join('')+
   '</table><div class="muted" style="margin-top:6px">'+Object.entries(g.features[n-1].properties.agencies)
   .sort((a,b)=>b[1]-a[1]).map(([k,v])=>k.toUpperCase()+' '+v).join(' · ')+'</div>';
}
async function trip(lat,lon){
  if(!origin){return iso(lat,lon)}
  const r=await fetch('/api/route?'+q({from:origin.join(','),to:lat+','+lon,time:time.value})); const j=await r.json();
  tripLayer.clearLayers();
  if(j.error){out.insertAdjacentHTML('beforeend','<pre>'+j.error+'</pre>');return}
  j.legs.forEach(l=>L.polyline(l.coords,{color:l.feed?FEEDCOL[l.feed]:'#6b6a65',weight:l.feed?5:3,dashArray:l.feed?null:'6 6'})
    .bindTooltip(l.label).addTo(tripLayer));
  L.marker([lat,lon]).addTo(tripLayer);
  const old=out.querySelector('pre'); if(old) old.remove();
  out.insertAdjacentHTML('beforeend','<pre>'+j.text.replace(/</g,'&lt;')+'</pre>');
}
map.on('click',e=>e.originalEvent.shiftKey?trip(e.latlng.lat,e.latlng.lng):iso(e.latlng.lat,e.latlng.lng));
async function loadVeh(){ if(!veh.checked) return; const r=await fetch('/api/vehicles'); const d=await r.json();
  vehLayer.clearLayers(); d.forEach(v=>L.circleMarker([v.lat,v.lon],{radius:3,weight:0,fillColor:FEEDCOL[v.feed],fillOpacity:.9})
   .bindTooltip(v.feed.toUpperCase()+' route '+v.route_id).addTo(vehLayer)); }
veh.onchange=()=>{ if(veh.checked){vehLayer.addTo(map);loadVeh()} else map.removeLayer(vehLayer) };
setInterval(loadVeh,30000);
async function stat(){const s=await (await fetch('/api/status')).json(); status.innerHTML=s.summary.replace(/\n/g,'<br>')}
stat(); setInterval(stat,60000);
</script></body></html>"""


def make_handler(state: State):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _t(self, net, qs):
            t = qs.get("time", [""])[0]
            if t:
                h, m = map(int, t.split(":"))
                return h * 3600 + m * 60, False
            return net.secs_of(dt.datetime.now(TZ)), True

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            try:
                if u.path == "/":
                    page = PAGE.replace("__FEEDCOL__", json.dumps({k: f.color for k, f in FEEDS.items()}))
                    b = page.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b)
                elif u.path == "/api/isochrone":
                    bands = [int(x) for x in qs.get("bands", ["15,30,45,60"])[0].split(",") if x.strip()][:6]
                    bands = [min(max(b, 1), 240) for b in bands]
                    with state.lock:
                        net = state.net
                        t, live = self._t(net, qs)
                        iso = isochrones(net, float(qs["lat"][0]), float(qs["lon"][0]), t, bands)
                    g = iso.geojson()
                    g["realtime"] = live and bool(net.rt_applied)
                    g["colors"] = band_colors(len(bands))
                    self._json(g)
                elif u.path == "/api/route":
                    o = list(map(float, qs["from"][0].split(",")))
                    d = list(map(float, qs["to"][0].split(",")))
                    with state.lock:
                        net = state.net
                        t, _ = self._t(net, qs)
                        res, stop, arr, eg = plan(net, o[0], o[1], d[0], d[1], t, 240)
                        if stop is None:
                            return self._json({"error": "No journey found within 4 hours."})
                        legs = []
                        prev = o
                        st = net.stop_times
                        for leg in res.journey(stop):
                            to = net.stops.iloc[leg["to"]]
                            if leg["mode"] == "walk":
                                fr = prev if leg["from"] is None else [net.stops.stop_lat.iat[leg["from"]],
                                                                       net.stops.stop_lon.iat[leg["from"]]]
                                legs.append({"coords": [fr, [to.stop_lat, to.stop_lon]], "label": "walk"})
                            else:
                                g = int(net.c_trip[res.p_enter[leg["to"]]])
                                rows = st.index[st["gt"] == g]
                                sids = st.loc[rows, "gs"].tolist()
                                i0 = sids.index(leg["from"])
                                i1 = i0 + sids[i0:].index(leg["to"])
                                pts = net.stops.iloc[sids[i0:i1 + 1]][["stop_lat", "stop_lon"]].values.tolist()
                                legs.append({"coords": pts, "feed": leg["feed"],
                                             "label": f"{leg['feed'].upper()} {leg['route']} {leg['headsign']}"})
                            prev = [to.stop_lat, to.stop_lon]
                        legs.append({"coords": [prev, d], "label": "walk"})
                        text = (f"Arrive {net.to_clock(arr)} ({(arr - t) / 60:.0f} min)\n"
                                + res.describe_journey(stop, eg))
                    self._json({"legs": legs, "text": text})
                elif u.path == "/api/vehicles":
                    df = vehicles(state.feeds)
                    self._json(df[["feed", "route_id", "lat", "lon"]].to_dict("records"))
                elif u.path == "/api/status":
                    with state.lock:
                        s = state.net.summary()
                    when = state.rt_time.strftime("%H:%M:%S") if state.rt_time else "never"
                    self._json({"summary": f"{s}\nrealtime refreshed {when}", "stats": state.rt_stats})
                else:
                    self.send_error(404)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)
    return H


def serve(port=8000, host="127.0.0.1", refresh_s=60, feeds=None):
    print("Building network…")
    state = State(feeds, refresh_s)
    threading.Thread(target=state.loop, daemon=True).start()
    print(state.net.summary())
    print(f"Serving on http://{host}:{port}")
    ThreadingHTTPServer((host, port), make_handler(state)).serve_forever()
