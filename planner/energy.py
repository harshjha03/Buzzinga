"""Per-segment energy model + reachability (M2).

E = speed-adjusted rolling/aero term (per routing step) + net climb term
(per elevation segment, with regen credit on descents) + temperature factor.
Base consumption is the vehicle's highway_wh_per_km, referenced at 90 km/h.
"""
from db import connect
from routing.google_maps import elevation_profile, route

G = 9.81
RESERVE_SOC = 10       # warn below this at arrival
HARD_FLOOR_SOC = 5     # below this = unreachable
REF_KMH = 90           # speed the base Wh/km figure refers to
PAYLOAD_KG = 150


def speed_factor(kmh):
    """Aero grows with v^2; floor covers stop-go/HVAC overhead at low speed
    (Indian EVs report city ~= highway consumption)."""
    kmh = min(max(kmh, 15), 130)
    return max(0.85, 0.55 + 0.45 * (kmh / REF_KMH) ** 2)


def temp_factor(temp_c):
    return 1.10 if temp_c < 10 else 1.08 if temp_c > 32 else 1.0


def climb_wh(mass_kg, elevs):
    """Net Wh for elevation changes: climbs cost mgh/0.9, descents recover 60%."""
    wh = 0.0
    for a, b in zip(elevs, elevs[1:]):
        dh = b - a
        if abs(dh) < 1.5:
            continue  # elevation API noise on flat terrain
        wh += mass_kg * G * dh / 3600 / 0.9 if dh > 0 else mass_kg * G * dh / 3600 * 0.6
    return wh


def trip_energy_wh(base_wh_km, mass_kg, steps, elevs, temp_c):
    rolling = sum(d / 1000 * base_wh_km * speed_factor(d / max(t, 1) * 3.6)
                  for d, t in steps)
    total = (rolling + climb_wh(mass_kg, elevs)) * temp_factor(temp_c)
    dist_km = sum(d for d, _ in steps) / 1000
    return max(total, 0.3 * base_wh_km * dist_km)  # long descents can't go free


def get_vehicle(vehicle_id):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM vehicles_catalog WHERE id=%s", (vehicle_id,))
        v = cur.fetchone()
    if not v:
        raise KeyError(vehicle_id)
    d = v["data"] or {}
    v["mass_kg"] = (d.get("kerb_weight_kg") or 1000 + 9 * float(d.get("gross_kwh") or
                    v["usable_kwh"])) + PAYLOAD_KG  # est fits tier-1 kerbs within ~12%
    v["tier"] = int(d.get("tier", 1))
    return v


def reachability(origin, destination, vehicle_id, soc, temp_c=30):
    v = get_vehicle(vehicle_id)
    if not v["highway_wh_per_km"]:
        raise ValueError(f"{vehicle_id} has no consumption data")
    rt = route(origin, destination)
    dist_km = rt["distance_m"] / 1000
    elevs = elevation_profile(rt["polyline"], n_samples=int(min(max(dist_km, 50), 500)))
    wh = trip_energy_wh(float(v["highway_wh_per_km"]), v["mass_kg"],
                        rt["steps"], elevs, temp_c)
    arrive = soc - wh / 1000 / float(v["usable_kwh"]) * 100
    out = {
        "vehicle": vehicle_id, "distance_km": round(dist_km, 1),
        "duration_min": rt["duration_s"] // 60,
        "energy_kwh": round(wh / 1000, 2),
        "effective_wh_per_km": round(wh / dist_km),
        "start_soc": soc, "arrive_soc": round(arrive, 1),
        "reachable": arrive >= HARD_FLOOR_SOC,
        "warnings": [],
    }
    if HARD_FLOOR_SOC <= arrive < RESERVE_SOC:
        out["warnings"].append(f"arrives below {RESERVE_SOC}% reserve")
    if v["tier"] == 2:
        out["warnings"].append("estimates only: synthetic consumption data (tier-2 vehicle)")
    return out
