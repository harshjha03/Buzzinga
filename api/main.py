"""Buzzinga API. Run from repo root: uvicorn api.main:app --reload

M1 exit criterion: GET /chargers?bbox=south,west,north,east
returns real chargers with provenance + kw flags.
"""
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from db import connect
from planner.energy import reachability
from planner.plan import plan
from planner.trust import charger_trust
from routing.google_maps import autocomplete, reverse_geocode

app = FastAPI(title="Buzzinga API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])  # ponytail: open CORS; pin origins at deploy


@app.get("/vehicles")
def vehicles():
    """Catalog for the picker: tier-1 first, then tier-2 (estimated)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, make, model, variant, usable_kwh, max_dc_kw,
                      highway_wh_per_km, dc_connector,
                      coalesce(data->>'tier','1')::int AS tier
               FROM vehicles_catalog WHERE max_dc_kw IS NOT NULL
               ORDER BY coalesce(data->>'tier','1'), make, model, usable_kwh""")
        return {"vehicles": cur.fetchall()}


@app.get("/config")
def config():
    """Browser config. The Maps JS key is meant to be public — restrict it by
    HTTP referrer in Google Cloud console before the pilot."""
    import os
    from db import load_env
    load_env()
    return {"maps_js_key": os.environ.get("GOOGLE_MAPS_API_KEY", "")}


@app.get("/geo/autocomplete")
def geo_autocomplete(q: str = Query(..., min_length=2), near: Optional[str] = None):
    """Places Autocomplete proxy — the Google key never reaches the browser.
    Degrades to an empty list if Places API isn't enabled on the key."""
    near_pt = None
    if near:
        try:
            lat, lng = (float(x) for x in near.split(","))
            near_pt = (lat, lng)
        except ValueError:
            pass
    try:
        return {"suggestions": autocomplete(q, near_pt)}
    except Exception as e:
        return {"suggestions": [], "error": str(e)[:200]}


@app.get("/geo/reverse")
def geo_reverse(at: str):
    """lat,lng -> address label for the drop-pin flow. None label is fine —
    the client falls back to raw coordinates."""
    try:
        lat, lng = (float(x) for x in at.split(","))
    except ValueError:
        raise HTTPException(422, "at must be lat,lng")
    try:
        return {"label": reverse_geocode(lat, lng)}
    except Exception:
        return {"label": None}


class CheckinRequest(BaseModel):
    result: Literal["success", "failure", "occupied"]
    user_ref: Optional[str] = None
    note: Optional[str] = None


@app.post("/chargers/{charger_id}/checkin")
def checkin(charger_id: int, req: CheckinRequest):
    """M4: one-tap check-in; returns the charger's updated trust score."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM chargers WHERE id=%s", (charger_id,))
        if not cur.fetchone():
            raise HTTPException(404, f"unknown charger {charger_id}")
        cur.execute(
            "INSERT INTO checkins (charger_id, user_ref, result, note) VALUES (%s,%s,%s,%s)",
            (charger_id, req.user_ref, req.result, req.note))
        return {"charger_id": charger_id, "result": req.result,
                "trust": round(charger_trust(cur, charger_id), 2)}


class PlanRequest(BaseModel):
    origin: str
    destination: str
    vehicle_id: str
    soc: float = Field(ge=0, le=100)
    temp_c: float = 30
    charger_ids: Optional[list[int]] = None  # user-customized stops -> replan


@app.post("/route/plan")
def route_plan(req: PlanRequest):
    """M3: charging itinerary with plan-B per stop. Pass charger_ids to
    restrict stops to the user's picks; SoC/times recompute for that set."""
    try:
        return plan(req.origin, req.destination, req.vehicle_id, req.soc,
                    req.temp_c, req.charger_ids)
    except KeyError:
        raise HTTPException(404, f"unknown vehicle_id {req.vehicle_id!r}")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/route/reachability")
def route_reachability(
    origin: str, destination: str, vehicle_id: str,
    soc: float = Query(..., ge=0, le=100), temp_c: float = 30,
):
    """M2 exit: origin/destination/vehicle/SoC -> 'arrive with X%' or unreachable."""
    try:
        return reachability(origin, destination, vehicle_id, soc, temp_c)
    except KeyError:
        raise HTTPException(404, f"unknown vehicle_id {vehicle_id!r}")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/chargers")
def chargers(bbox: str = Query(..., description="south,west,north,east")):
    try:
        s, w, n, e = (float(x) for x in bbox.split(","))
    except ValueError:
        raise HTTPException(422, "bbox must be south,west,north,east")
    if not (s < n and w < e):
        raise HTTPException(422, "bbox must have south<north and west<east")
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.name, c.operator, c.address, c.status,
                   c.last_verified_at,
                   ST_Y(c.location::geometry) AS lat,
                   ST_X(c.location::geometry) AS lon,
                   COALESCE(json_agg(DISTINCT jsonb_build_object(
                       'connector_type', st.connector_type,
                       'current_type', st.current_type,
                       'power_kw', st.power_kw,
                       'kw_is_assumed', st.kw_is_assumed,
                       'evse_id', st.evse_id,
                       'quantity', st.quantity
                   )) FILTER (WHERE st.id IS NOT NULL), '[]') AS stalls,
                   json_agg(DISTINCT jsonb_build_object(
                       'source', ds.name,
                       'ref', ss.source_ref,
                       'fetched_at', to_char(ss.fetched_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')
                   )) AS sources
            FROM chargers c
            LEFT JOIN charger_stalls st ON st.charger_id = c.id
            JOIN station_sources ss ON ss.charger_id = c.id
            JOIN data_sources ds ON ds.id = ss.source_id
            WHERE c.location && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
            GROUP BY c.id
            LIMIT 20000
            """,  # ponytail: cap fits all-India (~5.6k today); paginate past ~20k
            (w, s, e, n),
        )
        return {"chargers": cur.fetchall()}


# the PWA — mounted last so API routes win
app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "web", html=True),
          name="pwa")
