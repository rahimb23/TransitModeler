"""Connection Scan Algorithm (CSA) routing on the unified network.

One-to-all earliest arrival from a point: this powers both point-to-point
journey planning and isochrones ("how far can I get in N minutes").
"""
from __future__ import annotations

from dataclasses import dataclass

import numba
import numpy as np
import pandas as pd

from .network import ROUTE_TYPE_NAMES, Network

INF = np.int32(2**31 - 1)
ORIGIN, VEHICLE, WALK = 0, 1, 2


@numba.njit(cache=True)
def _csa(c_dep_stop, c_arr_stop, c_dep, c_arr, c_trip, n_trips,
         fp_ptr, fp_to, fp_dur, tau, ready, kind, p_enter, p_exit, p_from,
         t_start, t_end, slack, max_transfers):
    n_conn = c_dep.shape[0]
    trip_enter = np.full(n_trips, -1, dtype=np.int64)
    trip_legs = np.zeros(n_trips, dtype=np.int32)
    legs = np.zeros(tau.shape[0], dtype=np.int32)  # vehicles used to reach stop
    # binary search first connection departing >= t_start
    lo, hi = 0, n_conn
    while lo < hi:
        mid = (lo + hi) // 2
        if c_dep[mid] < t_start:
            lo = mid + 1
        else:
            hi = mid
    for c in range(lo, n_conn):
        d = c_dep[c]
        if d > t_end:
            break
        tr = c_trip[c]
        u = c_dep_stop[c]
        if trip_enter[tr] < 0:
            if ready[u] > d or legs[u] >= max_transfers + 1:
                continue
            trip_enter[tr] = c
            trip_legs[tr] = legs[u] + 1
        a = c_arr[c]
        v = c_arr_stop[c]
        if a < tau[v]:
            tau[v] = a
            ready[v] = a + slack
            kind[v] = 1
            p_enter[v] = trip_enter[tr]
            p_exit[v] = c
            legs[v] = trip_legs[tr]
            for k in range(fp_ptr[v], fp_ptr[v + 1]):
                w = fp_to[k]
                t = a + fp_dur[k]
                if t < tau[w]:
                    tau[w] = t
                    ready[w] = t
                    kind[w] = 2
                    p_from[w] = v
                    legs[w] = legs[v]
    return legs


@dataclass
class SearchResult:
    net: Network
    origin: tuple
    t_start: int
    t_end: int
    tau: np.ndarray      # earliest arrival per stop (seconds since midnight), INF if unreached
    kind: np.ndarray
    p_enter: np.ndarray
    p_exit: np.ndarray
    p_from: np.ndarray
    access_walk: np.ndarray  # seconds of initial walk per stop (if kind==ORIGIN)
    legs: np.ndarray

    @property
    def reached(self) -> np.ndarray:
        return np.flatnonzero(self.tau <= self.t_end)

    def stops_table(self) -> pd.DataFrame:
        idx = self.reached
        s = self.net.stops.iloc[idx][["feed", "stop_id", "stop_name", "stop_lat", "stop_lon"]].copy()
        s["arrival"] = [self.net.to_clock(t) for t in self.tau[idx]]
        s["minutes"] = (self.tau[idx] - self.t_start) / 60.0
        s["vehicles"] = self.legs[idx]
        s["stop_index"] = idx
        return s.sort_values("minutes").reset_index(drop=True)

    def journey(self, stop: int) -> list[dict]:
        """Reconstruct the leg list from origin to a stop."""
        net = self.net
        st = net.stop_times
        legs = []
        v = int(stop)
        guard = 0
        while self.kind[v] != ORIGIN and guard < 100:
            guard += 1
            if self.kind[v] == WALK:
                u = int(self.p_from[v])
                legs.append({"mode": "walk", "from": u, "to": v, "dep": int(self.tau[u]), "arr": int(self.tau[v])})
                v = u
            elif self.kind[v] == VEHICLE:
                ce, cx = int(self.p_enter[v]), int(self.p_exit[v])
                u = int(net.c_dep_stop[ce])
                trip = net.trips.iloc[int(net.c_trip[ce])]
                route = net.routes.iloc[trip["route_idx"]] if trip["route_idx"] >= 0 else None
                i0, i1 = int(net.c_st_dep[ce]), int(net.c_st_arr[cx])
                legs.append({
                    "mode": ROUTE_TYPE_NAMES.get(int(route["route_type"]), "Transit") if route is not None else "Transit",
                    "feed": trip["feed"],
                    "route": (route["route_short_name"] or route["route_long_name"]) if route is not None else trip["route_id"],
                    "headsign": trip["trip_headsign"], "trip_id": trip["trip_id"],
                    "from": u, "to": v, "dep": int(net.c_dep[ce]), "arr": int(net.c_arr[cx]),
                    "delay_s": int(st["dep"].iat[i0] - st["sched_dep"].iat[i0]),
                    "n_stops": i1 - i0,
                })
                v = u
            else:
                break
        legs.append({"mode": "walk", "from": None, "to": v, "dep": self.t_start,
                     "arr": self.t_start + int(self.access_walk[v])})
        return legs[::-1]

    def describe_journey(self, stop: int, egress_s: int = 0) -> str:
        net = self.net
        name = lambda i: net.stops["stop_name"].iat[i] if i is not None else "origin"
        out = []
        for leg in self.journey(stop):
            if leg["mode"] == "walk":
                mins = (leg["arr"] - leg["dep"]) / 60
                if mins >= 0.5:
                    out.append(f"  {net.to_clock(leg['dep'])}  walk {mins:.0f} min  {name(leg['from'])} -> {name(leg['to'])}")
            else:
                dly = leg["delay_s"]
                d = f"  [{'+' if dly >= 0 else ''}{dly // 60} min live]" if dly else ""
                out.append(f"  {net.to_clock(leg['dep'])}  {leg['feed'].upper()} {leg['mode']} {leg['route']} "
                           f"({leg['headsign']})  {name(leg['from'])} -> {name(leg['to'])}  "
                           f"arr {net.to_clock(leg['arr'])}, {leg['n_stops']} stops{d}")
        if egress_s:
            out.append(f"  {net.to_clock(self.tau[stop])}  walk {egress_s / 60:.0f} min to destination")
        return "\n".join(out)


def search(net: Network, lat: float, lon: float, t_start: int, max_minutes: float = 60,
           max_access_m: float = 800.0, max_transfers: int = 8) -> SearchResult:
    """Earliest arrival at every stop from (lat, lon) departing at t_start (secs since midnight)."""
    n = len(net.stops)
    t_end = int(t_start + max_minutes * 60)
    tau = np.full(n, INF, dtype=np.int32)
    ready = np.full(n, INF, dtype=np.int32)
    kind = np.full(n, -1, dtype=np.int8)
    p_enter = np.full(n, -1, dtype=np.int64)
    p_exit = np.full(n, -1, dtype=np.int64)
    p_from = np.full(n, -1, dtype=np.int32)
    access = np.zeros(n, dtype=np.int32)
    idx, d = net.stops_near(lat, lon, max_access_m)
    w = np.ceil(d * net.detour / net.walk_speed).astype(np.int32)
    tau[idx] = t_start + w
    ready[idx] = t_start + w
    kind[idx] = ORIGIN
    access[idx] = w
    legs = _csa(net.c_dep_stop, net.c_arr_stop, net.c_dep, net.c_arr, net.c_trip, len(net.trips),
                net.fp_ptr, net.fp_to, net.fp_dur, tau, ready, kind, p_enter, p_exit, p_from,
                np.int32(t_start), np.int32(t_end), np.int32(net.transfer_slack), max_transfers)
    return SearchResult(net, (lat, lon), t_start, t_end, tau, kind, p_enter, p_exit, p_from, access, legs)


def plan(net: Network, o_lat, o_lon, d_lat, d_lon, t_start: int, max_minutes=180, max_egress_m=800.0):
    """Point-to-point earliest arrival. Returns (result, best_stop, arrival_secs, egress_secs)."""
    res = search(net, o_lat, o_lon, t_start, max_minutes)
    idx, d = net.stops_near(d_lat, d_lon, max_egress_m)
    if not len(idx):
        return res, None, None, None
    eg = np.ceil(d * net.detour / net.walk_speed).astype(np.int64)
    total = res.tau[idx].astype(np.int64) + eg
    k = int(np.argmin(total))
    if res.tau[idx[k]] >= INF:
        return res, None, None, None
    return res, int(idx[k]), int(total[k]), int(eg[k])
