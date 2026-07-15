#!/usr/bin/env python3
"""Kazam ev_vehicles CSV -> tier-2 vehicle catalog (synthetic curves).

Run from repo root: python3 -m ingest.import_kazam_vehicles

Tier-2 = selectable in the picker, curve SYNTHESIZED (chemistry-agnostic
taper anchored to spec max DC kW), everything estimated is flagged in
`data` jsonb. Tier-1 (data/vehicles.json, measured/fitted curves) remains
the only planner-validated set; skip CSV rows that duplicate it.

Keeps: 4W Cars, BEV (>=20 kWh, drops PHEV noise), DC-fast capable.
Variants: dedup key is (brand, model, kWh) — "40.5 - 46.08" range strings
become one entry per endpoint, per the different-battery-different-range rule.
"""
import csv
import json
import re

from db import ROOT, connect

CSV = ROOT / "data" / "kazam_ev_vehicles.csv"

BRAND_ALIASES = {
    "tata motors": "Tata", "tata": "Tata",
    "mg motor": "MG", "mg motors": "MG", "mg": "MG",
    "mahindra electric": "Mahindra", "mahindra": "Mahindra",
    "byd india": "BYD", "byd": "BYD",
    "hyundai india": "Hyundai", "hyundai": "Hyundai",
    "kia": "Kia", "mercedez india": "Mercedes-Benz", "mercedes-benz": "Mercedes-Benz",
    "mercedes": "Mercedes-Benz", "pca automobiles": "Citroen", "citroen": "Citroen",
    "bmw mini": "Mini", "maruti suzuki": "Maruti Suzuki", "vinfast": "VinFast",
    "volvo": "Volvo", "bmw": "BMW", "audi": "Audi", "porsche": "Porsche",
    "tesla": "Tesla", "nissan": "Nissan", "volkswagen": "Volkswagen",
    "skoda": "Skoda", "jaguar": "Jaguar", "lexus": "Lexus", "toyota": "Toyota",
}

# Normalized DC acceptance vs SoC — conservative single template (NMC-ish
# taper; understates LFP plateaus -> overestimates charge time = planner-safe).
# ponytail: one template for all chemistries; split LFP/NMC if validation demands
TEMPLATE = [(0, 0.85), (10, 1.0), (20, 0.97), (35, 0.90), (50, 0.80),
            (65, 0.68), (80, 0.52), (88, 0.35), (93, 0.20), (97, 0.10), (100, 0.04)]


def battery_variants(raw):
    """'40.5 - 46.08' -> [40.5, 46.08]; junk text -> every plausible kWh in it."""
    return [v for v in (float(m) for m in re.findall(r"\d+(?:\.\d+)?", raw or ""))
            if 15 <= v <= 250]


# trim/marketing tokens that don't change the pack -- kWh is the real variant key
MODEL_STOP = {"ev", "electric", "kwh", "premium", "pro", "plus", "lr", "mr",
              "base", "standard", "select", "luxury", "recharge", "pack",
              "one", "two", "three", "four"}


def norm_model(model):
    """Dedup key: strip kWh tokens, years, floats, trim words. Keeps model
    numbers ('Ioniq 5' vs 'Ioniq 6' stay distinct)."""
    m = model.lower()
    m = re.sub(r"\d+(?:\.\d+)?\s*kwh", " ", m)
    m = re.sub(r"\b(19|20)\d{2}\b", " ", m)
    m = re.sub(r"\b\d+\.\d+\b", " ", m)
    words = [w for w in re.split(r"[^a-z0-9]+", m) if w and w not in MODEL_STOP]
    return " ".join(words)


def split_brand_prefix(brand, model):
    """Rows like brand='Hyundai India', model='Kia EV6' -> ('Kia', 'EV6')."""
    ml = model.lower()
    for alias in sorted(BRAND_ALIASES, key=len, reverse=True):
        if ml.startswith(alias + " "):
            return BRAND_ALIASES[alias], model[len(alias):].strip()
    return brand, model


def parse_ranges_km(raw):
    return [v for v in (float(m) for m in re.findall(r"\d+(?:\.\d+)?", raw or ""))
            if 50 <= v <= 1200]


def is_dc_capable(row):
    conns = (row.get("connector_type", "") + " " + row.get("connector_type2", "")).lower()
    return any(t in conns for t in ("ccs", "chademo", "gb/t", "gb-t"))


def synth_curve(peak_kw):
    return [[soc, round(peak_kw * f, 1)] for soc, f in TEMPLATE]


def charge_minutes(usable_kwh, curve, lo=10, hi=80):
    """Integrate SoC/power over the curve — same math as data/validate_vehicles.py."""
    mins = 0.0
    pts = {s: k for s, k in curve}
    socs = sorted(pts)
    for a, b in zip(socs, socs[1:]):
        s, e = max(a, lo), min(b, hi)
        if s >= e:
            continue
        p = (pts[a] + pts[b]) / 2
        mins += (e - s) / 100 * usable_kwh / p * 60
    return mins


def load_candidates():
    rows = list(csv.DictReader(open(CSV)))
    best = {}  # (brand, model, kwh_rounded) -> candidate
    for r in rows:
        if r.get("category") != "4W" or r.get("type") != "Car":
            continue
        if r.get("status", "").strip().lower() != "true" or not is_dc_capable(r):
            continue
        brand = BRAND_ALIASES.get(r["brand"].strip().lower())
        if not brand:
            continue  # unknown/global-only brands (Togg, BAIC...) add on demand
        model = re.sub(r"[^\x20-\x7e]+", "", r["model"]).strip()
        brand, model = split_brand_prefix(brand, model)
        model = re.sub(r"\d+(?:\.\d+)?\s*kwh", "", model, flags=re.I).strip(" -")
        dc = battery_variants(r.get("max_charging_power_dc_kw"))
        ac = battery_variants(r.get("max_charging_power_ac_kw"))
        kwhs = sorted(battery_variants(r.get("battery_capacity")))
        ranges = sorted(parse_ranges_km(r.get("range")))
        for i, kwh in enumerate(kwhs):  # pair sorted kWh<->range: '25-35 kWh' <-> '315-421 km'
            if kwh < 20:
                continue  # PHEVs / city quadricycles
            rng = ranges[min(i, len(ranges) - 1)] if ranges else None
            key = (brand, norm_model(model), round(kwh))
            cand = {"brand": brand, "model": model, "gross_kwh": kwh,
                    "dc_kw": dc[0] if dc else None, "ac_kw": ac[0] if ac else None,
                    "range_km": rng, "src_id": r["_id"]}
            old = best.get(key)
            if not old or (cand["dc_kw"] and not old["dc_kw"]):
                best[key] = cand
    return list(best.values())


def tier1_skiplist():
    t1 = json.loads((ROOT / "data" / "vehicles.json").read_text())["vehicles"]
    return [(v["make"].lower(), v["model"].lower(), v["battery"]["gross_kwh"]) for v in t1]


def calibrate_k(cands):
    """Highway Wh/km = k * cycle Wh/km (kWh*1000/brochure range). Fit k on
    tier-1 cars (measured highway consumption) matched to their CSV rows.
    Median k on 2026-07 data = 1.43, spread 1.28-1.97 -> tier-2 estimates
    are +-30%; display-grade only, planner stays tier-1-gated."""
    t1 = json.loads((ROOT / "data" / "vehicles.json").read_text())["vehicles"]
    ks = []
    for v in t1:
        real = v.get("consumption", {}).get("highway_wh_per_km")
        for c in cands:
            if (real and c["range_km"] and c["brand"].lower() == v["make"].lower()
                    and v["model"].lower().split()[0] in c["model"].lower()
                    and abs(c["gross_kwh"] - v["battery"]["gross_kwh"]) < 1.5):
                ks.append(real / (c["gross_kwh"] * 1000 / c["range_km"]))
                break
    ks.sort()
    return round(ks[len(ks) // 2], 2) if ks else 1.43


def selfcheck():
    assert battery_variants("40.5 - 46.08") == [40.5, 46.08]
    assert battery_variants("55 kWh, 72 kWh, and 86 kWh") == [55.0, 72.0, 86.0]
    assert battery_variants("") == []
    assert norm_model("Creta Electric 42 kWh") == norm_model("Creta") == "creta"
    assert norm_model("IONIQ 5") == "ioniq 5" != norm_model("Ioniq 6")
    assert norm_model("Kona Electric Premium 39.2 kWh") == norm_model("Kona Electric")
    assert split_brand_prefix("Hyundai", "Kia EV6") == ("Kia", "EV6")
    assert split_brand_prefix("Tata", "Nexon EV") == ("Tata", "Nexon EV")
    t = charge_minutes(42.75, synth_curve(60))  # ~45 kWh gross, 60 kW car
    assert 30 < t < 55, t  # published 10-80 for this class is ~40 min


def main():
    selfcheck()
    cands = load_candidates()
    skip = tier1_skiplist()
    k = calibrate_k(cands)
    print(f"consumption calibration k = {k}")
    n_dup = 0
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM vehicles_catalog WHERE data->>'tier'='2'")  # rebuild
        for c in cands:
            if any(mk == c["brand"].lower() and md.split()[0] in c["model"].lower()
                   and abs(kwh - c["gross_kwh"]) < 1.5 for mk, md, kwh in skip):
                n_dup += 1
                continue
            peak = c["dc_kw"] or round(c["gross_kwh"])  # no anchor -> assume ~1C
            usable = round(c["gross_kwh"] * 0.95, 1)
            curve = synth_curve(peak)
            if c["range_km"]:
                wh_km = k * c["gross_kwh"] * 1000 / c["range_km"]
            else:
                wh_km = 100 + 1.2 * c["gross_kwh"]  # ponytail: linear fallback, fit on tier-1
            wh_km = round(min(max(wh_km, 100), 250))
            vid = re.sub(r"[^a-z0-9]+", "-",
                         f"kazam {c['brand']} {c['model']} {c['gross_kwh']:g}".lower()).strip("-")
            cur.execute(
                """INSERT INTO vehicles_catalog
                   (id, make, model, variant, usable_kwh, max_dc_kw, max_ac_kw,
                    dc_connector, ac_connector, highway_wh_per_km, data)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO UPDATE SET usable_kwh=EXCLUDED.usable_kwh,
                     max_dc_kw=EXCLUDED.max_dc_kw, max_ac_kw=EXCLUDED.max_ac_kw,
                     highway_wh_per_km=EXCLUDED.highway_wh_per_km, data=EXCLUDED.data""",
                (vid, c["brand"], c["model"], f"{c['gross_kwh']:g} kWh",
                 usable, peak, c["ac_kw"], "CCS2", "Type2", wh_km,
                 json.dumps({"tier": 2, "curve_source": "synthetic",
                             "usable_is_estimated": True,
                             "dc_kw_is_assumed": c["dc_kw"] is None,
                             "consumption_est": "cycle*k" if c["range_km"] else "linear-fallback",
                             "consumption_k": k, "brochure_range_km": c["range_km"],
                             "est_10_80_min": round(charge_minutes(usable, curve)),
                             "source": "kazam_ev_vehicles", "source_id": c["src_id"],
                             "gross_kwh": c["gross_kwh"]})),
            )
            cur.execute("DELETE FROM charging_curves WHERE vehicle_id=%s", (vid,))
            for soc, kw in curve:
                cur.execute(
                    "INSERT INTO charging_curves (vehicle_id, soc_pct, kw) VALUES (%s,%s,%s)",
                    (vid, soc, kw))
        cur.execute("SELECT count(*) n FROM vehicles_catalog WHERE data->>'tier'='2'")
        n = cur.fetchone()["n"]
    print(f"{len(cands)} candidates, {n_dup} skipped as tier-1 duplicates, {n} tier-2 in catalog")


if __name__ == "__main__":
    main()
