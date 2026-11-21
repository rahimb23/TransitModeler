"""Registry of GTA transit agencies: static GTFS and GTFS-Realtime endpoints."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Feed:
    key: str
    name: str
    static_url: str
    trip_updates_url: str | None = None
    vehicle_positions_url: str | None = None
    # How to match realtime trip ids to static trips:
    #   "trip_id"     - realtime trip_id == static trip_id (normal case)
    #   "vehicle_position" - ids differ; estimate delays by matching vehicle positions to
    #                        scheduled trips on the same route (see realtime._match_vehicles)
    rt_match: str = "trip_id"
    rt_key_env: str | None = None  # env var holding an API key, appended as ?key=
    color: str = "#555555"
    notes: str = ""


MDB = "https://files.mobilitydatabase.org/{}/latest.zip"

FEEDS: dict[str, Feed] = {f.key: f for f in [
    Feed("ttc", "Toronto Transit Commission",
         "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/7795b45e-e65a-4465-81fc-c36b9dfff169/"
         "resource/cfb6b2b8-6191-41e3-bda1-b175c51148cb/download/opendata_ttc_schedules.zip",
         "https://bustime.ttc.ca/gtfsrt/trips", "https://bustime.ttc.ca/gtfsrt/vehicles",
         rt_match="vehicle_position", color="#da251d",
         notes="BusTime realtime uses its own trip/stop ids; delays estimated from vehicle positions."),
    Feed("go", "GO Transit (Metrolinx)",
         "https://assets.metrolinx.com/raw/upload/Documents/Metrolinx/Open%20Data/GO-GTFS.zip",
         "https://api.openmetrolinx.com/OpenDataAPI/api/V1/Gtfs/Feed/TripUpdates",
         "https://api.openmetrolinx.com/OpenDataAPI/api/V1/Gtfs/Feed/VehiclePosition",
         rt_key_env="GO_API_KEY", color="#3d8a2e",
         notes="Realtime requires a free Metrolinx Open Data API key in $GO_API_KEY."),
    Feed("miway", "MiWay (Mississauga)", "https://www.miapp.ca/GTFS/google_transit.zip",
         "https://www.miapp.ca/GTFS_RT/TripUpdate/TripUpdates.pb",
         "https://www.miapp.ca/GTFS_RT/Vehicle/VehiclePositions.pb", color="#f58220"),
    Feed("brampton", "Brampton Transit", MDB.format("mdb-1994"), color="#0072bc",
         notes="Static from Mobility Database mirror; no reachable public realtime feed."),
    Feed("yrt", "York Region Transit", "https://www.yrt.ca/google/google_transit.zip",
         "https://rtu.york.ca/gtfsrealtime/TripUpdates",
         "https://rtu.york.ca/gtfsrealtime/VehiclePositions", color="#00a3e0"),
    Feed("drt", "Durham Region Transit", "https://maps.durham.ca/OpenDataGTFS/GTFS_Durham_TXT.zip",
         "https://drtonline.durhamregiontransit.com/gtfsrealtime/TripUpdates",
         "https://drtonline.durhamregiontransit.com/gtfsrealtime/VehiclePositions", color="#00843d"),
    Feed("hsr", "Hamilton Street Railway", "https://opendata.hamilton.ca/GTFS-Static/google_transit.zip",
         "https://opendata.hamilton.ca/GTFS-RT/GTFS_TripUpdates.pb",
         "https://opendata.hamilton.ca/GTFS-RT/GTFS_VehiclePositions.pb", color="#ffc20e"),
    Feed("oakville", "Oakville Transit", MDB.format("mdb-725"), color="#7a2a90"),
    Feed("burlington", "Burlington Transit", MDB.format("mdb-724"),
         "https://opendata.burlington.ca/gtfs-rt/GTFS_TripUpdates.pb",
         "https://opendata.burlington.ca/gtfs-rt/GTFS_VehiclePositions.pb", color="#003c71"),
]}


def rt_url(feed: Feed, url: str | None) -> str | None:
    """Return a usable realtime URL (with API key if needed) or None."""
    if not url:
        return None
    if feed.rt_key_env:
        key = os.environ.get(feed.rt_key_env)
        if not key:
            return None
        return f"{url}?key={key}"
    return url


DATA_DIR = os.environ.get("GTA_TRANSIT_DATA",
                          os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
RAW_DIR = os.path.join(DATA_DIR, "raw")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
