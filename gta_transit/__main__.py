"""Command-line interface.

  python -m gta_transit update                         # download + preprocess all GTA feeds
  python -m gta_transit info
  python -m gta_transit isochrone "Union Station GO" --bands 15,30,45,60 --out iso.html
  python -m gta_transit isochrone 43.6532,-79.3832 --at 08:30 --no-realtime
  python -m gta_transit route "Finch Station" "Square One" --map trip.html
  python -m gta_transit departures "Kipling Station" --limit 15
  python -m gta_transit vehicles --out live.html
  python -m gta_transit serve --port 8000                # interactive click-to-isochrone map
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

import numpy as np

from .network import TZ, load_network
from .feeds import FEEDS


def _now():
    return dt.datetime.now(TZ)


def _when(args) -> dt.datetime:
    base = dt.datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else _now().date()
    if args.at:
        h, m = map(int, args.at.split(":"))
        return dt.datetime(base.year, base.month, base.day, tzinfo=TZ) + dt.timedelta(hours=h, minutes=m)
    if args.date:
        return dt.datetime(base.year, base.month, base.day, 8, 0, tzinfo=TZ)
    return _now()


def _network(args, when):
    t0 = time.time()
    net = load_network(when.date(), feeds=args.feeds.split(",") if args.feeds else None)
    use_rt = args.realtime if args.realtime is not None else abs((when - _now()).total_seconds()) < 3 * 3600
    if use_rt:
        from .realtime import refresh
        refresh(net, log=lambda s: print(s, file=sys.stderr))
    print(f"[network ready in {time.time() - t0:.1f}s; realtime {'ON' if use_rt else 'off'}]", file=sys.stderr)
    return net, use_rt


def _place(net, text: str):
    """'lat,lon' or a stop name -> (lat, lon, label)."""
    try:
        lat, lon = map(float, text.split(","))
        return lat, lon, f"{lat:.5f},{lon:.5f}"
    except ValueError:
        pass
    m = net.find_stop(text, limit=50)
    if m.empty:
        sys.exit(f"No stop matching '{text}'. Use 'lat,lon' or part of a stop name.")
    # prefer exact-ish name, then shortest name
    m = m.assign(_l=m["stop_name"].str.len()).sort_values("_l")
    r = m.iloc[0]
    return float(r.stop_lat), float(r.stop_lon), f"{r.stop_name} ({r.feed.upper()})"


def cmd_update(args):
    from .static import update_all
    update_all(args.feeds.split(",") if args.feeds else None, force_download=args.force,
               force_preprocess=args.force)


def cmd_info(args):
    when = _when(args)
    net, _ = _network(args, when)
    print(net.summary())
    for k in net.feeds:
        f = FEEDS[k]
        print(f"  {k:<11} {f.name}" + (f"  — {f.notes}" if f.notes else ""))


def cmd_isochrone(args):
    from .isochrone import isochrones
    when = _when(args)
    net, rt = _network(args, when)
    lat, lon, label = _place(net, args.origin)
    bands = [int(x) for x in args.bands.split(",")]
    t0 = time.time()
    iso = isochrones(net, lat, lon, net.secs_of(when), bands, max_walk_m=args.max_walk)
    print(f"Isochrones from {label}, departing {when:%Y-%m-%d %H:%M} ({time.time() - t0:.2f}s):")
    for b in iso.bands:
        ag = ", ".join(f"{k}:{v}" for k, v in sorted(b.feeds.items(), key=lambda x: -x[1]))
        print(f"  {b.minutes:>4} min  {b.area_km2:>8,.1f} km²  {b.n_stops:>6,} stops   [{ag}]")
    if args.top:
        tbl = iso.search.stops_table()
        far = tbl.assign(d=np.hypot((tbl.stop_lat - lat) * 111.1, (tbl.stop_lon - lon) * 80.4))
        far = far.sort_values("d", ascending=False).drop_duplicates("stop_name").head(args.top)
        print(f"\nFarthest stops reachable within {bands[-1]} min:")
        for r in far.itertuples():
            print(f"  {r.d:5.1f} km  {r.minutes:4.0f} min  {r.stop_name} ({r.feed.upper()})")
    if args.geojson:
        json.dump(iso.geojson(), open(args.geojson, "w"))
        print(f"GeoJSON -> {args.geojson}")
    if args.out:
        from .viz import isochrone_map
        isochrone_map(iso, f"Reachable from {label}", realtime_note=" · live delays applied" if rt else
                      " · scheduled times").save(args.out)
        print(f"Map -> {args.out}")


def cmd_route(args):
    from .router import plan
    when = _when(args)
    net, rt = _network(args, when)
    o = _place(net, args.origin)
    d = _place(net, args.dest)
    res, stop, arr, eg = plan(net, o[0], o[1], d[0], d[1], net.secs_of(when), max_minutes=args.max_minutes)
    if stop is None:
        sys.exit("No transit journey found within the time limit.")
    t0 = net.secs_of(when)
    print(f"{o[2]} -> {d[2]}\nDepart {net.to_clock(t0)}, arrive {net.to_clock(arr)} ({(arr - t0) / 60:.0f} min)"
          f"{' using live delays' if rt else ''}")
    print(res.describe_journey(stop, eg))
    if args.map:
        from .viz import journey_map
        journey_map(res, stop, (d[0], d[1]), eg, f"{o[2]} → {d[2]}").save(args.map)
        print(f"Map -> {args.map}")


def cmd_departures(args):
    when = _when(args)
    net, rt = _network(args, when)
    lat, lon, label = _place(net, args.stop)
    idx, _ = net.stops_near(lat, lon, args.radius)
    t0 = net.secs_of(when)
    sel = np.isin(net.c_dep_stop, idx) & (net.c_dep >= t0 - 60)
    c = np.flatnonzero(sel)
    c = c[np.argsort(net.c_dep[c])][: args.limit]
    st = net.stop_times
    print(f"Departures near {label} from {net.to_clock(t0)}:")
    for ci in c:
        tr = net.trips.iloc[net.c_trip[ci]]
        r = net.routes.iloc[tr.route_idx] if tr.route_idx >= 0 else None
        row = net.c_st_dep[ci]
        delay = int(st["dep"].iat[row] - st["sched_dep"].iat[row])
        live = f"  {'+' if delay >= 0 else ''}{delay / 60:.0f} min" if delay else ("  on time" if rt else "")
        rname = (r.route_short_name or r.route_long_name) if r is not None else tr.route_id
        print(f"  {net.to_clock(net.c_dep[ci])}  {tr.feed.upper():<10} {rname:<6} {str(tr.trip_headsign)[:45]:<45} "
              f"@ {net.stops.stop_name.iat[net.c_dep_stop[ci]][:35]}{live}")


def cmd_vehicles(args):
    from .realtime import vehicles
    from .viz import vehicles_map
    df = vehicles(args.feeds.split(",") if args.feeds else None, log=print)
    print(df.groupby("feed").size().to_string())
    if args.out:
        vehicles_map(df, f"Live GTA transit vehicles · {_now():%H:%M}").save(args.out)
        print(f"Map -> {args.out}")


def cmd_serve(args):
    from .server import serve
    serve(args.port, args.host, args.refresh, feeds=args.feeds.split(",") if args.feeds else None)


def main(argv=None):
    p = argparse.ArgumentParser(prog="gta_transit", description="GTA multi-agency GTFS network model")
    p.add_argument("--feeds", help="comma-separated subset, e.g. ttc,go,miway (default: all)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def timeopts(sp):
        sp.add_argument("--date", help="service date YYYY-MM-DD (default today)")
        sp.add_argument("--at", help="departure time HH:MM (default now)")
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--realtime", dest="realtime", action="store_true", default=None,
                       help="apply live GTFS-RT delays (default: on when departing within 3h of now)")
        g.add_argument("--no-realtime", dest="realtime", action="store_false")

    s = sub.add_parser("update", help="download + preprocess static GTFS")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("info", help="network summary")
    timeopts(s)
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser("isochrone", help="area reachable from a point")
    s.add_argument("origin", help="'lat,lon' or stop name")
    s.add_argument("--bands", default="15,30,45,60")
    s.add_argument("--max-walk", type=float, default=1000.0, help="max walk from last stop, metres")
    s.add_argument("--out", help="HTML map output")
    s.add_argument("--geojson", help="GeoJSON output")
    s.add_argument("--top", type=int, default=10, help="list N farthest reachable stops")
    timeopts(s)
    s.set_defaults(fn=cmd_isochrone)

    s = sub.add_parser("route", help="point-to-point journey")
    s.add_argument("origin")
    s.add_argument("dest")
    s.add_argument("--max-minutes", type=int, default=240)
    s.add_argument("--map", help="HTML map output")
    timeopts(s)
    s.set_defaults(fn=cmd_route)

    s = sub.add_parser("departures", help="next departures near a stop")
    s.add_argument("stop")
    s.add_argument("--radius", type=float, default=150.0)
    s.add_argument("--limit", type=int, default=20)
    timeopts(s)
    s.set_defaults(fn=cmd_departures)

    s = sub.add_parser("vehicles", help="live vehicle positions")
    s.add_argument("--out", help="HTML map output")
    s.set_defaults(fn=cmd_vehicles)

    s = sub.add_parser("serve", help="interactive web map with live data")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--refresh", type=int, default=60, help="realtime refresh seconds")
    s.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
