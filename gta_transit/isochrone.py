"""Isochrones: the area reachable from a point within N minutes by transit + walking."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.geometry import mapping

from .network import Network, project, unproject
from .router import SearchResult, search


@dataclass
class Isochrone:
    minutes: int
    geometry: shapely.Geometry      # lon/lat polygon(s)
    area_km2: float
    n_stops: int
    feeds: dict = field(default_factory=dict)  # stops reached per agency


@dataclass
class IsochroneResult:
    lat: float
    lon: float
    depart: str
    bands: list[Isochrone]
    search: SearchResult

    def geojson(self) -> dict:
        feats = []
        for b in self.bands:
            feats.append({"type": "Feature", "geometry": mapping(b.geometry),
                          "properties": {"minutes": b.minutes, "area_km2": round(b.area_km2, 1),
                                         "stops": b.n_stops, "agencies": b.feeds,
                                         "depart": self.depart, "origin": [self.lat, self.lon]}})
        return {"type": "FeatureCollection", "features": feats}


def _to_lonlat(geom):
    def f(coords):
        lat, lon = unproject(coords)
        return np.column_stack([lon, lat])
    return shapely.transform(geom, f)


def isochrones(net: Network, lat: float, lon: float, t_start: int, bands=(15, 30, 45, 60),
               max_walk_m: float = 1000.0, max_access_m: float = 800.0, max_transfers: int = 8,
               quad_segs: int = 6) -> IsochroneResult:
    """Compute nested reachability polygons.

    Each reached stop contributes a walking circle whose radius is the time left
    in the budget (at the network walking speed, discounted by the detour factor),
    capped at `max_walk_m`. The origin contributes its own walking circle.
    """
    bands = sorted(int(b) for b in bands)
    res = search(net, lat, lon, t_start, max(bands), max_access_m=max_access_m, max_transfers=max_transfers)
    speed = net.walk_speed / net.detour
    origin_xy = project([lat], [lon])[0]
    reached = res.reached
    out = []
    for b in bands:
        t_end = t_start + b * 60
        idx = reached[res.tau[reached] <= t_end]
        rad = np.minimum((t_end - res.tau[idx]) * speed, max_walk_m)
        keep = rad > 25
        idx, rad = idx[keep], rad[keep]
        pts = np.vstack([net.xy[idx], origin_xy[None, :]])
        radii = np.append(rad, min(b * 60 * speed, max_walk_m))
        circles = shapely.buffer(shapely.points(pts), radii, quad_segs=quad_segs)
        poly = shapely.union_all(circles)
        poly = shapely.simplify(poly, 15)
        feeds = net.stops["feed"].to_numpy()[idx]
        u, c = np.unique(feeds, return_counts=True)
        out.append(Isochrone(b, _to_lonlat(poly), poly.area / 1e6, len(idx),
                             {k: int(v) for k, v in zip(u, c)}))
    return IsochroneResult(lat, lon, net.to_clock(t_start), out, res)
