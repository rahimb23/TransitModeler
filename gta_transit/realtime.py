"""GTFS-Realtime: fetch TripUpdates / VehiclePositions and apply them to the network."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests
from google.transit import gtfs_realtime_pb2

from .feeds import FEEDS, rt_url
from .network import Network, project

MAX_ABS_DELAY = 45 * 60  # ignore absurd delays (bad predictions)
ROUTE_TIME_TOLERANCE = 20 * 60
VEHICLE_RADIUS_M = 200.0


def fetch(url: str, timeout=20) -> gtfs_realtime_pb2.FeedMessage:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "gta-transit-model/1.0"})
    r.raise_for_status()
    m = gtfs_realtime_pb2.FeedMessage()
    m.ParseFromString(r.content)
    return m


def fetch_all(kind="trip_updates", feeds=None, log=None) -> dict:
    """Fetch a realtime product for every feed that has one. Returns {key: FeedMessage}."""
    jobs = {}
    for k in feeds or FEEDS:
        f = FEEDS[k]
        url = rt_url(f, f.trip_updates_url if kind == "trip_updates" else f.vehicle_positions_url)
        if url:
            jobs[k] = url
    out = {}
    with ThreadPoolExecutor(8) as ex:
        futs = {k: ex.submit(fetch, u) for k, u in jobs.items()}
        for k, fu in futs.items():
            try:
                out[k] = fu.result()
            except Exception as e:
                if log:
                    log(f"  {k}: realtime fetch failed ({e})")
    return out


def _parse_trip_updates(msg) -> list[dict]:
    rows = []
    for e in msg.entity:
        if not e.HasField("trip_update"):
            continue
        tu = e.trip_update
        trip = tu.trip
        rel = trip.schedule_relationship
        stus = []
        for s in tu.stop_time_update:
            ev = s.departure if s.HasField("departure") else s.arrival
            arr_ev = s.arrival if s.HasField("arrival") else s.departure
            stus.append({
                "seq": s.stop_sequence if s.HasField("stop_sequence") else None,
                "stop_id": s.stop_id or None,
                "skipped": s.schedule_relationship == 1,
                "dep_delay": ev.delay if ev.HasField("delay") else None,
                "dep_time": ev.time if ev.HasField("time") and ev.time > 0 else None,
                "arr_delay": arr_ev.delay if arr_ev.HasField("delay") else None,
                "arr_time": arr_ev.time if arr_ev.HasField("time") and arr_ev.time > 0 else None,
            })
        rows.append({"trip_id": trip.trip_id, "route_id": trip.route_id, "start_date": trip.start_date,
                     "cancelled": rel == 3, "stus": stus})
    return rows


def apply_trip_updates(net: Network, messages: dict, vehicle_messages: dict | None = None, log=None) -> dict:
    """Shift stop_times of matched trips by realtime delays, drop cancelled trips, rebuild connections."""
    st = net.stop_times
    arr = st["arr"].to_numpy().copy()
    dep = st["dep"].to_numpy().copy()
    sched_arr = st["sched_arr"].to_numpy()
    sched_dep = st["sched_dep"].to_numpy()
    gt = st["gt"].to_numpy()
    seq = st["seq"].to_numpy()
    gs = st["gs"].to_numpy()
    trip_start = np.searchsorted(gt, np.arange(len(net.trips) + 1))
    trips = net.trips
    stop_ids = net.stops["stop_id"].to_numpy()
    stats = {}
    now = time.time()

    for key, msg in (vehicle_messages or {}).items():
        if FEEDS[key].rt_match != "vehicle_position":
            continue
        n, d, seen = _match_vehicles(net, key, msg, set(), trip_start, arr, dep, sched_arr, sched_dep)
        med = round(float(np.median(d)) / 60, 1) if d else 0.0
        stats[key] = {"updates": seen, "matched": n, "cancelled": 0, "median_delay_min": med,
                      "age_s": int(now - msg.header.timestamp) if msg.header.timestamp else None}
        net.rt_applied[key] = f"{n}/{seen} vehicles matched to scheduled trips, median delay {med} min"

    for key, msg in messages.items():
        feed = FEEDS[key]
        if feed.rt_match == "vehicle_position":
            continue
        rows = _parse_trip_updates(msg)
        sub = trips[trips["feed"] == key]
        # prefer today's service date instance when ids collide with previous-day trips
        by_id = {}
        for gidx, tid, shift in zip(sub.index.to_numpy(), sub["trip_id"].to_numpy(), sub["day_shift"].to_numpy()):
            if tid not in by_id or shift == 0:
                by_id[tid] = gidx
        matched = cancelled = 0
        delays = []
        used = set()
        for r in rows:
            g = by_id.get(r["trip_id"])
            if g is None:
                continue
            m = _apply_one(net, g, r, trip_start, arr, dep, sched_arr, sched_dep, seq, gs, stop_ids)
            if m is not None:
                matched += 1
                used.add(g)
                if r["cancelled"]:
                    cancelled += 1
                else:
                    delays.append(m)
        cur = [d for d in delays if d is not None]
        stats[key] = {"updates": len(rows), "matched": matched, "cancelled": cancelled,
                      "median_delay_min": round(float(np.median(cur)) / 60, 1) if cur else 0.0,
                      "age_s": int(now - msg.header.timestamp) if msg.header.timestamp else None}
        net.rt_applied[key] = (f"{matched}/{len(rows)} trips matched, {cancelled} cancelled, "
                               f"median delay {stats[key]['median_delay_min']} min")
    st["arr"] = arr
    st["dep"] = dep
    net.build_connections()
    return stats


def _delay_profile(net, g, r, trip_start, sched_arr, sched_dep, seq, gs, stop_ids):
    """Return per-stop (arr_delay, dep_delay) arrays for trip g given an update row, or None."""
    a, b = trip_start[g], trip_start[g + 1]
    if b <= a:
        return None
    t_seq = seq[a:b]
    t_sid = stop_ids[gs[a:b]]
    n = b - a
    known_a = np.full(n, np.nan)
    known_d = np.full(n, np.nan)
    for s in r["stus"]:
        if s["seq"] is not None:
            pos = np.flatnonzero(t_seq == s["seq"])
        elif s["stop_id"] is not None:
            pos = np.flatnonzero(t_sid == s["stop_id"])
        else:
            continue
        if not len(pos):
            continue
        p = pos[0]
        # schedule secs are relative to net.midnight (previous-day trips already shifted)
        sch_a = net.midnight + int(sched_arr[a + p])
        sch_d = net.midnight + int(sched_dep[a + p])
        da = s["arr_delay"] if s["arr_delay"] is not None else (s["arr_time"] - sch_a if s["arr_time"] else None)
        dd = s["dep_delay"] if s["dep_delay"] is not None else (s["dep_time"] - sch_d if s["dep_time"] else None)
        if da is None and dd is None:
            continue
        da = dd if da is None else da
        dd = da if dd is None else dd
        if abs(da) > MAX_ABS_DELAY or abs(dd) > MAX_ABS_DELAY:
            continue
        known_a[p], known_d[p] = da, dd
    if np.all(np.isnan(known_d)):
        return None
    # GTFS-RT propagation: a delay persists downstream until the next update.
    idx = np.where(~np.isnan(known_d), np.arange(n), -1)
    idx = np.maximum.accumulate(idx)
    first = np.flatnonzero(idx >= 0)[0]
    idx[:first] = first  # stops before first update: assume same delay (mostly already passed)
    dd = known_d[idx]
    # Arrival at a stop with no own update takes the departure delay carried from upstream.
    own = ~np.isnan(known_a)
    da = np.where(own, known_a, dd)
    return a, b, da, dd


def _write(a, b, da, dd, arr, dep, sched_arr, sched_dep):
    new_a = sched_arr[a:b] + da
    new_d = np.maximum(sched_dep[a:b] + dd, new_a)
    # times must not go backwards along a trip
    new_d = np.maximum.accumulate(new_d)
    new_a = np.minimum(np.maximum(new_a, np.concatenate([[new_a[0]], new_d[:-1]])), new_d)
    arr[a:b] = new_a.astype(arr.dtype)
    dep[a:b] = new_d.astype(dep.dtype)


def _apply_one(net, g, r, trip_start, arr, dep, sched_arr, sched_dep, seq, gs, stop_ids):
    if r["cancelled"]:
        net.trip_cancelled[g] = True
        return 0
    prof = _delay_profile(net, g, r, trip_start, sched_arr, sched_dep, seq, gs, stop_ids)
    if prof is None:
        return None
    a, b, da, dd = prof
    _write(a, b, da, dd, arr, dep, sched_arr, sched_dep)
    return float(np.median(dd))


def _match_vehicles(net, key, msg, used, trip_start, arr, dep, sched_arr, sched_dep):
    """Estimate delays from vehicle positions when realtime trip/stop ids don't match the
    static feed (TTC BusTime). For each vehicle: candidate static trips on the same route
    that pass a stop within VEHICLE_RADIUS_M heading the same way; delay = now - scheduled
    time at that stop. Assign greedily one-to-one by smallest |delay|."""
    trips = net.trips
    st = net.stop_times
    gt = st["gt"].to_numpy()
    gs = st["gs"].to_numpy()
    feed_mask = (net.stops["feed"] == key).to_numpy()
    route_of_row = trips["route_id"].to_numpy()[gt]
    # map global stop -> stop_times rows (for this feed only)
    rows_feed = np.flatnonzero(feed_mask[gs])
    order = np.argsort(gs[rows_feed], kind="stable")
    rows_sorted = rows_feed[order]
    gs_sorted = gs[rows_sorted]
    xy = net.xy
    cands = []
    vehicles_seen = 0
    for e in msg.entity:
        if not e.HasField("vehicle") or not e.vehicle.HasField("position"):
            continue
        v = e.vehicle
        rid = v.trip.route_id
        ts = v.timestamp or msg.header.timestamp
        if not rid or not ts:
            continue
        vehicles_seen += 1
        now_s = ts - net.midnight
        pxy = project([v.position.latitude], [v.position.longitude])[0]
        near = [i for i in net.tree.query_ball_point(pxy, VEHICLE_RADIUS_M) if feed_mask[i]]
        if not near:
            continue
        has_bearing = v.position.HasField("bearing")
        bvec = np.array([np.sin(np.radians(v.position.bearing)), np.cos(np.radians(v.position.bearing))])
        for sidx in near:
            lo, hi = np.searchsorted(gs_sorted, [sidx, sidx + 1])
            rr = rows_sorted[lo:hi]
            rr = rr[route_of_row[rr] == rid]
            if not len(rr):
                continue
            delay = now_s - sched_dep[rr]
            ok = np.abs(delay) <= ROUTE_TIME_TOLERANCE
            rr, delay = rr[ok], delay[ok]
            for row, d in zip(rr, delay):
                g = gt[row]
                if has_bearing:
                    a, b = trip_start[g], trip_start[g + 1]
                    nxt = row + 1 if row + 1 < b else row
                    prv = row - 1 if row - 1 >= a else row
                    seg = xy[gs[nxt]] - xy[gs[prv]]
                    nrm = np.linalg.norm(seg)
                    if nrm > 0 and seg @ bvec / nrm < 0.3:
                        continue  # wrong direction
                dist = np.linalg.norm(xy[sidx] - pxy)
                cands.append((abs(int(d)) + dist / 10.0, e.id, int(g), int(row), int(d)))
    cands.sort()
    taken_v, taken_t = set(), set(used)
    n, delays = 0, []
    for _, vid, g, row, d in cands:
        if vid in taken_v or g in taken_t:
            continue
        taken_v.add(vid)
        taken_t.add(g)
        a, b = trip_start[g], trip_start[g + 1]
        da = np.full(b - a, float(d))
        _write(a, b, da, da, arr, dep, sched_arr, sched_dep)
        n += 1
        delays.append(float(d))
    return n, delays, vehicles_seen


def vehicles(feeds=None, log=None) -> pd.DataFrame:
    """Current vehicle positions across all agencies with realtime feeds."""
    msgs = fetch_all("vehicle_positions", feeds, log)
    rows = []
    for k, m in msgs.items():
        for e in m.entity:
            if not e.HasField("vehicle"):
                continue
            v = e.vehicle
            if not v.HasField("position"):
                continue
            rows.append({"feed": k, "vehicle_id": v.vehicle.id or v.vehicle.label or e.id,
                         "label": v.vehicle.label, "trip_id": v.trip.trip_id, "route_id": v.trip.route_id,
                         "lat": v.position.latitude, "lon": v.position.longitude,
                         "bearing": v.position.bearing, "speed": v.position.speed,
                         "timestamp": v.timestamp})
    return pd.DataFrame(rows)


def refresh(net: Network, feeds=None, log=print) -> dict:
    """Reset to schedule, then fetch and apply the latest trip updates."""
    net.stop_times["arr"] = net.stop_times["sched_arr"]
    net.stop_times["dep"] = net.stop_times["sched_dep"]
    net.trip_cancelled[:] = False
    net.rt_applied = {}
    feeds = feeds or net.feeds
    msgs = fetch_all("trip_updates", [k for k in feeds if FEEDS[k].rt_match != "vehicle_position"], log)
    vmsgs = fetch_all("vehicle_positions", [k for k in feeds if FEEDS[k].rt_match == "vehicle_position"], log)
    return apply_trip_updates(net, msgs, vmsgs, log)
