"""M3 planner: candidate filter -> state graph (location x SoC, 5% buckets)
-> Dijkstra -> charge time by curve integration -> itinerary + plan-B per stop.

One Routes call + one elevation profile per plan. Energy along the route is
a prefix sum over samples, so any charger-pair leg is a subtraction; detours
to off-route chargers are costed as a round trip at 40 km/h.

gather() does all I/O (Google + DB) and returns a JSON-serializable context;
solve() is pure — the corridor fixture suite replays recorded contexts.
"""
import heapq
import math

from db import connect
from planner.energy import (G, HARD_FLOOR_SOC, RESERVE_SOC, get_vehicle,
                            speed_factor, temp_factor)
from planner.trust import GATE, OK_RESULTS, RISK_PENALTY_MIN, score
from routing.google_maps import elevations, routes, sample_polyline

BUCKET = 5            # SoC granularity (%), per plan
STOP_OVERHEAD_MIN = 10  # pull off, park, auth, plug
MAX_DETOUR_M = 10_000  # chargers sit in towns off the highway (Vellore lesson)
MIN_DC_KW = 24        # road-trip-usable DC
DETOUR_KMH = 40


def _hav_m(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 12_742_000 * math.asin(math.sqrt(h))


def gather(origin, destination, vehicle_id):
    """All I/O for one plan -> one serializable context per alternative route.
    An EV may need the longer road with chargers on it (B'lore->Chennai must
    go via Vellore/NH48, not the charger-desert Chittoor road).
    Cached so customizing stops re-solves without re-billing Google.
    # ponytail: process-lifetime cache; add TTL if trust staleness ever bites."""
    key = (origin, destination, vehicle_id)
    if key not in _GATHER_CACHE:
        v = get_vehicle(vehicle_id)
        _GATHER_CACHE[key] = [_ctx(rt, v, vehicle_id)
                              for rt in routes(origin, destination, alternatives=True)]
        while len(_GATHER_CACHE) > 32:
            _GATHER_CACHE.pop(next(iter(_GATHER_CACHE)))
    return _GATHER_CACHE[key]


_GATHER_CACHE = {}


def _ctx(rt, v, vehicle_id):
    dist_km = rt["distance_m"] / 1000
    pts = sample_polyline(rt["polyline"], int(min(max(dist_km, 50), 500)))
    elevs = elevations(pts)
    line = "LINESTRING(" + ",".join(f"{lng:.5f} {lat:.5f}" for lat, lng in pts) + ")"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT c.id, c.name, c.operator, c.status,
                      to_char(c.last_verified_at, 'YYYY-MM-DD') last_verified,
                      extract(epoch FROM now() - c.last_verified_at) / 86400 verify_age_days,
                      ST_Y(c.location::geometry) lat, ST_X(c.location::geometry) lon,
                      ST_Distance(c.location, ST_GeogFromText(%s)) detour_m,
                      max(st.power_kw) dc_kw, bool_or(st.kw_is_assumed) kw_is_assumed
               FROM chargers c
               JOIN charger_stalls st ON st.charger_id = c.id
               WHERE st.current_type = 'DC' AND st.connector_type ILIKE '%%CCS%%'
                 AND st.power_kw >= %s
                 AND c.status NOT IN ('not_operational', 'planned')
                 AND ST_DWithin(c.location, ST_GeogFromText(%s), %s)
               GROUP BY c.id""",
            (line, MIN_DC_KW, line, MAX_DETOUR_M),
        )
        chargers = [{**r, "detour_m": float(r["detour_m"]), "dc_kw": float(r["dc_kw"]),
                     "lat": float(r["lat"]), "lon": float(r["lon"]),
                     "verify_age_days": float(r["verify_age_days"])
                     if r["verify_age_days"] is not None else None,
                     "checkins": []} for r in cur.fetchall()]
        if chargers:
            by_id = {c["id"]: c for c in chargers}
            cur.execute(
                """SELECT charger_id, result,
                          extract(epoch FROM now() - created_at) / 86400 age
                   FROM checkins WHERE charger_id = ANY(%s)""",
                (list(by_id),))
            for r in cur.fetchall():
                by_id[r["charger_id"]]["checkins"].append(
                    [float(r["age"]), OK_RESULTS.get(r["result"], 0)])
        cur.execute("SELECT soc_pct, kw FROM charging_curves WHERE vehicle_id=%s ORDER BY 1",
                    (vehicle_id,))
        curve = [(float(r["soc_pct"]), float(r["kw"])) for r in cur.fetchall()]
    return {
        "vehicle": {"id": vehicle_id, "usable_kwh": float(v["usable_kwh"]),
                    "base_wh_km": float(v["highway_wh_per_km"]), "mass_kg": v["mass_kg"],
                    "max_dc_kw": float(v["max_dc_kw"]), "tier": v["tier"]},
        "curve": curve,
        "route": {"distance_m": rt["distance_m"], "duration_s": rt["duration_s"],
                  "steps": rt["steps"]},
        "pts": pts, "elevs": elevs, "chargers": chargers,
    }


def _curve_kw(curve, soc):
    for (s1, k1), (s2, k2) in zip(curve, curve[1:]):
        if s1 <= soc <= s2:
            return k1 + (k2 - k1) * (soc - s1) / (s2 - s1)
    return curve[-1][1]


def charge_minutes(curve, cap_kw, usable_kwh, soc_from, soc_to):
    """Curve integration — never flat kWh/kW (plan's never-simplify rule)."""
    mins = 0.0
    for s in range(int(soc_from), int(soc_to)):
        p = min(_curve_kw(curve, s + 0.5), cap_kw)
        mins += (usable_kwh / 100) / p * 60
    return mins


def _profiles(ctx, temp_c):
    """Prefix sums along route samples: distance (m), time (min), energy (Wh)."""
    pts, elevs = ctx["pts"], ctx["elevs"]
    v = ctx["vehicle"]
    # speed as a function of distance-along-route, from routing steps
    step_ends, step_kmh, d = [], [], 0.0
    for dist_m, dur_s in ctx["route"]["steps"]:
        d += dist_m
        step_ends.append(d)
        step_kmh.append(dist_m / max(dur_s, 1) * 3.6)
    tf = temp_factor(temp_c)
    cumd, cumt, cume = [0.0], [0.0], [0.0]
    si = 0
    for i in range(1, len(pts)):
        dd = _hav_m(pts[i - 1], pts[i])
        while si < len(step_ends) - 1 and cumd[-1] > step_ends[si]:
            si += 1
        kmh = step_kmh[si]
        wh = dd / 1000 * v["base_wh_km"] * speed_factor(kmh)
        dh = elevs[i] - elevs[i - 1]
        if abs(dh) >= 1.5:  # elevation noise threshold
            wh += (v["mass_kg"] * G * dh / 3600 / 0.9 if dh > 0
                   else v["mass_kg"] * G * dh / 3600 * 0.6)
        cumd.append(cumd[-1] + dd)
        cumt.append(cumt[-1] + dd / 1000 / max(kmh, 5) * 60)
        cume.append(cume[-1] + wh * tf)  # regen may dip the prefix; matches M2 model
    return cumd, cumt, cume


def solve(ctx, start_soc, temp_c=30, pinned=None):
    """Pure search over the recorded context -> itinerary dict.
    pinned = set of charger ids the user chose: the search may only stop at
    those (their explicit pick also bypasses the trust gate), but per-stop
    alternatives still come from the full candidate pool so the UI can keep
    offering swaps."""
    v, curve = ctx["vehicle"], ctx["curve"]
    cumd, cumt, cume = _profiles(ctx, temp_c)
    pts = ctx["pts"]

    # locations: 0 = origin, 1..m = chargers (sorted by offset), m+1 = destination
    all_cands = []
    for c in ctx["chargers"]:
        idx = min(range(len(pts)), key=lambda i: _hav_m(pts[i], (c["lat"], c["lon"])))
        trust = score(c.get("checkins", []), c.get("verify_age_days"))
        all_cands.append({**c, "sample_idx": idx, "offset_m": cumd[idx],
                          "trust": round(trust, 2)})
    all_cands.sort(key=lambda c: c["offset_m"])
    cands = [c for c in all_cands if pinned is None or c["id"] in pinned]
    locs = ([{"kind": "origin", "sample_idx": 0}] + [dict(c, kind="charger") for c in cands]
            + [{"kind": "dest", "sample_idx": len(pts) - 1}])
    dest = len(locs) - 1
    e_pct = lambda wh: wh / 1000 / v["usable_kwh"] * 100

    def drive(i, j):  # -> (minutes, soc_drop_pct)
        a, b = locs[i]["sample_idx"], locs[j]["sample_idx"]
        wh = cume[b] - cume[a]
        mins = cumt[b] - cumt[a]
        if locs[j]["kind"] == "charger":  # round-trip detour, charged on arrival
            det_km = locs[j]["detour_m"] / 1000 * 2
            wh += det_km * v["base_wh_km"]
            mins += det_km / DETOUR_KMH * 60
        return mins, e_pct(wh)

    start = (0, int(start_soc // BUCKET))
    best = {start: 0.0}
    prev = {}
    heap = [(0.0, *start)]
    goal = None
    while heap:
        cost, li, b = heapq.heappop(heap)
        if cost > best.get((li, b), 1e18):
            continue
        if li == dest:
            goal = (li, b)
            break
        soc = b * BUCKET
        for j in range(li + 1, len(locs)):  # drive forward
            mins, drop = drive(li, j)
            arrive = soc - drop
            floor = RESERVE_SOC if locs[j]["kind"] == "charger" else HARD_FLOOR_SOC
            if arrive < floor:
                continue
            key = (j, int(arrive // BUCKET))
            if cost + mins < best.get(key, 1e18):
                best[key] = cost + mins
                prev[key] = (li, b, "drive", mins)
                heapq.heappush(heap, (cost + mins, *key))
        if locs[li]["kind"] == "charger" and (pinned or locs[li]["trust"] >= GATE):  # charge up
            cap = min(locs[li]["dc_kw"], v["max_dc_kw"])
            # low-trust chargers cost extra search-time (expected failure risk)
            # but the padding is not reported as real minutes
            risk = (1 - locs[li]["trust"]) * RISK_PENALTY_MIN
            for b2 in range(b + 1, 100 // BUCKET + 1):
                mins = charge_minutes(curve, cap, v["usable_kwh"], soc, b2 * BUCKET) \
                    + STOP_OVERHEAD_MIN
                key = (li, b2)
                if cost + mins + risk < best.get(key, 1e18):
                    best[key] = cost + mins + risk
                    prev[key] = (li, b, "charge", mins)
                    heapq.heappush(heap, (cost + mins + risk, *key))

    out = {"vehicle": v["id"], "start_soc": start_soc,
           "distance_km": round(ctx["route"]["distance_m"] / 1000, 1),
           "n_candidates": len(cands), "warnings": []}
    if v["tier"] == 2:
        out["warnings"].append("estimates only: synthetic vehicle data (tier-2)")
    if goal is None:
        out.update(feasible=False,
                   reason="your chosen chargers can't cover this route at this "
                          "starting charge — pick an extra stop or reset to auto"
                   if pinned else
                   "no feasible plan: charger gap exceeds usable range "
                   f"({len(cands)} DC candidates within {MAX_DETOUR_M // 1000} km of route)")
        return out

    # walk back through prev to build the itinerary (real minutes, not
    # search cost — risk padding steers the choice but isn't clock time)
    node, hops = goal, []
    while node != start:
        pl, pb, kind, mins = prev[node]
        hops.append((kind, (pl, pb), node, mins))
        node = (pl, pb)
    hops.reverse()
    stops, charge_min, drive_min = [], 0.0, 0.0
    for kind, (li, b), (lj, b2), mins in hops:
        if kind == "drive":
            drive_min += mins
            continue
        c = locs[lj]
        charge_min += mins
        stops.append({
            "charger_id": c["id"], "name": c["name"], "operator": c["operator"],
            "lat": c["lat"], "lon": c["lon"],
            "detour_km": round(c["detour_m"] / 1000, 1),
            "offset_km": round(c["offset_m"] / 1000),
            "arrive_soc": b * BUCKET, "depart_soc": b2 * BUCKET,
            "charge_min": round(mins), "dc_kw": c["dc_kw"],
            "kw_is_assumed": c["kw_is_assumed"], "last_verified": c["last_verified"],
            "trust": c["trust"], "plan_b": _plan_b(all_cands, c),
            "alternatives": _alternatives(all_cands, c),
        })
    out.update(feasible=True, arrive_soc=goal[1] * BUCKET, stops=stops,
               drive_min=round(drive_min), charge_min=round(charge_min),
               total_min=round(drive_min + charge_min))
    if any(s["kw_is_assumed"] for s in stops):
        out["warnings"].append("a stop's charger kW is assumed, not reported")
    if out["arrive_soc"] < RESERVE_SOC:
        out["warnings"].append(f"arrives below {RESERVE_SOC}% reserve")
    return out


def _plan_b(cands, chosen):
    alts = [c for c in cands if c["id"] != chosen["id"] and c["trust"] >= GATE
            and abs(c["offset_m"] - chosen["offset_m"]) < 30_000]
    if not alts:
        return None
    c = min(alts, key=lambda c: abs(c["offset_m"] - chosen["offset_m"]) + c["detour_m"])
    return {"charger_id": c["id"], "name": c["name"], "operator": c["operator"],
            "dc_kw": c["dc_kw"], "detour_km": round(c["detour_m"] / 1000, 1),
            "offset_km": round(c["offset_m"] / 1000), "trust": c["trust"]}


def _alternatives(cands, chosen, n=6):
    """Nearby swap options for one stop (within ±30 km along the route),
    closest-and-least-detour first. Low-trust ones stay listed — trust is
    shown and picking one is the user's call."""
    pool = sorted(
        (c for c in cands if c["id"] != chosen["id"]
         and abs(c["offset_m"] - chosen["offset_m"]) < 30_000),
        key=lambda c: abs(c["offset_m"] - chosen["offset_m"]) + c["detour_m"])
    return [{"charger_id": c["id"], "name": c["name"], "operator": c["operator"],
             "dc_kw": c["dc_kw"], "kw_is_assumed": c["kw_is_assumed"],
             "detour_km": round(c["detour_m"] / 1000, 1),
             "offset_km": round(c["offset_m"] / 1000), "trust": c["trust"],
             "last_verified": c["last_verified"]} for c in pool[:n]]


def solve_best(ctxs, soc, temp_c=30, pinned=None):
    """Solve every alternative route, return the fastest feasible plan."""
    results = [solve(c, soc, temp_c, pinned) for c in ctxs]
    feasible = [r for r in results if r["feasible"]]
    best = min(feasible, key=lambda r: r["total_min"]) if feasible else results[0]
    best["routes_tried"] = len(results)
    return best


def _rescue_via(ctx, exclude):
    """The biggest charger gap along an infeasible route, and the best
    off-route charger near its middle (wider net than MAX_DETOUR_M) — used to
    force Google onto the road that actually has chargers (e.g. NH48/Vellore
    when it proposes the Chittoor desert)."""
    pts = ctx["pts"]
    cumd = [0.0]
    for a, b in zip(pts, pts[1:]):
        cumd.append(cumd[-1] + _hav_m(a, b))
    offs = sorted(
        cumd[min(range(len(pts)), key=lambda i: _hav_m(pts[i], (c["lat"], c["lon"])))]
        for c in ctx["chargers"]) or [0.0]
    edges = [0.0] + offs + [cumd[-1]]
    gap_a, gap_b = max(zip(edges, edges[1:]), key=lambda g: g[1] - g[0])
    mid_off = (gap_a + gap_b) / 2
    mid_idx = min(range(len(cumd)), key=lambda i: abs(cumd[i] - mid_off))
    lat, lon = pts[mid_idx]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT c.id, ST_Y(c.location::geometry) lat, ST_X(c.location::geometry) lon
               FROM chargers c JOIN charger_stalls st ON st.charger_id = c.id
               WHERE st.current_type = 'DC' AND st.connector_type ILIKE '%%CCS%%'
                 AND st.power_kw >= %s
                 AND c.status NOT IN ('not_operational', 'planned')
                 AND c.id != ALL(%s)
                 AND ST_DWithin(c.location, ST_GeogFromText(%s), 45000)
               GROUP BY c.id
               ORDER BY ST_Distance(c.location, ST_GeogFromText(%s)) LIMIT 1""",
            (MIN_DC_KW, list(exclude) or [0], f"POINT({lon} {lat})", f"POINT({lon} {lat})"))
        row = cur.fetchone()
    return (row["id"], float(row["lat"]), float(row["lon"])) if row else None


def plan(origin, destination, vehicle_id, soc, temp_c=30, charger_ids=None):
    """charger_ids: user-customized stop set — solve only over those chargers
    (no route repair; an infeasible pick reports back so the UI can undo)."""
    ctxs = gather(origin, destination, vehicle_id)
    if charger_ids:
        return solve_best(ctxs, soc, temp_c, pinned=set(charger_ids))
    best = solve_best(ctxs, soc, temp_c)
    if best["feasible"]:
        return best
    # route repair: pull the route through a charger cluster near the gap
    v = get_vehicle(vehicle_id)
    tried = set()
    for _ in range(2):
        found = _rescue_via(ctxs[0], tried)
        if not found:
            break
        cid, lat, lon = found
        tried.add(cid)
        rt = routes(origin, destination, via=(lat, lon))[0]
        result = solve(_ctx(rt, v, vehicle_id), soc, temp_c)
        best["routes_tried"] = best.get("routes_tried", 1) + 1
        if result["feasible"]:
            result["routes_tried"] = best["routes_tried"]
            result["warnings"].append("rerouted via a charger corridor off the default road")
            return result
    return best
