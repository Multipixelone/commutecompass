"""Tiny static feed and production-shaped Directions fixture for bridge tests."""

from __future__ import annotations

import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from commutecompass.gtfs_allowlist import load_allowlist
from commutecompass.gtfs_index import GtfsIndex, import_feed, service_instant

DAY = date(2026, 10, 30)
BOARDING = service_instant(DAY, 8 * 3600 + 30 * 60)
NOW = BOARDING - timedelta(minutes=15)
TRIP = "WK_050800_A..N"


def build_index(path: Path, *, now: datetime = NOW, files: dict[str, str] | None = None) -> Path:
    """Import one allowlisted Utica Av → Kingston-Throop Avs trip."""
    data = {
        "feed_info.txt": "feed_publisher_name,feed_version\nMTA,bridge-fixture\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
        "A48,Utica Av,40.679364,-73.930729,1,\n"
        "A48N,Utica Av,40.679364,-73.930729,0,A48\n"
        "A48S,Utica Av,40.679364,-73.930729,0,A48\n"
        "A47,Kingston-Throop Avs,40.679921,-73.940858,1,\n"
        "A47N,Kingston-Throop Avs,40.679921,-73.940858,0,A47\n",
        "routes.txt": "route_id,route_short_name,route_type\nA,A,1\nC,C,1\n",
        "trips.txt": "route_id,trip_id,service_id,direction_id,shape_id\n"
        f"A,{TRIP},Weekday,0,A..N\n",
        "stop_times.txt": "trip_id,stop_id,arrival_time,departure_time,stop_sequence\n"
        f"{TRIP},A48N,08:30:00,08:30:00,10\n"
        f"{TRIP},A47N,08:32:00,08:32:00,11\n",
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\nWeekday,1,1,1,1,1,1,1,20261024,20270331\n",
    }
    data.update(files or {})
    archive = path.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w") as zf:
        for name, content in data.items():
            zf.writestr(name, content)
    import_feed(archive, path, feed_kind="supplemented", now=now,
                allowlist=[load_allowlist()[0]])
    return path


def transit_step(boarding: datetime = BOARDING) -> dict[str, Any]:
    return {
        "travel_mode": "TRANSIT", "duration": {"value": 120},
        "transit_details": {
            "line": {"short_name": "A", "name": "8 Avenue Express",
                     "agencies": [{"name": "MTA New York City Transit"}],
                     "vehicle": {"type": "SUBWAY"}},
            "departure_stop": {"name": "Utica Av",
                               "location": {"lat": 40.679364, "lng": -73.930729}},
            "arrival_stop": {"name": "Kingston-Throop Avs",
                             "location": {"lat": 40.679921, "lng": -73.940858}},
            "departure_time": {"value": int(boarding.timestamp()), "text": "8:30 AM",
                               "time_zone": "America/New_York"},
            "arrival_time": {"value": int((boarding + timedelta(minutes=2)).timestamp()),
                             "text": "8:32 AM", "time_zone": "America/New_York"},
            "headsign": "Inwood - 207 St", "num_stops": 1,
        },
    }


def directions(boarding: datetime = BOARDING) -> dict[str, Any]:
    depart = boarding - timedelta(minutes=5)
    arrive = boarding + timedelta(minutes=7)
    return {"status": "OK", "routes": [{"legs": [{
        "departure_time": {"value": int(depart.timestamp())},
        "arrival_time": {"value": int(arrive.timestamp())},
        "duration": {"value": 720},
        "steps": [{"travel_mode": "WALKING", "duration": {"value": 300},
                   "html_instructions": "Walk to Utica Av"}, transit_step(boarding),
                  {"travel_mode": "WALKING", "duration": {"value": 300},
                   "html_instructions": "Walk to destination"}],
    }]}]}


def open_index(path: Path, *, now: datetime = NOW) -> GtfsIndex:
    index = GtfsIndex.open(path, now=now.astimezone(UTC))
    assert index is not None
    return index
