#!/usr/bin/env python3
"""Seed vehicles_catalog + charging_curves from data/vehicles.json.
Run from repo root: python3 -m ingest.seed_vehicles (idempotent)."""
import json

from db import ROOT, connect

vehicles = json.loads((ROOT / "data" / "vehicles.json").read_text())["vehicles"]

with connect() as conn, conn.cursor() as cur:
    for v in vehicles:
        ch = v["charging"]
        cur.execute(
            """INSERT INTO vehicles_catalog
               (id, make, model, variant, usable_kwh, max_dc_kw, max_ac_kw,
                dc_connector, ac_connector, highway_wh_per_km, data)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET
                 make=EXCLUDED.make, model=EXCLUDED.model, variant=EXCLUDED.variant,
                 usable_kwh=EXCLUDED.usable_kwh, max_dc_kw=EXCLUDED.max_dc_kw,
                 max_ac_kw=EXCLUDED.max_ac_kw, dc_connector=EXCLUDED.dc_connector,
                 ac_connector=EXCLUDED.ac_connector,
                 highway_wh_per_km=EXCLUDED.highway_wh_per_km, data=EXCLUDED.data""",
            (v["id"], v["make"], v["model"], v.get("variant"),
             v["battery"]["usable_kwh"], ch["max_dc_kw"], ch.get("max_ac_kw"),
             v["connectors"].get("dc"), v["connectors"].get("ac"),
             v.get("consumption", {}).get("highway_wh_per_km"), json.dumps(v)),
        )
        cur.execute("DELETE FROM charging_curves WHERE vehicle_id=%s", (v["id"],))
        for soc, kw in ch["dc_curve"]:
            cur.execute(
                "INSERT INTO charging_curves (vehicle_id, soc_pct, kw) VALUES (%s,%s,%s)",
                (v["id"], soc, kw),
            )
    print(f"seeded {len(vehicles)} vehicles")
