#!/usr/bin/env python3
"""M0 spike 1: Open Charge Map data quality for Indian corridors.

Usage:
    OCM_API_KEY=your-key python3 spikes/ocm_spike.py

Get a free key: https://openchargemap.org -> sign in -> my profile -> my apps.
Throwaway script; stdlib only.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CORRIDORS = {  # name: (south, west, north, east)
    "delhi-jaipur": (26.7, 75.5, 28.8, 77.4),
    "mumbai-pune": (18.3, 72.7, 19.3, 74.1),
    "bangalore-chennai": (12.6, 77.4, 13.4, 80.5),
}

API = "https://api.openchargemap.io/v3/poi"


def fetch(bbox, key):
    s, w, n, e = bbox
    params = urllib.parse.urlencode({
        "output": "json",
        "boundingbox": f"({s},{w}),({n},{e})",
        "maxresults": 5000,
        "compact": "true",
        "verbose": "false",
        "key": key,
    })
    req = urllib.request.Request(
        f"{API}?{params}", headers={"User-Agent": "Buzzinga-Spike/0.1"}
    )  # OCM's edge 403s the default python user-agent
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def pct(n, total):
    return f"{100 * n / total:.0f}%" if total else "n/a"


def analyze(name, pois):
    total = len(pois)
    conns = [c for p in pois for c in (p.get("Connections") or [])]
    with_kw = sum(1 for c in conns if c.get("PowerKW"))
    with_type = sum(1 for c in conns if c.get("ConnectionTypeID"))
    with_ref = sum(1 for c in conns if c.get("Reference"))
    operational = sum(
        1 for p in pois
        if (p.get("StatusTypeID") or 0) in (50, 75)  # operational / partly
    )
    now = datetime.now(timezone.utc)
    ages = []
    for p in pois:
        d = p.get("DateLastVerified") or p.get("DateLastStatusUpdate")
        if d:
            ages.append((now - datetime.fromisoformat(d.rstrip("Z")).replace(tzinfo=timezone.utc)).days)
    fresh_1y = sum(1 for a in ages if a <= 365)
    dc_fast = sum(1 for c in conns if (c.get("PowerKW") or 0) >= 50)

    print(f"\n== {name}: {total} stations, {len(conns)} connections ==")
    print(f"  operational status:        {pct(operational, total)}")
    print(f"  connections with kW:       {pct(with_kw, len(conns))}")
    print(f"  connections with type:     {pct(with_type, len(conns))}")
    print(f"  connections with ref/EVSE: {pct(with_ref, len(conns))}")
    print(f"  DC fast (>=50 kW) conns:   {dc_fast}")
    print(f"  verified within 1 year:    {pct(fresh_1y, total)} ({len(ages)} have any date)")
    if ages:
        ages.sort()
        print(f"  median verification age:   {ages[len(ages) // 2]} days")


def main():
    key = os.environ.get("OCM_API_KEY")
    if not key:
        sys.exit("Set OCM_API_KEY (free at https://openchargemap.org)")
    for name, bbox in CORRIDORS.items():
        analyze(name, fetch(bbox, key))


if __name__ == "__main__":
    main()
