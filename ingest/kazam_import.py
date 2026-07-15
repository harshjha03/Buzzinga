#!/usr/bin/env python3
"""Kazam device-details CSV -> Postgres. Run from repo root:
    python3 -m ingest.kazam_import [path/to/outputs_dir]

Source: converted MongoDB export (devices_core.csv + device_sockets.csv).
Scope: public en-route chargers only — is_test=False, coords_valid=True,
is_private=False, segment='Enroute'. Home/Workplace/Fleet/RWA are excluded:
a passing driver can't plug in there.
# ponytail: Enroute-only gate; widen SEGMENTS if Kazam confirms other
# segments are publicly usable.

Devices within ~11 m of each other (4-decimal coords) collapse into one
canonical charger; every device keeps its own station_sources row
(source_ref = device_id). Idempotent — re-running updates in place.
"""
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from db import connect

DEFAULT_DIR = ("/Users/harshjha/Library/Application Support/Claude/"
               "local-agent-mode-sessions/09d06a71-6e85-4aaa-aa45-84abb63931dc/"
               "01c9a823-b120-4adb-bcaa-cc2b96f4ba56/"
               "local_5c62a992-5c61-4f3a-b920-37916ce67e24/outputs")
SEGMENTS = {"Enroute"}
ASSUMED_KW = {"DC": 25, "AC": 7}  # planner-safe lows, same as ocm_sync

# kazam connector slug -> (display title, current type)
CONNECTOR = {
    "ccs": ("CCS (Type 2)", "DC"),
    "gbt": ("GB-T", "DC"),
    "chademo": ("CHAdeMO", "DC"),
    "chogori": ("Chogori (2W)", "DC"),
    "type_2": ("Type 2", "AC"),
    "ac type 2": ("Type 2", "AC"),
    "three_pin": ("3-Pin", "AC"),
    "industrial_three_pin": ("Industrial 3-Pin", "AC"),
    "industrial": ("Industrial", "AC"),
    "ac001": ("Bharat AC-001", "AC"),
}


def load(outdir):
    """-> [(core row dict, [socket dicts])] for in-scope devices."""
    sockets = defaultdict(list)
    with open(outdir / "device_sockets.csv") as f:
        for r in csv.DictReader(f):
            sockets[r["device_id"]].append(r)
    devices = []
    with open(outdir / "devices_core.csv") as f:
        for r in csv.DictReader(f):
            if (r["is_test"] == "False" and r["coords_valid"] == "True"
                    and r["is_private"] == "False" and r["segment"] in SEGMENTS
                    and r["device_id"]):
                devices.append((r, sockets.get(r["device_id"], [])))
    return devices


def stalls_for(row, socks):
    """One device -> stall dicts (connector, current, kW, assumed flag)."""
    n = max(len(socks), 1)
    site_kw = None  # per-socket share of total_capacity_kw, if sane
    try:
        total = float(row["total_capacity_kw"])
        if 0 < total <= 1000:  # README: 17 corrupt values incl. billions
            site_kw = total / n
    except ValueError:
        pass
    out = []
    for s in socks or [{"connector_type": row["connector_types"].split("|")[0],
                        "capacity_kw": ""}]:
        slug = (s["connector_type"] or "").strip().lower()
        title, current = CONNECTOR.get(
            slug, (s["connector_type"] or "unknown", row["output_type"].upper() or "unknown"))
        try:
            kw, assumed = float(s["capacity_kw"]), False
        except ValueError:
            kw, assumed = site_kw, site_kw is None
            if kw is None:
                kw = ASSUMED_KW.get(current)
        out.append({"connector_type": title, "current_type": current,
                    "power_kw": kw, "kw_is_assumed": assumed})
    return out


def group_sites(devices):
    """Collapse devices at the same spot (~11 m) into one canonical site."""
    sites = defaultdict(list)
    for row, socks in devices:
        key = (round(float(row["latitude"]), 4), round(float(row["longitude"]), 4))
        sites[key].append((row, socks))
    return sites


def upsert_site(conn, members):
    row0 = members[0][0]
    lat, lon = float(row0["latitude"]), float(row0["longitude"])
    loc = f"POINT({lon} {lat})"
    name = row0["name"] or None
    address = row0["formatted_address"] or None
    verified = max((r["last_updated"] for r, _ in members if r["last_updated"]),
                   default=None) or None
    dev_ids = [r["device_id"] for r, _ in members]
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT charger_id FROM station_sources ss
               JOIN data_sources ds ON ds.id = ss.source_id
               WHERE ds.name='kazam' AND ss.source_ref = ANY(%s)""", (dev_ids,))
        hit = cur.fetchone()
        cid = hit["charger_id"] if hit else None
        if cid is None:
            # OCM overlap: a kazam-branded charger already within 100 m -> same site
            cur.execute(
                """SELECT c.id FROM chargers c
                   JOIN station_sources ss ON ss.charger_id = c.id
                   JOIN data_sources ds ON ds.id = ss.source_id AND ds.name = 'ocm'
                   WHERE (c.name ILIKE '%%kazam%%' OR c.operator ILIKE '%%kazam%%')
                     AND ST_DWithin(c.location, ST_GeogFromText(%s), 100) LIMIT 1""",
                (loc,))
            hit = cur.fetchone()
            cid = hit["id"] if hit else None
        if cid:
            cur.execute(
                """UPDATE chargers SET name=%s, operator='Kazam', address=%s,
                   location=%s, last_verified_at=coalesce(%s, last_verified_at),
                   updated_at=now() WHERE id=%s""",
                (name, address, loc, verified, cid))
        else:
            cur.execute(
                """INSERT INTO chargers (name, operator, address, location, status,
                                         last_verified_at)
                   VALUES (%s,'Kazam',%s,%s,'unknown',%s) RETURNING id""",
                (name, address, loc, verified))
            cid = cur.fetchone()["id"]
        cur.execute("DELETE FROM charger_stalls WHERE charger_id=%s", (cid,))
        for r, socks in members:
            for st in stalls_for(r, socks):
                cur.execute(
                    """INSERT INTO charger_stalls
                       (charger_id, connector_type, current_type, power_kw, kw_is_assumed)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (cid, st["connector_type"], st["current_type"],
                     st["power_kw"], st["kw_is_assumed"]))
            cur.execute(
                """INSERT INTO station_sources (source_id, source_ref, charger_id, raw)
                   VALUES ((SELECT id FROM data_sources WHERE name='kazam'), %s, %s, %s)
                   ON CONFLICT (source_id, source_ref)
                   DO UPDATE SET charger_id=excluded.charger_id, raw=excluded.raw,
                                 fetched_at=now()""",
                (r["device_id"], cid, json.dumps({"core": r, "sockets": socks})))
    return cid


def selfcheck():
    row = {"total_capacity_kw": "60", "connector_types": "ccs|ccs", "output_type": "dc"}
    socks = [{"connector_type": "ccs", "capacity_kw": "30"},
             {"connector_type": "ccs", "capacity_kw": ""}]
    st = stalls_for(row, socks)
    assert st[0] == {"connector_type": "CCS (Type 2)", "current_type": "DC",
                     "power_kw": 30.0, "kw_is_assumed": False}
    assert st[1]["power_kw"] == 30.0 and st[1]["kw_is_assumed"] is False  # site share
    st = stalls_for({"total_capacity_kw": "9999999", "connector_types": "three_pin",
                     "output_type": "ac"}, [])  # corrupt total -> assumed AC default
    assert st[0]["power_kw"] == 7 and st[0]["kw_is_assumed"] is True


def main():
    selfcheck()
    outdir = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR)
    devices = load(outdir)
    sites = group_sites(devices)
    print(f"{len(devices)} in-scope devices -> {len(sites)} sites")
    counts = Counter()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO data_sources (name) VALUES ('kazam') "
                        "ON CONFLICT (name) DO NOTHING")
        for members in sites.values():
            upsert_site(conn, members)
            counts["sites"] += 1
        with conn.cursor() as cur:
            cur.execute("UPDATE data_sources SET last_sync_at=now() WHERE name='kazam'")
    print(f"upserted {counts['sites']} chargers")


if __name__ == "__main__":
    main()
