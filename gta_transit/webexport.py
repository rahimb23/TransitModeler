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
    shape_index = None
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
                "color": [("#" + c.strip().lstrip("#")) if len(c.strip().lstrip("#")) == 6
                          and c.strip().lstrip("#").upper() not in ("FFFFFF", "000000") else FEEDS[f].color
                          for c, f in zip(r["route_color"].fillna(""), r["feed"])],
            })
            stops_written = (net.feeds, n_stops)
        elif stops_written != (net.feeds, n_stops):
            raise RuntimeError("stop set differs between days; cannot share stops.json")

        ds = d.isoformat()
        ddir = os.path.join(data, ds)
        os.makedirs(ddir, exist_ok=True)
        t = net.trips
        heads, hidx = np.unique(t["trip_headsign"].fillna("").to_numpy().astype(str), return_inverse=True)
        if shape_index is None:
            shape_index = export_shapes(data, net.feeds, log=log)
        shp = [shape_index.get(f, {}).get(sid, -1) for f, sid in zip(t["feed"], t["shape_id"].fillna(""))]
        _write_json(os.path.join(ddir, "trips.json"), {
            "route": t["route_idx"].astype(int).tolist(), "headsigns": heads.tolist(),
            "headsign": hidx.astype(int).tolist(), "id": t["trip_id"].tolist(),
            "prev": (t["day_shift"] != 0).astype(int).tolist(),
            "feed": t["feed"].map({k: i for i, k in enumerate(net.feeds)}).astype(int).tolist(),
            "shape": shp,
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
    index["lines"] = export_lines(data, feeds, log=log)
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


# ----------------------------------------------------------------------------- route lines
LINE_CATEGORIES = ["subway", "lrt", "streetcar", "train", "bus"]


def route_category(feed: str, rtype: int, short: str, long: str) -> str:
    if rtype in (1, 12):
        return "subway"
    if rtype == 0:
        # TTC files Lines 5/6 (LRT) under the same type as streetcars.
        if feed == "ttc" and not (short in ("5", "6") or long.lower().startswith("line")):
            return "streetcar"
        return "lrt"
    if rtype == 2:
        return "train"
    return "bus"


def export_lines(data_dir: str, feeds=None, tolerance_m: float = 8.0, log=print):
    """Write data/lines/<category>.json with one simplified geometry per route direction."""
    import pandas as pd
    import shapely

    from .feeds import CACHE_DIR
    from .network import project, unproject

    out = {c: [] for c in LINE_CATEGORIES}
    merged = {}  # (feed, category, short name) -> entry; some feeds split a line over several route_ids
    for key in feeds or FEEDS:
        p = os.path.join(CACHE_DIR, key)
        if not os.path.exists(os.path.join(p, "shapes.parquet")):
            continue
        routes = pd.read_parquet(os.path.join(p, "routes.parquet"))
        trips = pd.read_parquet(os.path.join(p, "trips.parquet"))
        shapes = pd.read_parquet(os.path.join(p, "shapes.parquet"))
        stops = pd.read_parquet(os.path.join(p, "stops.parquet"))
        st = None
        sh_groups = {k: g for k, g in shapes.groupby("shape_id")} if len(shapes) else {}
        for r in routes.itertuples():
            rt = trips[trips["route_id"] == r.route_id]
            if rt.empty:
                continue
            cat = route_category(key, int(r.route_type), str(r.route_short_name), str(r.route_long_name))
            mkey = (key, cat, r.route_short_name or r.route_long_name)
            entry = merged.get(mkey)
            geoms = entry["geoms"] if entry else []
            n_before = len(geoms)
            for _, grp in rt.groupby("direction_id"):
                sid = grp["shape_id"].replace("", pd.NA).dropna()
                coords = None
                if len(sid) and sid.mode().iloc[0] in sh_groups:
                    g = sh_groups[sid.mode().iloc[0]]
                    coords = g[["lat", "lon"]].to_numpy()
                else:  # no shape: connect the stops of the longest trip
                    if st is None:
                        st = pd.read_parquet(os.path.join(p, "stop_times.parquet"))
                    ti = trips.index[trips["trip_id"].isin(grp["trip_id"])]
                    sub = st[st["t"].isin(ti)]
                    if sub.empty:
                        continue
                    longest = sub.groupby("t").size().idxmax()
                    s_idx = sub[sub["t"] == longest].sort_values("seq")["s"].to_numpy()
                    coords = stops.iloc[s_idx][["stop_lat", "stop_lon"]].to_numpy()
                if coords is None or len(coords) < 2:
                    continue
                line = shapely.simplify(shapely.linestrings(project(coords[:, 0], coords[:, 1])), tolerance_m)
                if any(shapely.equals(line, g) or shapely.hausdorff_distance(line, g) < 30 for g in geoms):
                    continue  # the other direction runs on the same streets
                geoms.append(line)
            if entry:
                entry["new"] += geoms[n_before:]
                continue
            if not geoms:
                continue
            color = (r.route_color or "").strip().lstrip("#")
            if len(color) != 6 or color.upper() in ("FFFFFF", "000000"):
                color = FEEDS[key].color.lstrip("#")
            entry = {"f": key, "s": r.route_short_name, "l": r.route_long_name, "c": "#" + color,
                     "geoms": geoms, "new": list(geoms)}
            merged[mkey] = entry
            out[cat].append(entry)
    for cat, items in out.items():
        for e in items:
            paths = []
            for g in e.pop("new"):
                lat, lon = unproject(shapely.get_coordinates(g))
                q = np.column_stack([np.round(lat * 1e5), np.round(lon * 1e5)]).astype(int)
                d = np.vstack([q[:1], np.diff(q, axis=0)])  # delta-encode
                paths.append(d.ravel().tolist())
            e.pop("geoms")
            e["p"] = paths
    for cat, items in out.items():
        _write_json(os.path.join(data_dir, "lines", f"{cat}.json"), items)
        size = os.path.getsize(os.path.join(data_dir, "lines", f"{cat}.json"))
        log(f"  lines/{cat}.json: {len(items)} routes, {size / 1e6:.2f} MB")
    return {c: len(v) for c, v in out.items()}


def export_shapes(data_dir: str, feeds, tolerance_m: float = 5.0, log=print) -> dict:
    """Write data/shapes/<feed>.json (delta-encoded 1e-5 degree paths) for every shape used by
    a trip, so the site can draw trip legs along the real route. Returns {feed: {shape_id: idx}}."""
    import pandas as pd
    import shapely

    from .feeds import CACHE_DIR
    from .network import project, unproject

    index = {}
    for key in feeds:
        p = os.path.join(CACHE_DIR, key, "shapes.parquet")
        if not os.path.exists(p):
            continue
        sh = pd.read_parquet(p)
        used = set(pd.read_parquet(os.path.join(CACHE_DIR, key, "trips.parquet"))["shape_id"])
        ids, paths = [], []
        for sid, g in sh.groupby("shape_id", sort=False):
            if sid not in used or len(g) < 2:
                continue
            line = shapely.simplify(shapely.linestrings(project(g["lat"].to_numpy(), g["lon"].to_numpy())),
                                    tolerance_m)
            lat, lon = unproject(shapely.get_coordinates(line))
            q = np.column_stack([np.round(lat * 1e5), np.round(lon * 1e5)]).astype(int)
            d = np.vstack([q[:1], np.diff(q, axis=0)])
            ids.append(sid)
            paths.append(d.ravel().tolist())
        _write_json(os.path.join(data_dir, "shapes", f"{key}.json"), {"p": paths})
        index[key] = {sid: i for i, sid in enumerate(ids)}
        size = os.path.getsize(os.path.join(data_dir, "shapes", f"{key}.json"))
        log(f"  shapes/{key}.json: {len(ids)} shapes, {size / 1e6:.2f} MB")
    return index
