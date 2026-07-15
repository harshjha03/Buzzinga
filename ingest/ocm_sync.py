#!/usr/bin/env python3
"""OCM -> Postgres sync. Run from repo root: python3 -m ingest.ocm_sync

Fetches all-India POIs from Open Charge Map on a bbox grid, normalizes,
upserts on OCM ID (station_sources.source_ref). Stalls are replaced per
charger each sync. Cron: every 6h.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

from db import connect, load_env

API = "https://api.openchargemap.io/v3/poi"
INDIA = (6.5, 68.0, 36.0, 97.5)  # south, west, north, east
GRID = 4  # 4x4 tiles; warn if a tile hits the 5000 cap

# StatusTypeID -> our enum (unknown ids fall through to 'unknown')
STATUS = {
    50: "operational", 75: "partly_operational",
    30: "not_operational", 100: "not_operational",
    200: "not_operational", 210: "not_operational",
    150: "planned",
}
CURRENT = {10: "AC", 20: "AC", 30: "DC"}
# planner-safe (low) defaults when OCM has no PowerKW -> kw_is_assumed
ASSUMED_KW = {"DC": 25, "AC": 7}


def fetch_tile(bbox, key):
    s, w, n, e = bbox
    params = urllib.parse.urlencode({
        "output": "json",
        "boundingbox": f"({s},{w}),({n},{e})",
        "maxresults": 5000,
        "compact": "false",  # inline ConnectionType/StatusType/Operator titles — no ID lookup tables
        "verbose": "false",
        "key": key,
    })
    req = urllib.request.Request(
        f"{API}?{params}", headers={"User-Agent": "Buzzinga/0.1"}
    )  # OCM's edge 403s the default python user-agent
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def fetch_india(key):
    s, w, n, e = INDIA
    dlat, dlon = (n - s) / GRID, (e - w) / GRID
    pois = {}
    for i in range(GRID):
        for j in range(GRID):
            tile = (s + i * dlat, w + j * dlon, s + (i + 1) * dlat, w + (j + 1) * dlon)
            batch = fetch_tile(tile, key)
            if len(batch) >= 5000:
                print(f"WARN tile {tile} hit 5000 cap — shrink GRID tiles", file=sys.stderr)
            for p in batch:
                pois[p["ID"]] = p  # dedup boundary duplicates
            print(f"  tile {i},{j}: {len(batch)} POIs ({len(pois)} total)")
    return list(pois.values())


def normalize(poi):
    """OCM POI -> (charger dict, [stall dicts])."""
    a = poi.get("AddressInfo") or {}
    op = (poi.get("OperatorInfo") or {}).get("Title")
    address = ", ".join(
        x for x in (a.get("AddressLine1"), a.get("Town"), a.get("StateOrProvince")) if x
    )
    charger = {
        "name": a.get("Title"),
        "operator": op,
        "address": address or None,
        "lat": a.get("Latitude"),
        "lon": a.get("Longitude"),
        "status": STATUS.get(poi.get("StatusTypeID"), "unknown"),
        "last_verified_at": poi.get("DateLastVerified") or poi.get("DateLastStatusUpdate"),
    }
    stalls = []
    for c in poi.get("Connections") or []:
        ctype = (c.get("ConnectionType") or {}).get("Title") or "unknown"
        current = CURRENT.get(c.get("CurrentTypeID"), "unknown")
        if current == "unknown" and any(
            t in ctype for t in ("CCS", "CHAdeMO", "GB-T")
        ):
            current = "DC"
        kw = c.get("PowerKW")
        assumed = not kw
        if assumed:
            kw = ASSUMED_KW.get(current)  # None if current unknown too
        stalls.append({
            "connector_type": ctype,
            "current_type": current,
            "power_kw": kw,
            "kw_is_assumed": assumed,
            "evse_id": c.get("Reference") or None,
            "quantity": c.get("Quantity") or 1,
        })
    return charger, stalls


def upsert(conn, poi):
    charger, stalls = normalize(poi)
    if charger["lat"] is None or charger["lon"] is None:
        return False
    with conn.cursor() as cur:
        cur.execute(
            """SELECT charger_id FROM station_sources ss
               JOIN data_sources ds ON ds.id = ss.source_id
               WHERE ds.name = 'ocm' AND ss.source_ref = %s""",
            (str(poi["ID"]),),
        )
        row = cur.fetchone()
        loc = f"POINT({charger['lon']} {charger['lat']})"
        if row:
            cid = row["charger_id"]
            cur.execute(
                """UPDATE chargers SET name=%s, operator=%s, address=%s,
                   location=%s, status=%s, last_verified_at=%s, updated_at=now()
                   WHERE id=%s""",
                (charger["name"], charger["operator"], charger["address"],
                 loc, charger["status"], charger["last_verified_at"], cid),
            )
            cur.execute(
                """UPDATE station_sources SET raw=%s, fetched_at=now()
                   WHERE charger_id=%s AND source_ref=%s""",
                (json.dumps(poi), cid, str(poi["ID"])),
            )
        else:
            cur.execute(
                """INSERT INTO chargers (name, operator, address, location, status, last_verified_at)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (charger["name"], charger["operator"], charger["address"],
                 loc, charger["status"], charger["last_verified_at"]),
            )
            cid = cur.fetchone()["id"]
            cur.execute(
                """INSERT INTO station_sources (source_id, source_ref, charger_id, raw)
                   VALUES ((SELECT id FROM data_sources WHERE name='ocm'), %s, %s, %s)""",
                (str(poi["ID"]), cid, json.dumps(poi)),
            )
        cur.execute("DELETE FROM charger_stalls WHERE charger_id=%s", (cid,))
        for st in stalls:
            cur.execute(
                """INSERT INTO charger_stalls
                   (charger_id, connector_type, current_type, power_kw, kw_is_assumed, evse_id, quantity)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (cid, st["connector_type"], st["current_type"], st["power_kw"],
                 st["kw_is_assumed"], st["evse_id"], st["quantity"]),
            )
    return True


def main():
    load_env()
    key = os.environ.get("OCM_API_KEY")
    if not key:
        sys.exit("OCM_API_KEY not set (expected in .env)")
    pois = fetch_india(key)
    print(f"fetched {len(pois)} unique POIs")
    n = 0
    with connect() as conn:
        for poi in pois:
            n += upsert(conn, poi)
        with conn.cursor() as cur:
            cur.execute("UPDATE data_sources SET last_sync_at=now() WHERE name='ocm'")
    print(f"upserted {n} chargers")


if __name__ == "__main__":
    main()
