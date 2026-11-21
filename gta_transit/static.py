"""Download static GTFS feeds and preprocess them into a compact parquet cache."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import time
import zipfile

import numpy as np
import pandas as pd
import requests

from .feeds import CACHE_DIR, FEEDS, RAW_DIR, Feed

CACHE_VERSION = 3


def _hms_to_sec(s: pd.Series) -> np.ndarray:
    """Convert GTFS HH:MM:SS (may exceed 24h, may be blank) to seconds; NaN for blank."""
    s = s.fillna("").astype(str).str.strip()
    parts = s.str.split(":", expand=True)
    if parts.shape[1] < 3:
        return np.full(len(s), np.nan)
    h = pd.to_numeric(parts[0], errors="coerce")
    m = pd.to_numeric(parts[1], errors="coerce")
    sec = pd.to_numeric(parts[2], errors="coerce")
    return (h * 3600 + m * 60 + sec).to_numpy(dtype=float)


def download(feed: Feed, force: bool = False, max_age_h: float = 24.0) -> str:
    os.makedirs(RAW_DIR, exist_ok=True)
    path = os.path.join(RAW_DIR, f"{feed.key}.zip")
    if not force and os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_h * 3600:
        return path
    r = requests.get(feed.static_url, timeout=300)
    r.raise_for_status()
    zipfile.ZipFile(io.BytesIO(r.content)).testzip()
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, path)
    return path


def _read(z: zipfile.ZipFile, name: str, usecols=None) -> pd.DataFrame | None:
    matches = [n for n in z.namelist() if n.split("/")[-1] == name]
    if not matches:
        return None
    with z.open(matches[0]) as f:
        df = pd.read_csv(f, dtype=str, encoding="utf-8-sig", keep_default_na=False,
                         usecols=lambda c: usecols is None or c.strip() in usecols)
    df.columns = [c.strip() for c in df.columns]
    return df


def preprocess(feed: Feed, force: bool = False) -> str:
    """Parse a feed's zip into parquet tables under data/cache/<key>/."""
    raw = os.path.join(RAW_DIR, f"{feed.key}.zip")
    out = os.path.join(CACHE_DIR, feed.key)
    meta_path = os.path.join(out, "meta.json")
    if not force and os.path.exists(meta_path):
        meta = json.load(open(meta_path))
        if meta.get("version") == CACHE_VERSION and meta.get("raw_mtime") == os.path.getmtime(raw):
            return out
    os.makedirs(out, exist_ok=True)
    z = zipfile.ZipFile(raw)

    stops = _read(z, "stops.txt")
    stops["stop_lat"] = pd.to_numeric(stops["stop_lat"], errors="coerce")
    stops["stop_lon"] = pd.to_numeric(stops["stop_lon"], errors="coerce")
    if "location_type" not in stops:
        stops["location_type"] = ""
    if "parent_station" not in stops:
        stops["parent_station"] = ""
    stops = stops[["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"]]
    stops = stops.dropna(subset=["stop_lat", "stop_lon"]).reset_index(drop=True)

    routes = _read(z, "routes.txt")
    for c in ("route_short_name", "route_long_name", "route_color"):
        if c not in routes:
            routes[c] = ""
    routes["route_type"] = pd.to_numeric(routes["route_type"], errors="coerce").fillna(3).astype(int)
    routes = routes[["route_id", "route_short_name", "route_long_name", "route_type", "route_color"]]

    trips = _read(z, "trips.txt")
    for c in ("trip_headsign", "direction_id", "shape_id"):
        if c not in trips:
            trips[c] = ""
    trips = trips[["trip_id", "route_id", "service_id", "trip_headsign", "direction_id", "shape_id"]
                  ].reset_index(drop=True)

    shapes = _read(z, "shapes.txt", usecols={"shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"})
    if shapes is not None and len(shapes):
        shapes = pd.DataFrame({
            "shape_id": shapes["shape_id"],
            "lat": pd.to_numeric(shapes["shape_pt_lat"], errors="coerce"),
            "lon": pd.to_numeric(shapes["shape_pt_lon"], errors="coerce"),
            "seq": pd.to_numeric(shapes["shape_pt_sequence"], errors="coerce"),
        }).dropna().sort_values(["shape_id", "seq"], kind="stable").reset_index(drop=True)
    else:
        shapes = pd.DataFrame({"shape_id": [], "lat": [], "lon": [], "seq": []})

    st = _read(z, "stop_times.txt",
               usecols={"trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"})
    trip_idx = pd.Series(np.arange(len(trips), dtype=np.int32), index=trips["trip_id"])
    stop_idx = pd.Series(np.arange(len(stops), dtype=np.int32), index=stops["stop_id"])
    st["t"] = st["trip_id"].map(trip_idx)
    st["s"] = st["stop_id"].map(stop_idx)
    st = st.dropna(subset=["t", "s"])
    st["seq"] = pd.to_numeric(st["stop_sequence"], errors="coerce")
    arr = _hms_to_sec(st["arrival_time"])
    dep = _hms_to_sec(st["departure_time"])
    arr = np.where(np.isnan(arr), dep, arr)
    dep = np.where(np.isnan(dep), arr, dep)
    df = pd.DataFrame({"t": st["t"].astype(np.int32).to_numpy(), "s": st["s"].astype(np.int32).to_numpy(),
                       "seq": st["seq"].astype(np.int32).to_numpy(), "arr": arr, "dep": dep})
    df = df.sort_values(["t", "seq"], kind="stable").reset_index(drop=True)

    # Interpolate missing (non-timepoint) times linearly by position within each trip.
    missing = np.isnan(df["arr"].to_numpy())
    if missing.any():
        pos = np.arange(len(df), dtype=float)
        valid = ~missing
        idx_prev = np.where(valid, np.arange(len(df)), -1)
        idx_prev = np.maximum.accumulate(idx_prev)
        idx_next = np.where(valid, np.arange(len(df)), len(df))
        idx_next = np.minimum.accumulate(idx_next[::-1])[::-1]
        t_arr = df["t"].to_numpy()
        ok = missing & (idx_prev >= 0) & (idx_next < len(df))
        ok &= (t_arr[np.clip(idx_prev, 0, None)] == t_arr) & (t_arr[np.clip(idx_next, None, len(df) - 1)] == t_arr)
        a = df["dep"].to_numpy().copy()
        b = df["arr"].to_numpy().copy()
        ip, inn = idx_prev[ok], idx_next[ok]
        frac = (pos[ok] - ip) / (inn - ip)
        val = a[ip] + frac * (b[inn] - a[ip])
        b[ok] = val
        a[ok] = val
        df["arr"], df["dep"] = b, a
        df = df[~np.isnan(df["arr"].to_numpy())]
    df["arr"] = df["arr"].astype(np.int32)
    df["dep"] = df["dep"].astype(np.int32)

    # Expand frequency-based trips (frequencies.txt) into explicit trips.
    freq = _read(z, "frequencies.txt")
    if freq is not None and len(freq):
        new_trips, new_st = [], []
        next_t = len(trips)
        for _, fr in freq.iterrows():
            if fr["trip_id"] not in trip_idx.index:
                continue
            t0 = trip_idx[fr["trip_id"]]
            tpl = df[df["t"] == t0]
            if tpl.empty:
                continue
            base = int(tpl["dep"].iloc[0])
            start, end = _hms_to_sec(pd.Series([fr["start_time"], fr["end_time"]]))
            hw = int(fr["headway_secs"])
            for k, s0 in enumerate(range(int(start), int(end), hw)):
                row = trips.iloc[t0].copy()
                row["trip_id"] = f"{row['trip_id']}#f{k}"
                new_trips.append(row)
                seg = tpl.copy()
                seg["t"] = next_t
                seg["arr"] += s0 - base
                seg["dep"] += s0 - base
                new_st.append(seg)
                next_t += 1
        if new_trips:
            trips = pd.concat([trips, pd.DataFrame(new_trips)], ignore_index=True)
            df = pd.concat([df[~df["t"].isin(trip_idx[freq["trip_id"]].dropna().values)]] + new_st,
                           ignore_index=True).sort_values(["t", "seq"], kind="stable")

    # Calendar -> long table of (service_id, date) active days.
    cal = _read(z, "calendar.txt")
    cd = _read(z, "calendar_dates.txt")
    days = []
    if cal is not None and len(cal):
        wd = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for _, r in cal.iterrows():
            s = dt.datetime.strptime(r["start_date"], "%Y%m%d").date()
            e = dt.datetime.strptime(r["end_date"], "%Y%m%d").date()
            mask = [r[w] == "1" for w in wd]
            d = s
            while d <= e:
                if mask[d.weekday()]:
                    days.append((r["service_id"], d.strftime("%Y%m%d")))
                d += dt.timedelta(days=1)
    svc = pd.DataFrame(days, columns=["service_id", "date"]).drop_duplicates()
    if cd is not None and len(cd):
        cd = cd[["service_id", "date", "exception_type"]]
        add = cd[cd["exception_type"] == "1"][["service_id", "date"]]
        rem = cd[cd["exception_type"] == "2"][["service_id", "date"]]
        svc = pd.concat([svc, add]).drop_duplicates()
        if len(rem):
            key = svc["service_id"] + "|" + svc["date"]
            svc = svc[~key.isin(rem["service_id"] + "|" + rem["date"])]

    stops.to_parquet(os.path.join(out, "stops.parquet"))
    routes.to_parquet(os.path.join(out, "routes.parquet"))
    trips.to_parquet(os.path.join(out, "trips.parquet"))
    shapes.to_parquet(os.path.join(out, "shapes.parquet"))
    df.reset_index(drop=True).to_parquet(os.path.join(out, "stop_times.parquet"))
    svc.reset_index(drop=True).to_parquet(os.path.join(out, "services.parquet"))
    agency = _read(z, "agency.txt")
    tz = agency["agency_timezone"].iloc[0] if agency is not None and "agency_timezone" in agency else "America/Toronto"
    json.dump({"version": CACHE_VERSION, "raw_mtime": os.path.getmtime(raw), "timezone": tz,
               "n_stops": len(stops), "n_trips": len(trips), "n_stop_times": len(df),
               "dates": [svc["date"].min(), svc["date"].max()] if len(svc) else None},
              open(meta_path, "w"))
    return out


def update_all(keys=None, force_download=False, force_preprocess=False, log=print):
    for key in keys or FEEDS:
        feed = FEEDS[key]
        t0 = time.time()
        try:
            download(feed, force=force_download)
            path = preprocess(feed, force=force_preprocess)
            meta = json.load(open(os.path.join(path, "meta.json")))
            log(f"  {key:<11} {meta['n_stops']:>6} stops {meta['n_trips']:>7} trips "
                f"{meta['n_stop_times']:>9} stop_times  service {meta['dates']}  ({time.time() - t0:.1f}s)")
        except Exception as e:  # keep going if one agency is down
            log(f"  {key:<11} FAILED: {e}")
