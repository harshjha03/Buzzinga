"""The only module that talks to Google Maps Platform (swap point if provider
changes). Routes API for the drive, Elevation API for the profile."""
import json
import os
import urllib.parse
import urllib.request

from db import load_env

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
ELEV_URL = "https://maps.googleapis.com/maps/api/elevation/json"
FIELDS = ("routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline,"
          "routes.legs.steps.distanceMeters,routes.legs.steps.staticDuration")


def _key(name):
    load_env()
    key = os.environ.get(name) or os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        raise RuntimeError(f"{name} not set in .env")
    return key


def _waypoint(s):
    if s.startswith("place_id:"):
        return {"placeId": s[9:]}
    try:
        lat, lng = (float(x) for x in s.split(","))
        return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}
    except ValueError:
        return {"address": s}


def reverse_geocode(lat, lng):
    """(lat, lng) -> short human label, or None if geocoding unavailable."""
    url = ("https://maps.googleapis.com/maps/api/geocode/json?"
           + urllib.parse.urlencode({"latlng": f"{lat},{lng}",
                                     "key": _key("GOOGLE_MAPS_API_KEY")}))
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.load(r)
    results = data.get("results") or []
    return results[0]["formatted_address"] if results else None


def autocomplete(query, near=None):
    """Places Autocomplete (New), India-scoped. near=(lat, lng) biases results.
    -> [{place_id, main, secondary}]"""
    body = {"input": query, "includedRegionCodes": ["IN"]}
    if near:
        body["locationBias"] = {"circle": {
            "center": {"latitude": near[0], "longitude": near[1]}, "radius": 50000.0}}
    req = urllib.request.Request(
        "https://places.googleapis.com/v1/places:autocomplete",
        json.dumps(body).encode(),
        {"Content-Type": "application/json", "X-Goog-Api-Key": _key("GOOGLE_MAPS_API_KEY")})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    out = []
    for s in data.get("suggestions", []):
        p = s.get("placePrediction") or {}
        fmt = p.get("structuredFormat") or {}
        out.append({
            "place_id": p.get("placeId"),
            "main": (fmt.get("mainText") or {}).get("text")
                or (p.get("text") or {}).get("text", ""),
            "secondary": (fmt.get("secondaryText") or {}).get("text", ""),
        })
    return out


def routes(origin, destination, alternatives=False, via=None):
    """-> [{distance_m, duration_s, polyline, steps: [(dist_m, dur_s), ...]}]
    alternatives=True asks for up to ~3 distinct roads (best-effort — Google
    often returns just one). via=(lat, lng) forces the route through a point;
    the API forbids combining it with alternatives."""
    req_body = {
        "origin": _waypoint(origin), "destination": _waypoint(destination),
        "travelMode": "DRIVE", "routingPreference": "TRAFFIC_UNAWARE",
        "computeAlternativeRoutes": alternatives and not via,
    }
    if via:
        req_body["intermediates"] = [
            {"location": {"latLng": {"latitude": via[0], "longitude": via[1]}}}]
    body = json.dumps(req_body).encode()
    req = urllib.request.Request(ROUTES_URL, body, {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": _key("GOOGLE_ROUTES_API_KEY"),
        "X-Goog-FieldMask": FIELDS,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if not data.get("routes"):
        raise ValueError(f"no route: {data}")
    out = []
    for rt in data["routes"]:
        steps = [(s["distanceMeters"], int(s["staticDuration"].rstrip("s")))
                 for leg in rt["legs"] for s in leg["steps"]
                 if s.get("distanceMeters") and s.get("staticDuration")]
        out.append({"distance_m": rt["distanceMeters"],
                    "duration_s": int(rt["duration"].rstrip("s")),
                    "polyline": rt["polyline"]["encodedPolyline"], "steps": steps})
    return out


def route(origin, destination):
    return routes(origin, destination)[0]


def decode_polyline(enc):
    """Google encoded polyline -> [(lat, lng)]."""
    pts, i, lat, lng = [], 0, 0, 0
    while i < len(enc):
        for coord in ("lat", "lng"):
            shift = result = 0
            while True:
                b = ord(enc[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if coord == "lat":
                lat += delta
            else:
                lng += delta
        pts.append((lat / 1e5, lng / 1e5))
    return pts


def sample_polyline(polyline, n_samples):
    """~n_samples (lat, lng) vertices along the polyline."""
    pts = decode_polyline(polyline)
    step = max(1, len(pts) // n_samples)
    return pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])


def elevations(points):
    """Elevation (m) at each (lat, lng) point."""
    key = _key("GOOGLE_MAPS_API_KEY")
    elevs = []
    for i in range(0, len(points), 150):  # keep URLs well under limits
        locs = "|".join(f"{la:.5f},{ln:.5f}" for la, ln in points[i:i + 150])
        url = f"{ELEV_URL}?{urllib.parse.urlencode({'locations': locs, 'key': key})}"
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.load(r)
        if data["status"] != "OK":
            raise ValueError(f"elevation: {data['status']}")
        elevs += [res["elevation"] for res in data["results"]]
    return elevs


def elevation_profile(polyline, n_samples):
    return elevations(sample_polyline(polyline, n_samples))
