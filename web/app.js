/* GTA Transit Reach — in-browser Connection Scan Algorithm over GTA GTFS data. */
(() => {
'use strict';

// ---------------------------------------------------------------- constants
const WALK = 1.3, DETOUR = 1.3, SLACK = 60, MAX_FOOT = 400, MAX_ACCESS = 800, MAX_WALK_TAIL = 1000;
const LAT0 = 43.7, MLAT = 111132, MLON = 111320 * Math.cos(LAT0 * Math.PI / 180);
const INF = 0x7fffffff;
const BLUE = ['#104281', '#1c5cab', '#2a78d6', '#5598e7', '#86b6ef', '#b7d3f6'];
const MODES = {0: 'Streetcar', 1: 'Subway', 2: 'Train', 3: 'Bus', 4: 'Ferry', 11: 'Trolleybus', 12: 'Monorail'};
const $ = id => document.getElementById(id);

// ---------------------------------------------------------------- state
const S = {
  index: null, stops: null, routes: null, x: null, y: null, lat: null, lon: null,
  fpPtr: null, fpTo: null, fpDur: null, grid: null,
  trips: {}, chunks: {}, live: null, liveMap: {},
  origin: null, dest: null, mode: 'reach', last: null,
};

// ---------------------------------------------------------------- map
const map = L.map('map', {zoomControl: false, preferCanvas: true}).setView([43.70, -79.42], 10);
L.control.zoom({position: 'topright'}).addTo(map);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
}).addTo(map);

const IsoLayer = L.Layer.extend({
  onAdd(m) {
    this._c = L.DomUtil.create('canvas', 'iso-canvas leaflet-zoom-hide');
    m.getPanes().overlayPane.appendChild(this._c);
    m.on('moveend zoomend resize', this._draw, this);
    this._draw();
  },
  onRemove(m) { L.DomUtil.remove(this._c); m.off('moveend zoomend resize', this._draw, this); },
  setData(d) { this._d = d; this._draw(); },
  _draw() {
    const m = this._map; if (!m) return;
    const size = m.getSize(), dpr = window.devicePixelRatio || 1, c = this._c;
    c.width = size.x * dpr; c.height = size.y * dpr; c.style.width = size.x + 'px'; c.style.height = size.y + 'px';
    L.DomUtil.setPosition(c, m.containerPointToLayerPoint([0, 0]));
    const ctx = c.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, size.x, size.y);
    const d = this._d; if (!d) return;
    const mpp = 40075016.686 * Math.cos(LAT0 * Math.PI / 180) / Math.pow(2, m.getZoom() + 8);
    const pts = d.lat.map((la, i) => m.latLngToContainerPoint([la, d.lon[i]]));
    for (let b = d.bands.length - 1; b >= 0; b--) {
      const r = d.radius[b];
      for (const [fill, extra] of [['#ffffff', 1.5], [d.colors[b], 0]]) {
        ctx.fillStyle = fill; ctx.beginPath();
        for (let i = 0; i < pts.length; i++) {
          if (r[i] <= 0) continue;
          const rp = r[i] / mpp + extra, p = pts[i];
          if (p.x + rp < 0 || p.y + rp < 0 || p.x - rp > size.x || p.y - rp > size.y) continue;
          ctx.moveTo(p.x + rp, p.y); ctx.arc(p.x, p.y, rp, 0, 2 * Math.PI);
        }
        ctx.fill();
      }
    }
  },
});
const iso = new IsoLayer().addTo(map);
const tripLayer = L.layerGroup().addTo(map);
map.createPane('trip').style.zIndex = 460;
const tripRenderer = L.canvas({pane: 'trip', padding: 0.3});
const vehLayer = L.layerGroup();
const markers = L.layerGroup().addTo(map);

// ---------------------------------------------------------------- route lines
// Drawn above the isochrone canvas, below markers.
map.createPane('lines').style.zIndex = 450;
const lineRenderer = L.canvas({pane: 'lines', padding: 0.3, tolerance: 4});
const LINE_STYLE = {
  subway: {weight: 5, opacity: 0.95}, lrt: {weight: 4.5, opacity: 0.95}, streetcar: {weight: 3, opacity: 0.85},
  train: {weight: 4, opacity: 0.9, dashArray: '10 4'}, bus: {weight: 1.6, opacity: 0.55},
};
const LINE_LABEL = {subway: 'Subway', lrt: 'LRT', streetcar: 'Streetcar', train: 'GO Train', bus: 'Bus'};
const lineLayers = {};
async function toggleLines(cat, on) {
  if (!lineLayers[cat]) {
    lineLayers[cat] = (async () => {
      const items = await getJSON(`data/lines/${cat}.json`);
      const g = L.layerGroup(), st = LINE_STYLE[cat];
      for (const r of items) {
        const latlngs = r.p.map(d => {
          const pts = []; let la = 0, lo = 0;
          for (let i = 0; i < d.length; i += 2) { la += d[i]; lo += d[i + 1]; pts.push([la / 1e5, lo / 1e5]); }
          return pts;
        });
        const name = `${r.f.toUpperCase()} ${LINE_LABEL[cat]} ${r.s}${r.l && r.l !== r.s ? ' · ' + r.l : ''}`;
        L.polyline(latlngs, {renderer: lineRenderer, color: r.c, ...st})
          .bindTooltip(esc(name), {sticky: true}).addTo(g);
      }
      return g;
    })();
  }
  const g = await lineLayers[cat];
  if (on) g.addTo(map); else map.removeLayer(g);
  // keep rapid transit on top of buses
  for (const c of ['streetcar', 'lrt', 'train', 'subway']) if (c !== cat && lineLayers[c]) lineLayers[c].then(l => map.hasLayer(l) && l.eachLayer(x => x.bringToFront()));
}
document.querySelectorAll('#lines input').forEach(cb => {
  const go = () => toggleLines(cb.dataset.cat, cb.checked).catch(e => status('Could not load ' + cb.dataset.cat + ' lines: ' + e.message));
  cb.addEventListener('change', go);
  if (cb.checked) go();
});

// ---------------------------------------------------------------- utils
const fmt = s => { s = ((s % 86400) + 86400) % 86400; return `${String(Math.floor(s / 3600)).padStart(2, '0')}:${String(Math.floor(s / 60) % 60).padStart(2, '0')}`; };
const proj = (lat, lon) => [(lon + 79.4) * MLON, (lat - LAT0) * MLAT];
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const status = t => { $('status').textContent = t; };
async function getJSON(u) { const r = await fetch(u, {cache: 'no-cache'}); if (!r.ok) throw new Error(`${u}: ${r.status}`); return r.json(); }
function feedKey(i) { return S.stops.feeds[i]; }
function feedColor(k) { return (S.index.feeds[k] || {}).color || '#555'; }
function dateRec() { return S.index.dates.find(d => d.date === $('date').value); }
function torontoToday() {
  return new Intl.DateTimeFormat('en-CA', {timeZone: 'America/Toronto', year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date());
}

// spatial grid for neighbour queries
function buildGrid(cell) {
  const g = new Map();
  for (let i = 0; i < S.x.length; i++) {
    const k = Math.floor(S.x[i] / cell) * 100000 + Math.floor(S.y[i] / cell);
    let a = g.get(k); if (!a) g.set(k, a = []); a.push(i);
  }
  return {cell, g};
}
function near(x, y, r) {
  const {cell, g} = S.grid, out = [], n = Math.ceil(r / cell);
  const cx = Math.floor(x / cell), cy = Math.floor(y / cell);
  for (let i = cx - n; i <= cx + n; i++) for (let j = cy - n; j <= cy + n; j++) {
    const a = g.get(i * 100000 + j); if (!a) continue;
    for (const s of a) { const d = Math.hypot(S.x[s] - x, S.y[s] - y); if (d <= r) out.push([s, d]); }
  }
  return out;
}

// ---------------------------------------------------------------- loading
async function init() {
  status('Loading stops…');
  S.index = await getJSON('data/index.json');
  const [stops, routes] = await Promise.all([getJSON('data/stops.json'), getJSON('data/routes.json')]);
  S.stops = stops; S.routes = routes;
  const n = stops.name.length;
  S.lat = new Float64Array(n); S.lon = new Float64Array(n); S.x = new Float64Array(n); S.y = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    S.lat[i] = stops.lat[i] / 1e5; S.lon[i] = stops.lon[i] / 1e5;
    const p = proj(S.lat[i], S.lon[i]); S.x[i] = p[0]; S.y[i] = p[1];
  }
  S.grid = buildGrid(MAX_FOOT);
  // footpaths (CSR)
  const lists = new Array(n);
  let total = 0;
  for (let i = 0; i < n; i++) {
    const nb = near(S.x[i], S.y[i], MAX_FOOT).filter(([s]) => s !== i);
    lists[i] = nb; total += nb.length;
  }
  S.fpPtr = new Int32Array(n + 1); S.fpTo = new Int32Array(total); S.fpDur = new Int32Array(total);
  for (let i = 0, k = 0; i < n; i++) {
    S.fpPtr[i] = k;
    for (const [s, d] of lists[i]) { S.fpTo[k] = s; S.fpDur[k] = Math.ceil(d * DETOUR / WALK); k++; }
  }
  S.fpPtr[n] = total;

  const sel = $('date'), today = torontoToday();
  for (const d of S.index.dates) {
    const o = document.createElement('option');
    const day = new Date(d.date + 'T12:00:00');
    o.value = d.date; o.textContent = (d.date === today ? 'Today · ' : '') + day.toLocaleDateString(undefined, {weekday: 'short', month: 'short', day: 'numeric'});
    sel.appendChild(o);
  }
  if (S.index.dates.some(d => d.date === today)) sel.value = today;
  setNow();
  readHash();
  await loadLive();
  setInterval(loadLive, 60000);
  status(statusText());
  if (S.origin) run();
}

function statusText() {
  const g = new Date(S.index.generated);
  let t = `Schedules built ${g.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'})}. `;
  if (S.live) {
    const age = Math.round((Date.now() / 1000 - S.live.epoch) / 60);
    t += `Live data ${age <= 1 ? 'just now' : age + ' min ago'}: ${Object.keys(S.live.delays).length.toLocaleString()} trips with delays, ${S.live.cancelled.length} cancelled.`;
  } else t += 'Live data unavailable — using schedules.';
  return t;
}

async function loadTrips(date) {
  if (!S.trips[date]) S.trips[date] = getJSON(`data/${date}/trips.json`);
  return S.trips[date];
}
async function loadChunk(date, h) {
  const key = date + '/' + h;
  if (!S.chunks[key]) {
    S.chunks[key] = fetch(`data/${date}/c${String(h).padStart(2, '0')}.bin`, {cache: 'no-cache'}).then(async r => {
      if (!r.ok) return null;
      const buf = await r.arrayBuffer(), n = new DataView(buf).getUint32(0, true);
      return {h, n, off: new Uint16Array(buf, 4, n), dur: new Uint16Array(buf, 4 + 2 * n, n),
        from: new Uint16Array(buf, 4 + 4 * n, n), to: new Uint16Array(buf, 4 + 6 * n, n),
        trip: new Uint32Array(buf, 4 + 8 * n, n)};
    });
  }
  return S.chunks[key];
}

async function loadLive() {
  const url = S.index.live_url;
  if (!url) return;
  try {
    S.live = await getJSON(url + (url.includes('?') ? '&' : '?') + 't=' + Math.floor(Date.now() / 60000));
    S.liveMap = {};
    if ($('veh').checked) drawVehicles();
    status(statusText());
  } catch (e) { console.warn('live data', e); }
}

// trip index -> delay seconds (and cancellations) for the given date
async function delaysFor(date) {
  const key = date + '|' + (S.live ? S.live.epoch : 0);
  if (S.liveMap[key]) return S.liveMap[key];
  const trips = await loadTrips(date), n = trips.id.length;
  const delay = new Int32Array(n), cancel = new Uint8Array(n);
  if (S.live) {
    const d0 = date.replace(/-/g, '');
    const prev = new Date(date + 'T12:00:00Z'); prev.setUTCDate(prev.getUTCDate() - 1);
    const d1 = prev.toISOString().slice(0, 10).replace(/-/g, '');
    const cset = new Set(S.live.cancelled);
    for (let i = 0; i < n; i++) {
      const k = `${feedKey(trips.feed[i])}|${trips.id[i]}|${trips.prev[i] ? d1 : d0}`;
      const v = S.live.delays[k]; if (v) delay[i] = v;
      if (cset.has(k)) cancel[i] = 1;
    }
  }
  return (S.liveMap[key] = {delay, cancel});
}

// assemble connections in [t0, t1] (+1h before when live delays may shift trips later)
async function connections(date, t0, t1, live) {
  const rec = S.index.dates.find(d => d.date === date);
  const h0 = Math.max(0, Math.floor(t0 / 3600) - (live ? 1 : 0)), h1 = Math.floor(t1 / 3600);
  const hours = rec.hours.filter(h => h >= h0 && h <= h1);
  const chunks = (await Promise.all(hours.map(h => loadChunk(date, h)))).filter(Boolean);
  let n = 0; for (const c of chunks) n += c.n;
  const dep = new Int32Array(n), arr = new Int32Array(n), from = new Uint16Array(n), to = new Uint16Array(n), trip = new Uint32Array(n);
  let k = 0;
  const dl = live ? await delaysFor(date) : null;
  for (const c of chunks) {
    const base = c.h * 3600;
    for (let i = 0; i < c.n; i++, k++) {
      let d = base + c.off[i], a = d + c.dur[i]; const t = c.trip[i];
      if (dl) { if (dl.cancel[t]) { d = INF; a = INF; } else { const x = dl.delay[t]; d += x; a += x; } }
      dep[k] = d; arr[k] = a; from[k] = c.from[i]; to[k] = c.to[i]; trip[k] = t;
    }
  }
  if (!dl) return {dep, arr, from, to, trip, n};
  // delays reorder connections; sort by departure
  const ord = new Uint32Array(n); for (let i = 0; i < n; i++) ord[i] = i;
  ord.sort((p, q) => dep[p] - dep[q] || arr[p] - arr[q]);
  const pick = (A, T) => { const B = new T(n); for (let i = 0; i < n; i++) B[i] = A[ord[i]]; return B; };
  return {dep: pick(dep, Int32Array), arr: pick(arr, Int32Array), from: pick(from, Uint16Array),
    to: pick(to, Uint16Array), trip: pick(trip, Uint32Array), n};
}

// ---------------------------------------------------------------- CSA
async function search(lat, lon, date, t0, minutes, live) {
  const t1 = t0 + minutes * 60;
  const C = await connections(date, t0, t1, live);
  const trips = await loadTrips(date), nT = trips.id.length, nS = S.x.length;
  const tau = new Int32Array(nS).fill(INF), ready = new Int32Array(nS).fill(INF);
  const kind = new Int8Array(nS).fill(-1), pEnter = new Int32Array(nS).fill(-1), pExit = new Int32Array(nS).fill(-1);
  const pFrom = new Int32Array(nS).fill(-1), access = new Int32Array(nS);
  const enter = new Int32Array(nT).fill(-1);
  const [ox, oy] = proj(lat, lon);
  for (const [s, d] of near(ox, oy, MAX_ACCESS)) {
    const w = Math.ceil(d * DETOUR / WALK); tau[s] = ready[s] = t0 + w; kind[s] = 0; access[s] = w;
  }
  let lo = 0, hi = C.n; while (lo < hi) { const m = (lo + hi) >> 1; if (C.dep[m] < t0) lo = m + 1; else hi = m; }
  const {dep, arr, from, to, trip} = C, fpPtr = S.fpPtr, fpTo = S.fpTo, fpDur = S.fpDur;
  for (let c = lo; c < C.n; c++) {
    const d = dep[c]; if (d > t1) break;
    const tr = trip[c];
    if (enter[tr] < 0) { if (ready[from[c]] > d) continue; enter[tr] = c; }
    const a = arr[c], v = to[c];
    if (a < tau[v]) {
      tau[v] = a; ready[v] = a + SLACK; kind[v] = 1; pEnter[v] = enter[tr]; pExit[v] = c;
      for (let k = fpPtr[v]; k < fpPtr[v + 1]; k++) {
        const w = fpTo[k], t = a + fpDur[k];
        if (t < tau[w]) { tau[w] = ready[w] = t; kind[w] = 2; pFrom[w] = v; }
      }
    }
  }
  return {lat, lon, date, t0, t1, tau, kind, pEnter, pExit, pFrom, access, C, trips, live};
}

function journey(R, stop) {
  const legs = []; let v = stop, guard = 0;
  while (R.kind[v] > 0 && guard++ < 60) {
    if (R.kind[v] === 2) { const u = R.pFrom[v]; legs.push({walk: true, from: u, to: v, dep: R.tau[u], arr: R.tau[v]}); v = u; }
    else {
      const ce = R.pEnter[v], cx = R.pExit[v], C = R.C, tr = C.trip[ce];
      const stops = [C.from[ce]];
      for (let c = ce; c <= cx; c++) if (C.trip[c] === tr) stops.push(C.to[c]);
      const ri = R.trips.route[tr], feed = feedKey(R.trips.feed[tr]);
      const route = ri >= 0 ? (S.routes.short[ri] || S.routes.long[ri]) : '';
      const type = ri >= 0 ? S.routes.type[ri] : 3;
      const delay = R.live ? (S.liveMap[R.date + '|' + (S.live ? S.live.epoch : 0)] || {delay: []}).delay[tr] || 0 : 0;
      const color = ri >= 0 && S.routes.color ? S.routes.color[ri] : feedColor(feed);
      legs.push({walk: false, feed, route, color, mode: MODES[type] || 'Transit', head: R.trips.headsigns[R.trips.headsign[tr]],
        from: C.from[ce], to: v, dep: C.dep[ce], arr: C.arr[cx], stops, delay, trip: tr, shape: R.trips.shape ? R.trips.shape[tr] : -1});
      v = C.from[ce];
    }
  }
  legs.push({walk: true, from: null, to: v, dep: R.t0, arr: R.t0 + R.access[v]});
  return legs.reverse();
}

// ---------------------------------------------------------------- actions
function bandsList() {
  const b = $('bands').value.split(/[ ,]+/).map(Number).filter(x => x > 0 && x <= 240);
  return [...new Set(b)].sort((a, c) => a - c).slice(0, 6);
}
function bandColors(n) {
  if (n <= 1) return [BLUE[2]];
  return Array.from({length: n}, (_, i) => BLUE[Math.round(i * (BLUE.length - 1) / (n - 1))]);
}
function departSecs() {
  const [h, m] = ($('time').value || '08:00').split(':').map(Number);
  return h * 3600 + m * 60;
}
function setNow() {
  const rec = S.index.dates.find(d => d.date === torontoToday());
  if (!rec) { $('time').value = '08:00'; return; }
  $('date').value = rec.date;
  $('time').value = fmt(Math.floor(Date.now() / 1000) - rec.midnight);
}
function liveActive() {
  return $('live').checked && !!S.live && $('date').value === torontoToday();
}

let runId = 0;
async function run() {
  if (!S.origin) return;
  const id = ++runId, bands = bandsList(); if (!bands.length) return;
  const date = $('date').value, t0 = departSecs(), live = liveActive();
  $('out').innerHTML = '<p class="muted">Computing…</p>';
  writeHash();
  const maxB = S.mode === 'trip' ? 240 : bands[bands.length - 1];
  const R = await search(S.origin[0], S.origin[1], date, t0, maxB, live);
  if (id !== runId) return;
  S.last = R;
  drawMarkers();
  if (S.mode === 'trip') { iso.setData(null); return showTrip(R, id); }
  tripLayer.clearLayers();
  map.getPane('lines').style.opacity = 1;
  // circles: every reached stop + origin
  const reached = [];
  for (let s = 0; s < R.tau.length; s++) if (R.tau[s] <= R.t1) reached.push(s);
  const lat = reached.map(s => S.lat[s]).concat([R.lat]), lon = reached.map(s => S.lon[s]).concat([R.lon]);
  const sp = WALK / DETOUR;
  const radius = bands.map(b => {
    const te = t0 + b * 60;
    return reached.map(s => Math.min((te - R.tau[s]) * sp, MAX_WALK_TAIL)).concat([Math.min(b * 60 * sp, MAX_WALK_TAIL)]);
  });
  const colors = bandColors(bands.length);
  iso.setData({lat, lon, radius, bands, colors});
  // stats: area by 100 m grid sampling, stops & agencies per band
  const xs = lat.map((la, i) => proj(la, lon[i])), cell = 100;
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  const rl = radius[radius.length - 1];
  xs.forEach(([x, y], i) => { const r = rl[i]; if (r <= 0) return; minx = Math.min(minx, x - r); maxx = Math.max(maxx, x + r); miny = Math.min(miny, y - r); maxy = Math.max(maxy, y + r); });
  const W = Math.ceil((maxx - minx) / cell) + 1, H = Math.ceil((maxy - miny) / cell) + 1;
  const grid = new Uint8Array(W * H).fill(255);
  for (let b = bands.length - 1; b >= 0; b--) {
    const r = radius[b];
    xs.forEach(([x, y], i) => {
      const ri = r[i]; if (ri <= 0) return;
      const gx0 = Math.floor((x - ri - minx) / cell), gx1 = Math.floor((x + ri - minx) / cell);
      const gy0 = Math.floor((y - ri - miny) / cell), gy1 = Math.floor((y + ri - miny) / cell);
      for (let gx = gx0; gx <= gx1; gx++) for (let gy = gy0; gy <= gy1; gy++) {
        const cx = minx + (gx + .5) * cell - x, cy = miny + (gy + .5) * cell - y;
        if (cx * cx + cy * cy <= ri * ri) { const k = gy * W + gx; if (grid[k] > b) grid[k] = b; }
      }
    });
  }
  const cnt = new Array(bands.length).fill(0);
  for (let k = 0; k < grid.length; k++) if (grid[k] !== 255) cnt[grid[k]]++;
  for (let b = 1; b < bands.length; b++) cnt[b] += cnt[b - 1];
  const rows = bands.map((b, i) => {
    const n = reached.filter(s => R.tau[s] <= t0 + b * 60).length;
    return `<tr><td><span class="sw" style="background:${colors[i]}"></span>${b} min</td>
      <td class="n">${Math.round(cnt[i] * cell * cell / 1e6).toLocaleString()} km²</td><td class="n">${n.toLocaleString()} stops</td></tr>`;
  }).join('');
  const per = {};
  for (const s of reached) { const f = feedKey(S.stops.feed[s]); per[f] = (per[f] || 0) + 1; }
  const ag = Object.entries(per).sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<span style="white-space:nowrap"><span class="dot" style="background:${feedColor(k)}"></span>${k.toUpperCase()} ${v.toLocaleString()}</span>`).join(' &nbsp;');
  // farthest reachable places
  let far = reached.map(s => [s, Math.hypot(S.x[s] - xs[xs.length - 1][0], S.y[s] - xs[xs.length - 1][1])]).sort((a, b) => b[1] - a[1]);
  const seen = new Set(), top = [];
  for (const [s, d] of far) { const nm = S.stops.name[s]; if (seen.has(nm)) continue; seen.add(nm); top.push([s, d]); if (top.length >= 5) break; }
  $('out').innerHTML = `<p class="summary">Departing ${fmt(t0)}${live ? ' · live delays' : ' · scheduled'}</p>
    <table>${rows}</table><p class="small">${ag}</p>
    <p class="small muted" style="margin-bottom:2px">Farthest stops reached</p>
    <table class="small">${top.map(([s, d]) => `<tr><td>${esc(S.stops.name[s])}</td><td class="n">${(d / 1000).toFixed(1)} km</td><td class="n">${Math.round((R.tau[s] - t0) / 60)} min</td></tr>`).join('')}</table>`;
}


// ---------------------------------------------------------------- snapping legs to route shapes
const shapeFiles = {};
function loadShapes(feed) {
  if (!shapeFiles[feed]) {
    shapeFiles[feed] = getJSON(`data/shapes/${feed}.json`).then(d => d.p.map(dl => {
      const n = dl.length / 2, x = new Float64Array(n), y = new Float64Array(n), lat = new Float64Array(n), lon = new Float64Array(n);
      let la = 0, lo = 0;
      for (let i = 0; i < n; i++) {
        la += dl[2 * i]; lo += dl[2 * i + 1]; lat[i] = la / 1e5; lon[i] = lo / 1e5;
        const p = proj(lat[i], lon[i]); x[i] = p[0]; y[i] = p[1];
      }
      return {x, y, lat, lon, n};
    })).catch(() => null);
  }
  return shapeFiles[feed];
}

// Project point (px,py) onto segment i of shape; returns [distance, t in 0..1].
function segProj(sh, i, px, py) {
  const ax = sh.x[i], ay = sh.y[i], dx = sh.x[i + 1] - ax, dy = sh.y[i + 1] - ay, L = dx * dx + dy * dy;
  let t = L > 0 ? ((px - ax) * dx + (py - ay) * dy) / L : 0;
  t = Math.max(0, Math.min(1, t));
  return [Math.hypot(ax + t * dx - px, ay + t * dy - py), t];
}

const SNAP_MAX = 150;  // metres: farther than this and we don't trust the snap

// Snap the leg's stops onto the shape *in order* (so loops and out-and-back routes don't jump),
// then return the shape geometry between consecutive stops. Falls back to straight lines.
function legGeometry(sh, stops) {
  const straight = stops.map(s => [S.lat[s], S.lon[s]]);
  if (!sh || sh.n < 2) return straight;
  const pos = [];  // [segment, t] per stop
  let from = 0;
  for (const s of stops) {
    const px = S.x[s], py = S.y[s];
    let best = Infinity;
    const d = new Float64Array(sh.n - 1).fill(Infinity);
    for (let i = from; i < sh.n - 1; i++) { d[i] = segProj(sh, i, px, py)[0]; if (d[i] < best) best = d[i]; }
    if (best > SNAP_MAX) { pos.push(null); continue; }
    // first segment (going forward) that is nearly as close as the best one
    let k = from; while (d[k] > best + 20) k++;
    const t = segProj(sh, k, px, py)[1];
    // never step backwards along the shape
    if (pos.length && pos[pos.length - 1] && (k < pos[pos.length - 1][0] || (k === pos[pos.length - 1][0] && t < pos[pos.length - 1][1]))) { pos.push(null); continue; }
    pos.push([k, t]); from = k;
  }
  const at = (k, t) => [sh.lat[k] + t * (sh.lat[k + 1] - sh.lat[k]), sh.lon[k] + t * (sh.lon[k + 1] - sh.lon[k])];
  const out = [straight[0]];
  for (let j = 1; j < stops.length; j++) {
    const a = pos[j - 1], b = pos[j];
    if (a && b) {
      out.push(at(a[0], a[1]));
      for (let k = a[0] + 1; k <= b[0]; k++) out.push([sh.lat[k], sh.lon[k]]);
      out.push(at(b[0], b[1]));
    }
    out.push(straight[j]);
  }
  return out;
}

async function showTrip(R, id) {
  tripLayer.clearLayers();
  map.getPane('lines').style.opacity = S.dest ? 0.3 : 1;
  if (!S.dest) { $('out').innerHTML = '<p class="muted">Now click the map (or search) to choose a destination.</p>'; return; }
  const [dx, dy] = proj(S.dest[0], S.dest[1]);
  let best = null;
  for (const [s, d] of near(dx, dy, MAX_ACCESS)) {
    if (R.tau[s] >= INF) continue;
    const t = R.tau[s] + Math.ceil(d * DETOUR / WALK);
    if (!best || t < best.t) best = {s, t, eg: t - R.tau[s]};
  }
  const [ox, oy] = proj(R.lat, R.lon), direct = Math.ceil(Math.hypot(dx - ox, dy - oy) * DETOUR / WALK);
  if (!best || direct <= best.t - R.t0) {
    if (direct <= 4 * 3600 && (!best || direct <= best.t - R.t0)) {
      L.polyline([[R.lat, R.lon], S.dest], {color: '#6b6a65', weight: 3, dashArray: '6 6'}).addTo(tripLayer);
      $('out').innerHTML = `<p class="summary">Walk ${Math.round(direct / 60)} min — faster than transit</p>`;
    } else $('out').innerHTML = '<p>No transit journey found within 4 hours.</p>';
    return;
  }
  const legs = journey(R, best.s), items = [];
  let prev = [R.lat, R.lon];
  for (const l of legs) {
    const toLL = [S.lat[l.to], S.lon[l.to]];
    if (l.walk) {
      const fromLL = l.from == null ? prev : [S.lat[l.from], S.lon[l.from]];
      L.polyline([fromLL, toLL], {renderer: tripRenderer, color: '#1f1f1d', weight: 3, dashArray: '2 6', lineCap: 'round'}).addTo(tripLayer);
      const m = Math.round((l.arr - l.dep) / 60);
      if (m >= 1) items.push(`<li class="walk"><span class="t">${fmt(l.dep)}</span>Walk ${m} min to ${esc(S.stops.name[l.to])}</li>`);
    } else {
      const shapes = l.shape >= 0 ? await loadShapes(l.feed) : null;
      if (id !== runId) return;  // a newer search replaced this one while shapes loaded
      const pts = legGeometry(shapes ? shapes[l.shape] : null, l.stops);
      L.polyline(pts, {renderer: tripRenderer, color: '#ffffff', weight: 11, opacity: 1, interactive: false}).addTo(tripLayer);
      L.polyline(pts, {renderer: tripRenderer, color: l.color, weight: 7, opacity: 1})
        .bindTooltip(`${l.feed.toUpperCase()} ${esc(l.route)} · ${esc(l.head)}`, {sticky: true}).addTo(tripLayer);
      const dl = Math.abs(l.delay) >= 30 ? ` <span class="${l.delay > 0 ? 'late' : 'early'}">${l.delay > 0 ? '+' : '−'}${Math.round(Math.abs(l.delay) / 60)} min</span>` : '';
      items.push(`<li style="border-left-color:${l.color}"><span class="t">${fmt(l.dep)}</span>
        <b>${l.feed.toUpperCase()} ${esc(l.mode)} ${esc(l.route)}</b>${dl}<br><span class="small">${esc(l.head)}</span><br>
        <span class="small muted">${esc(S.stops.name[l.from])} → ${esc(S.stops.name[l.to])} · ${l.stops.length - 1} stops · arr ${fmt(l.arr)}</span></li>`);
    }
    prev = toLL;
  }
  L.polyline([prev, S.dest], {renderer: tripRenderer, color: '#1f1f1d', weight: 3, dashArray: '2 6', lineCap: 'round'}).addTo(tripLayer);
  items.push(`<li class="walk"><span class="t">${fmt(best.t - best.eg)}</span>Walk ${Math.round(best.eg / 60)} min to destination</li>`);
  $('out').innerHTML = `<p class="summary">Arrive ${fmt(best.t)} · ${Math.round((best.t - R.t0) / 60)} min${R.live ? ' · live' : ''}</p><ul class="legs">${items.join('')}</ul>`;
  const b = tripLayer.getLayers().reduce((acc, l) => acc.extend(l.getBounds()), L.latLngBounds([[R.lat, R.lon], S.dest]));
  map.fitBounds(b, panelPadding());
}

function panelPadding() {
  const r = $('panel').getBoundingClientRect(), mobile = window.innerWidth <= 600;
  return mobile ? {paddingTopLeft: [20, 20], paddingBottomRight: [20, window.innerHeight - r.top + 20]}
    : {paddingTopLeft: [r.right + 20, 20], paddingBottomRight: [60, 20]};
}

function drawMarkers() {
  markers.clearLayers();
  if (S.origin) L.circleMarker(S.origin, {radius: 8, color: '#fff', weight: 3, fillColor: '#1f1f1d', fillOpacity: 1}).bindTooltip('Origin').addTo(markers);
  if (S.dest && S.mode === 'trip') L.circleMarker(S.dest, {radius: 8, color: '#fff', weight: 3, fillColor: '#b3261e', fillOpacity: 1}).bindTooltip('Destination').addTo(markers);
}

function drawVehicles() {
  vehLayer.clearLayers();
  const v = S.live && S.live.vehicles; if (!v) return;
  for (let i = 0; i < v.lat.length; i++) {
    const f = v.feeds[v.feed[i]];
    L.circleMarker([v.lat[i] / 1e5, v.lon[i] / 1e5], {radius: 3.5, weight: 1, color: '#fff', fillColor: feedColor(f), fillOpacity: .95})
      .bindTooltip(`${f.toUpperCase()} route ${esc(v.route[i])}`).addTo(vehLayer);
  }
}

function setPoint(ll) {
  if (S.mode === 'trip' && S.origin && (!S.dest || S._pickDest)) { S.dest = ll; S._pickDest = false; }
  else { S.origin = ll; if (S.mode === 'trip') { S.dest = null; S._pickDest = true; } }
  run();
}

// ---------------------------------------------------------------- hash state
function writeHash() {
  const p = new URLSearchParams();
  if (S.origin) p.set('o', S.origin.map(v => v.toFixed(5)).join(','));
  if (S.dest && S.mode === 'trip') p.set('d', S.dest.map(v => v.toFixed(5)).join(','));
  p.set('m', S.mode); p.set('date', $('date').value); p.set('t', $('time').value); p.set('b', bandsList().join(','));
  history.replaceState(null, '', '#' + p.toString());
}
function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const ll = k => { const v = p.get(k); if (!v) return null; const a = v.split(',').map(Number); return a.length === 2 && a.every(isFinite) ? a : null; };
  S.origin = ll('o'); S.dest = ll('d');
  if (p.get('m') === 'trip') setMode('trip', true);
  if (p.get('date') && S.index.dates.some(d => d.date === p.get('date'))) $('date').value = p.get('date');
  if (p.get('t')) $('time').value = p.get('t');
  if (p.get('b')) $('bands').value = p.get('b');
  if (S.origin) map.setView(S.origin, 11);
}

// ---------------------------------------------------------------- UI wiring
function setMode(m, quiet) {
  S.mode = m;
  $('modeReach').classList.toggle('on', m === 'reach'); $('modeTrip').classList.toggle('on', m === 'trip');
  $('hint').textContent = m === 'reach' ? 'Click the map, or search a stop, to see how far transit gets you.'
    : 'Click (or search) an origin, then a destination.';
  if (m === 'trip') S._pickDest = !!S.origin && !S.dest;
  if (!quiet) run();
}
$('modeReach').onclick = () => setMode('reach');
$('modeTrip').onclick = () => setMode('trip');
map.on('click', e => setPoint([e.latlng.lat, e.latlng.lng]));
for (const id of ['date', 'time', 'bands', 'live']) $(id).addEventListener('change', run);
$('now').onclick = () => { setNow(); run(); };
$('veh').onchange = () => { if ($('veh').checked) { drawVehicles(); vehLayer.addTo(map); } else map.removeLayer(vehLayer); };
$('collapse').onclick = () => { const c = $('panel').classList.toggle('collapsed'); $('collapse').textContent = c ? '+' : '–'; };

let sel = -1;
function renderResults() {
  const q = $('q').value.trim().toLowerCase(), ul = $('results');
  if (q.length < 2) { ul.hidden = true; return; }
  const seen = new Set(), res = [];
  for (let i = 0; i < S.stops.name.length && res.length < 10; i++) {
    const nm = S.stops.name[i];
    if (!nm.toLowerCase().includes(q)) continue;
    const key = nm + S.stops.feed[i]; if (seen.has(key)) continue; seen.add(key); res.push(i);
  }
  res.sort((a, b) => S.stops.name[a].length - S.stops.name[b].length);
  ul.innerHTML = res.map((i, k) => `<li data-i="${i}" class="${k === sel ? 'sel' : ''}"><span class="dot" style="background:${feedColor(feedKey(S.stops.feed[i]))}"></span>${esc(S.stops.name[i])}<span class="tag">${feedKey(S.stops.feed[i]).toUpperCase()}</span></li>`).join('');
  ul.hidden = !res.length;
}
function pick(i) {
  $('results').hidden = true; $('q').value = S.stops.name[i]; sel = -1;
  const ll = [S.lat[i], S.lon[i]]; map.setView(ll, Math.max(map.getZoom(), 11)); setPoint(ll);
}
$('q').addEventListener('input', () => { sel = -1; renderResults(); });
$('q').addEventListener('keydown', e => {
  const items = [...$('results').querySelectorAll('li')];
  if (e.key === 'ArrowDown') { sel = Math.min(sel + 1, items.length - 1); renderResults(); e.preventDefault(); }
  else if (e.key === 'ArrowUp') { sel = Math.max(sel - 1, 0); renderResults(); e.preventDefault(); }
  else if (e.key === 'Enter' && items.length) pick(+items[Math.max(sel, 0)].dataset.i);
  else if (e.key === 'Escape') $('results').hidden = true;
});
$('results').addEventListener('click', e => { const li = e.target.closest('li'); if (li) pick(+li.dataset.i); });

init().catch(e => { console.error(e); status('Failed to load data: ' + e.message); });
})();
