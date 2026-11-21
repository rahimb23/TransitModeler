"""Folium (Leaflet) maps for isochrones, journeys and live vehicles."""
from __future__ import annotations

import html

import folium
import numpy as np
import pandas as pd
from shapely.geometry import mapping

from .feeds import FEEDS
from .isochrone import IsochroneResult

# Single-hue blue ramp (sequential). Darker = reachable sooner.
BLUE = ["#104281", "#1c5cab", "#2a78d6", "#5598e7", "#86b6ef", "#b7d3f6"]
INK = "#1f1f1d"
MUTED = "#6b6a65"


def band_colors(n: int) -> list[str]:
    if n <= 1:
        return [BLUE[2]]
    idx = np.round(np.linspace(0, len(BLUE) - 1, n)).astype(int)
    return [BLUE[i] for i in idx]


def _panel(html_body: str) -> str:
    return (f'<div style="position:fixed;top:12px;right:12px;z-index:9999;background:#fff;'
            f'padding:12px 14px;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.25);'
            f'font:13px/1.45 system-ui,sans-serif;color:{INK};max-width:300px">{html_body}</div>')


def isochrone_map(iso: IsochroneResult, title: str = "Transit reachability", show_stops=True,
                  realtime_note: str = "") -> folium.Map:
    m = folium.Map(location=[iso.lat, iso.lon], zoom_start=11, tiles="OpenStreetMap", control_scale=True)
    colors = band_colors(len(iso.bands))
    # largest band first so smaller bands draw on top
    for b, col in sorted(zip(iso.bands, colors), key=lambda x: -x[0].minutes):
        folium.GeoJson(
            mapping(b.geometry), name=f"{b.minutes} min",
            style_function=lambda _f, c=col: {"fillColor": c, "color": "#ffffff", "weight": 2, "fillOpacity": 0.55},
            tooltip=f"Within {b.minutes} min: {b.area_km2:,.0f} km², {b.n_stops:,} stops",
        ).add_to(m)
    if show_stops:
        res = iso.search
        tbl = res.stops_table()
        tbl = tbl[tbl["minutes"] <= iso.bands[-1].minutes]
        fg = folium.FeatureGroup(name="Reached stops", show=False)
        for r in tbl.itertuples():
            folium.CircleMarker(
                [r.stop_lat, r.stop_lon], radius=4, weight=1, color="#ffffff", fill=True,
                fill_color=FEEDS[r.feed].color, fill_opacity=0.9,
                tooltip=f"{html.escape(r.stop_name)} ({r.feed.upper()})<br>arrive {r.arrival} · {r.minutes:.0f} min "
                        f"· {r.vehicles} vehicle{'s' if r.vehicles != 1 else ''}",
            ).add_to(fg)
        fg.add_to(m)
    folium.Marker([iso.lat, iso.lon], tooltip="Origin", icon=folium.Icon(color="black", icon="star")).add_to(m)

    rows = "".join(
        f'<tr><td><span style="display:inline-block;width:12px;height:12px;border-radius:3px;'
        f'background:{c};margin-right:6px;vertical-align:-1px"></span>{b.minutes} min</td>'
        f'<td style="text-align:right;padding-left:10px">{b.area_km2:,.0f} km²</td>'
        f'<td style="text-align:right;padding-left:10px">{b.n_stops:,} stops</td></tr>'
        for b, c in zip(iso.bands, colors))
    agencies = iso.bands[-1].feeds
    ag = ", ".join(f"{k.upper()} {v:,}" for k, v in sorted(agencies.items(), key=lambda x: -x[1]))
    body = (f'<div style="font-weight:600;font-size:14px;margin-bottom:4px">{html.escape(title)}</div>'
            f'<div style="color:{MUTED};margin-bottom:8px">Departing {iso.depart}{realtime_note}</div>'
            f'<table style="border-collapse:collapse">{rows}</table>'
            f'<div style="color:{MUTED};margin-top:8px">Stops reached by agency: {ag}</div>')
    m.get_root().html.add_child(folium.Element(_panel(body)))
    folium.LayerControl(collapsed=True).add_to(m)
    return m


def journey_map(res, stop: int, dest: tuple, egress_s: int, title="Journey") -> folium.Map:
    net = res.net
    legs = res.journey(stop)
    o = res.origin
    m = folium.Map(location=[(o[0] + dest[0]) / 2, (o[1] + dest[1]) / 2], zoom_start=11, tiles="OpenStreetMap")
    st = net.stop_times
    prev = o
    for leg in legs:
        if leg["mode"] == "walk":
            to = net.stops.iloc[leg["to"]]
            fr = prev if leg["from"] is None else net.stops.iloc[leg["from"]][["stop_lat", "stop_lon"]].tolist()
            folium.PolyLine([fr, [to.stop_lat, to.stop_lon]], color=MUTED, weight=3, dash_array="6 6").add_to(m)
            prev = [to.stop_lat, to.stop_lon]
        else:
            # draw every stop passed on the vehicle leg
            trip_rows = st.index[st["gt"] == net.c_trip[res.p_enter[leg["to"]]]]
            s_ids = st.loc[trip_rows, "gs"].to_numpy()
            i0 = int(np.flatnonzero(s_ids == leg["from"])[0])
            i1 = i0 + int(np.flatnonzero(s_ids[i0:] == leg["to"])[0])
            pts = net.stops.iloc[s_ids[i0:i1 + 1]][["stop_lat", "stop_lon"]].to_numpy().tolist()
            col = FEEDS[leg["feed"]].color
            folium.PolyLine(pts, color=col, weight=5, opacity=0.9,
                            tooltip=f"{leg['feed'].upper()} {leg['route']} {html.escape(str(leg['headsign']))} "
                                    f"{net.to_clock(leg['dep'])}–{net.to_clock(leg['arr'])}").add_to(m)
            prev = pts[-1]
    folium.PolyLine([prev, list(dest)], color=MUTED, weight=3, dash_array="6 6").add_to(m)
    folium.Marker(list(o), tooltip="Origin", icon=folium.Icon(color="black", icon="play")).add_to(m)
    folium.Marker(list(dest), tooltip="Destination", icon=folium.Icon(color="black", icon="flag")).add_to(m)
    text = html.escape(res.describe_journey(stop, egress_s)).replace("\n", "<br>")
    m.get_root().html.add_child(folium.Element(_panel(
        f'<div style="font-weight:600;font-size:14px;margin-bottom:6px">{html.escape(title)}</div>'
        f'<div style="font-family:ui-monospace,monospace;font-size:11px;white-space:normal">{text}</div>')))
    return m


def vehicles_map(df: pd.DataFrame, title="Live GTA transit vehicles") -> folium.Map:
    m = folium.Map(location=[43.72, -79.45], zoom_start=10, tiles="OpenStreetMap", control_scale=True)
    counts = df.groupby("feed").size().sort_values(ascending=False)
    for feed, g in df.groupby("feed"):
        fg = folium.FeatureGroup(name=f"{feed.upper()} ({len(g)})")
        col = FEEDS[feed].color
        for r in g.itertuples():
            folium.CircleMarker([r.lat, r.lon], radius=4, weight=1, color="#ffffff", fill=True,
                                fill_color=col, fill_opacity=0.9,
                                tooltip=f"{feed.upper()} route {r.route_id} · vehicle {r.vehicle_id}").add_to(fg)
        fg.add_to(m)
    rows = "".join(f'<tr><td><span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
                   f'background:{FEEDS[k].color};margin-right:6px"></span>{k.upper()}</td>'
                   f'<td style="text-align:right;padding-left:12px">{v:,}</td></tr>' for k, v in counts.items())
    m.get_root().html.add_child(folium.Element(_panel(
        f'<div style="font-weight:600;font-size:14px;margin-bottom:6px">{html.escape(title)}</div>'
        f'<table>{rows}</table><div style="color:{MUTED};margin-top:6px">{len(df):,} vehicles reporting</div>')))
    folium.LayerControl(collapsed=True).add_to(m)
    return m
