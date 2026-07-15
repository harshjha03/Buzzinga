#!/usr/bin/env python3
"""Recompute each vehicle's published DC benchmark from its curve; flag big gaps.

    python3 data/validate_vehicles.py
"""
import json
import pathlib


def kw_at(curve, soc):
    for (s1, p1), (s2, p2) in zip(curve, curve[1:]):
        if s1 <= soc <= s2:
            return p1 + (p2 - p1) * (soc - s1) / (s2 - s1)
    return curve[-1][1]


def charge_minutes(v, from_pct, to_pct, charger_kw):
    usable = v["battery"]["usable_kwh"]
    curve = v["charging"]["dc_curve"]
    hours = sum(
        (usable * 0.01) / min(kw_at(curve, s + 0.5), charger_kw)
        for s in range(from_pct, to_pct)
    )
    return hours * 60


def main():
    data = json.loads((pathlib.Path(__file__).parent / "vehicles.json").read_text())
    failures = []
    for v in data["vehicles"]:
        b = v["charging"]["published_benchmark"]
        calc = charge_minutes(v, b["from_pct"], b["to_pct"], b["charger_kw"])
        gap = (calc - b["minutes"]) / b["minutes"]
        flag = "" if abs(gap) <= 0.30 else "  <-- CHECK"
        print(f"{v['id']:24s} {b['from_pct']}-{b['to_pct']}% @ {b['charger_kw']}kW: "
              f"curve says {calc:.0f} min, published {b['minutes']} min ({gap:+.0%}){flag}")
        if abs(gap) > 0.30:
            failures.append(v["id"])
    assert not failures, f"curve vs published benchmark off by >30%: {failures}"
    print("OK: all curves within 30% of published benchmarks")


if __name__ == "__main__":
    main()
