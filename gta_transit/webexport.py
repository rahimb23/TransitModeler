"""Export the network for the static GitHub Pages site, and a live delay snapshot.

Static layout (written to OUT/data):
  index.json                 dates available, feeds, live URL, build time
  stops.json                 all stops (columnar)
  routes.json                all routes (columnar)
  <YYYY-MM-DD>/trips.json    trips active that day (route index, headsign, trip id, day shift)
  <YYYY-MM-DD>/c<HH>.bin     connections departing in hour HH, sorted by departure.
                             Layout: u32 n | u16 dep_off[n] | u16 dur[n] | u16 dep_stop[n]
                                     | u16 arr_stop[n] | u32 trip[n]   (little endian)
                             dep = HH*3600 + dep_off; arr = dep + dur.

Live snapshot (live.json): per-trip delay at each trip's next stop, cancelled trips and
vehicle positions, keyed by "feed|trip_id|service_date" so it is independent of indices.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time

import numpy as np

from .feeds import FEEDS
from .network import TZ, Network


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def export_static(out: str, days: int = 3, live_url: str | None = None, feeds=None, log=print):
    data = os.path.join(out, "data")
    today = dt.datetime.now(TZ).date()
    dates = [today + dt.timedelta(days=i) for i in range(days)]
    index = {"generated": dt.datetime.now(TZ).isoformat(timespec="seconds"), "dates": [], "live_url": live_url,
             "feeds": {k: {"name": f.name, "color": f.color,
                           "realtime": bool(f.trip_updates_url or f.vehicle_positions_url) and not f.rt_key_env}
                       for k, f in FEEDS.items()}}
    stops_written = False
    for d in dates:
        t0 = time.time()
        net = Network(d, feeds=feeds)
        n_stops = len(net.stops)
        assert n_stops < 65536, "stop index no longer fits in u16"
        if not stops_written:
            s = net.stops
            _write_json(os.path.join(data, "stops.json"), {
                "feeds": net.feeds,
                "feed": s["feed"].map({k: i for i, k in enumerate(net.feeds)}).tolist(),
                "name": s["stop_name"].tolist(),
                "lat": np.round(s["stop_lat"].to_numpy() * 1e5).astype(int).tolist(),
                "lon": np.round(s["stop_lon"].to_numpy() * 1e5).astype(int).tolist(),
            })
            r = net.routes
            _write_json(os.path.join(data, "routes.json"), {
                "feed": r["feed"].map({k: i for i, k in enumerate(net.feeds)}).tolist(),
                "short": r["route_short_name"].tolist(), "long": r["route_long_name"].tolist(),
                "type": r["route_type"].astype(int).tolist(),
            })
            stops_written = (net.feeds, n_stops)
        elif stops_written != (net.feeds, n_stops):
            raise RuntimeError("stop set differs between days; cannot share stops.json")

        ds = d.isoformat()
        ddir = os.path.join(data, ds)
        os.makedirs(ddir, exist_ok=True)
        t = net.trips
        heads, hidx = np.unique(t["trip_headsign"].fillna("").to_numpy().astype(str), return_inverse=True)
        _write_json(os.path.join(ddir, "trips.json"), {
            "route": t["route_idx"].astype(int).tolist(), "headsigns": heads.tolist(),
            "headsign": hidx.astype(int).tolist(), "id": t["trip_id"].tolist(),
            "prev": (t["day_shift"] != 0).astype(int).tolist(),
            "feed": t["feed"].map({k: i for i, k in enumerate(net.feeds)}).astype(int).tolist(),
        })
        keep = net.c_dep >= 0
        dep, arr = net.c_dep[keep], net.c_arr[keep]
        ds_, as_, tr = net.c_dep_stop[keep], net.c_arr_stop[keep], net.c_trip[keep]
        hours = dep // 3600
        total = 0
        hour_list = []
        for h in range(int(hours.max()) + 1):
            m = hours == h
            n = int(m.sum())
            if not n:
                continue
            dur = np.minimum(arr[m] - dep[m], 65535)
            buf = (np.array([n], "<u4").tobytes() + (dep[m] - h * 3600).astype("<u2").tobytes()
                   + dur.astype("<u2").tobytes() + ds_[m].astype("<u2").tobytes()
                   + as_[m].astype("<u2").tobytes() + tr[m].astype("<u4").tobytes())
            with open(os.path.join(ddir, f"c{h:02d}.bin"), "wb") as f:
                f.write(buf)
            total += len(buf)
            hour_list.append(h)
        index["dates"].append({"date": ds, "hours": hour_list, "trips": len(t), "connections": int(keep.sum()),
                               "midnight": net.midnight})
        log(f"  {ds}: {len(t):,} trips, {int(keep.sum()):,} connections, {total / 1e6:.1f} MB "
            f"({time.time() - t0:.1f}s)")
    _write_json(os.path.join(data, "index.json"), index)


def export_live(out_file: str, feeds=None, log=print):
    """Fetch realtime for today's network and write a compact live.json."""
    from .realtime import refresh, vehicles
    now = dt.datetime.now(TZ)
    # before 4am the active service day may still be yesterday's; cover both
    dates = [now.date()] + ([now.date() - dt.timedelta(days=1)] if now.hour < 4 else [])
    payload = {"generated": now.isoformat(timespec="seconds"), "epoch": int(now.timestamp()),
               "delays": {}, "cancelled": [], "stats": {}}
    for d in dates:
        net = Network(d, feeds=feeds)
        stats = refresh(net, log=log)
        payload["stats"].update(stats)
        st = net.stop_times
        now_s = net.secs_of(now)
        delta = (st["dep"] - st["sched_dep"]).to_numpy()
        gt = st["gt"].to_numpy()
        changed = np.unique(gt[delta != 0])
        if len(changed):
            sub = st[np.isin(gt, changed)]
            fut = sub[sub["sched_dep"] >= now_s]
            nxt = fut.groupby("gt").first()
            last = sub.groupby("gt").last()
            pick = last.copy()
            pick.loc[nxt.index] = nxt
            dly = (pick["dep"] - pick["sched_dep"]).astype(int)
            keys = net.trips.loc[dly.index, "trip_key"]
            for k, v in zip(keys, dly):
                if v:
                    payload["delays"][k] = int(v)
        payload["cancelled"] += net.trips.loc[net.trip_cancelled, "trip_key"].tolist()
    try:
        v = vehicles(feeds)
        order = list(FEEDS)
        payload["vehicles"] = {"feeds": order, "feed": v["feed"].map(order.index).tolist(),
                               "lat": np.round(v["lat"] * 1e5).astype(int).tolist(),
                               "lon": np.round(v["lon"] * 1e5).astype(int).tolist(),
                               "route": v["route_id"].tolist()}
    except Exception as e:  # vehicles are optional
        log(f"vehicles failed: {e}")
    _write_json(out_file, payload)
    log(f"live: {len(payload['delays'])} delayed trips, {len(payload['cancelled'])} cancelled, "
        f"{len(payload.get('vehicles', {}).get('lat', []))} vehicles -> {out_file}")
