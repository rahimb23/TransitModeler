"""Unified multi-agency GTA transit network for a single service day.

The network is represented as a time-sorted array of *connections* (one vehicle
moving between two consecutive stops) plus a walking-transfer graph between
nearby stops. This is the input to the Connection Scan Algorithm in router.py.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from .feeds import CACHE_DIR, FEEDS

TZ = ZoneInfo("America/Toronto")
LAT0 = 43.7
M_PER_DEG_LAT = 111_132.0
M_PER_DEG_LON = 111_320.0 * np.cos(np.radians(LAT0))

ROUTE_TYPE_NAMES = {0: "Streetcar/LRT", 1: "Subway", 2: "Rail", 3: "Bus", 4: "Ferry", 5: "Cable car",
                    6: "Gondola", 7: "Funicular", 11: "Trolleybus", 12: "Monorail"}


def project(lat, lon):
    """Equirectangular projection to metres around the GTA (good to <0.5% here)."""
    return np.column_stack([(np.asarray(lon) + 79.4) * M_PER_DEG_LON, (np.asarray(lat) - LAT0) * M_PER_DEG_LAT])


def unproject(xy):
    xy = np.asarray(xy)
    return xy[:, 1] / M_PER_DEG_LAT + LAT0, xy[:, 0] / M_PER_DEG_LON - 79.4


def service_midnight(date: dt.date) -> int:
    """Unix time of GTFS 'noon minus 12h' reference for a service date."""
    noon = dt.datetime(date.year, date.month, date.day, 12, tzinfo=TZ)
    return int(noon.timestamp()) - 12 * 3600


class Network:
    """All agencies' trips active on `date`, with times in seconds since the date's midnight.

    Trips from the previous service day that run past midnight are included with
    their times shifted by -24h, so late-night queries work.
    """

    def __init__(self, date: dt.date, feeds=None, walk_speed=1.3, max_transfer_m=400.0,
                 detour=1.3, transfer_slack=60, window=None):
        self.date = date
        self.walk_speed = walk_speed          # m/s (~4.7 km/h)
        self.detour = detour                  # straight-line -> street distance factor
        self.max_transfer_m = max_transfer_m
        self.transfer_slack = transfer_slack  # seconds needed to change vehicles
        self.feeds = [k for k in (feeds or FEEDS) if os.path.exists(os.path.join(CACHE_DIR, k, "meta.json"))]
        self.midnight = service_midnight(date)
        self._load(window)
        self._build_footpaths()
        self.rt_applied = {}

    # ------------------------------------------------------------------ loading
    def _load(self, window):
        stops, trips, routes, sts = [], [], [], []
        stop_off = trip_off = 0
        d_today = self.date.strftime("%Y%m%d")
        d_prev = (self.date - dt.timedelta(days=1)).strftime("%Y%m%d")
        for key in self.feeds:
            p = os.path.join(CACHE_DIR, key)
            s = pd.read_parquet(os.path.join(p, "stops.parquet"))
            r = pd.read_parquet(os.path.join(p, "routes.parquet"))
            t = pd.read_parquet(os.path.join(p, "trips.parquet"))
            svc = pd.read_parquet(os.path.join(p, "services.parquet"))
            st = pd.read_parquet(os.path.join(p, "stop_times.parquet"))
            today = set(svc.loc[svc["date"] == d_today, "service_id"])
            prev = set(svc.loc[svc["date"] == d_prev, "service_id"])
            t["feed"] = key
            t["local_t"] = np.arange(len(t))
            act_today = t["service_id"].isin(today).to_numpy()
            act_prev = t["service_id"].isin(prev).to_numpy()

            parts = []
            for active, shift, day in ((act_today, 0, self.date), (act_prev, -86400, self.date - dt.timedelta(days=1))):
                ids = np.flatnonzero(active)
                if not len(ids):
                    continue
                x = st[np.isin(st["t"].to_numpy(), ids)].copy()
                if shift:
                    # keep only previous-day trips still running after midnight
                    late = x.groupby("t")["arr"].transform("max") >= 86400
                    x = x[late.to_numpy()]
                    x["arr"] += shift
                    x["dep"] += shift
                if window is not None:
                    lo, hi = window
                    span = x.groupby("t").agg(a=("dep", "min"), b=("arr", "max"))
                    keep = span.index[(span["b"] >= lo) & (span["a"] <= hi)]
                    x = x[x["t"].isin(keep)]
                x["day_shift"] = shift
                parts.append(x)
            if not parts:
                continue
            x = pd.concat(parts, ignore_index=True)
            # new global trip ids: one per (local trip, day_shift)
            x["gkey"] = x["t"].astype(np.int64) * 2 + (x["day_shift"] != 0)
            uk, inv = np.unique(x["gkey"].to_numpy(), return_inverse=True)
            tt = t.iloc[uk // 2].reset_index(drop=True)
            tt["day_shift"] = np.where(uk % 2 == 1, -86400, 0)
            tt["service_date"] = np.where(uk % 2 == 1, d_prev, d_today)
            x["gt"] = inv.astype(np.int64) + trip_off
            x["gs"] = x["s"].astype(np.int64) + stop_off
            s["feed"] = key
            s["local_s"] = np.arange(len(s))
            r["feed"] = key
            stops.append(s)
            routes.append(r)
            trips.append(tt)
            sts.append(x[["gt", "gs", "seq", "arr", "dep"]])
            stop_off += len(s)
            trip_off += len(tt)

        self.stops = pd.concat(stops, ignore_index=True)
        self.routes = pd.concat(routes, ignore_index=True)
        self.trips = pd.concat(trips, ignore_index=True)
        rkey = self.routes["feed"] + "|" + self.routes["route_id"]
        ridx = pd.Series(np.arange(len(self.routes)), index=rkey)
        ridx = ridx[~ridx.index.duplicated()]
        self.trips["route_idx"] = (self.trips["feed"] + "|" + self.trips["route_id"]).map(ridx).fillna(-1).astype(int)
        self.trips["trip_key"] = self.trips["feed"] + "|" + self.trips["trip_id"] + "|" + self.trips["service_date"]
        self.stop_times = pd.concat(sts, ignore_index=True).sort_values(["gt", "seq"], kind="stable").reset_index(drop=True)
        self.stop_times["sched_arr"] = self.stop_times["arr"]
        self.stop_times["sched_dep"] = self.stop_times["dep"]
        self.xy = project(self.stops["stop_lat"].to_numpy(), self.stops["stop_lon"].to_numpy())
        self.trip_cancelled = np.zeros(len(self.trips), dtype=bool)
        self.build_connections()

    def build_connections(self):
        """(Re)build the sorted connection arrays from stop_times (call after realtime edits)."""
        st = self.stop_times
        gt = st["gt"].to_numpy()
        same = gt[1:] == gt[:-1]
        i = np.flatnonzero(same)
        j = i + 1
        keep = ~self.trip_cancelled[gt[i]]
        i, j = i[keep], j[keep]
        dep = st["dep"].to_numpy()[i]
        arr = st["arr"].to_numpy()[j]
        arr = np.maximum(arr, dep)  # guard against bad data / RT
        order = np.lexsort((arr, dep))
        self.c_dep_stop = st["gs"].to_numpy()[i][order].astype(np.int32)
        self.c_arr_stop = st["gs"].to_numpy()[j][order].astype(np.int32)
        self.c_dep = dep[order].astype(np.int32)
        self.c_arr = arr[order].astype(np.int32)
        self.c_trip = gt[i][order].astype(np.int32)
        self.c_st_dep = i[order].astype(np.int64)   # row in stop_times (for journey output)
        self.c_st_arr = j[order].astype(np.int64)

    def _build_footpaths(self):
        tree = cKDTree(self.xy)
        pairs = tree.query_pairs(self.max_transfer_m, output_type="ndarray")
        a = np.concatenate([pairs[:, 0], pairs[:, 1]])
        b = np.concatenate([pairs[:, 1], pairs[:, 0]])
        d = np.linalg.norm(self.xy[a] - self.xy[b], axis=1)
        dur = np.ceil(d * self.detour / self.walk_speed).astype(np.int32)
        order = np.lexsort((dur, a))
        a, b, dur = a[order], b[order], dur[order]
        self.fp_ptr = np.searchsorted(a, np.arange(len(self.stops) + 1)).astype(np.int64)
        self.fp_to = b.astype(np.int32)
        self.fp_dur = dur
        self.tree = tree

    # ------------------------------------------------------------------ helpers
    def stops_near(self, lat, lon, radius_m):
        xy = project([lat], [lon])[0]
        idx = self.tree.query_ball_point(xy, radius_m)
        idx = np.array(sorted(idx), dtype=np.int64)
        d = np.linalg.norm(self.xy[idx] - xy, axis=1) if len(idx) else np.array([])
        return idx, d

    def find_stop(self, query: str, limit=10) -> pd.DataFrame:
        q = query.lower()
        m = self.stops[self.stops["stop_name"].str.lower().str.contains(q, regex=False)
                       | (self.stops["stop_id"] == query)]
        return m.head(limit)

    def to_clock(self, secs: int) -> str:
        t = dt.datetime.fromtimestamp(self.midnight + int(secs), TZ)
        return t.strftime("%H:%M")

    def secs_of(self, when: dt.datetime) -> int:
        if when.tzinfo is None:
            when = when.replace(tzinfo=TZ)
        return int(when.timestamp()) - self.midnight

    def summary(self) -> str:
        lines = [f"GTA network for {self.date}: {len(self.stops):,} stops, {len(self.trips):,} trips, "
                 f"{len(self.c_dep):,} connections, {len(self.fp_to):,} walking links"]
        per = self.trips.groupby("feed").size()
        for k in self.feeds:
            rt = self.rt_applied.get(k)
            extra = f"  realtime: {rt}" if rt else ""
            lines.append(f"  {k:<11} {per.get(k, 0):>6} trips{extra}")
        return "\n".join(lines)


_CACHE: dict = {}


def load_network(date: dt.date | None = None, **kw) -> Network:
    date = date or dt.datetime.now(TZ).date()
    kw = {k: tuple(v) if isinstance(v, list) else v for k, v in kw.items()}
    key = (date, tuple(sorted(kw.items())))
    if key not in _CACHE:
        _CACHE[key] = Network(date, **kw)
    return _CACHE[key]
