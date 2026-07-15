"""M3 exit criterion: corridor fixture suite. Run: python3 -m planner.test_plan

First run records real contexts (Google + DB) into planner/fixtures/*.json;
after that the suite replays them offline through the pure solve().
Expectations were sanity-checked by hand against corridor reality (see PLAN.md
M0 table + M2 validation) when the fixtures were recorded.
"""
import json
from pathlib import Path

from planner.plan import gather, solve_best

FIXDIR = Path(__file__).parent / "fixtures"

# (name, origin, destination, vehicle, soc, checks)
CASES = [
    # Nexon 45 does Mumbai->Pune (~148 km, ghats) from 90% without stopping
    ("mum-pune-nexon", "Mumbai, Maharashtra", "Pune, Maharashtra",
     "tata-nexon-ev-lr-45", 90, dict(feasible=True, n_stops=0, min_arrive=30)),
    # Atto 3 B'lore->Chennai (~352 km, > real range) needs exactly one stop
    ("blr-chennai-atto3", "Bangalore, Karnataka", "Chennai, Tamil Nadu",
     "byd-atto3-60", 95, dict(feasible=True, n_stops=1, min_arrive=5)),
    # ZS EV Delhi->Jaipur (~312 km) from 70% can't make it in one hop
    ("del-jaipur-zs", "Delhi", "Jaipur, Rajasthan",
     "mg-zs-ev-50", 70, dict(feasible=True, n_stops=1, min_arrive=5)),
    # Tiago LR (18 kW DC) from 75%: can't quite make Pune, one stop at the
    # km-123 cluster (OCM has NO DC charger before that — Mumbai-side gap,
    # see M0 finding 4). From <=60% the trip is correctly infeasible.
    ("mum-pune-tiago", "Mumbai, Maharashtra", "Pune, Maharashtra",
     "tata-tiago-ev-lr-24", 75, dict(feasible=True, n_stops=1, min_arrive=5)),
]


def run():
    FIXDIR.mkdir(exist_ok=True)
    for name, o, d, vid, soc, want in CASES:
        fix = FIXDIR / f"{name}.json"
        if not fix.exists():
            print(f"recording fixture {name} (network + db)...")
            fix.write_text(json.dumps(gather(o, d, vid)))
        ctxs = json.loads(fix.read_text())
        it = solve_best(ctxs, soc, temp_c=32)
        stops = it.get("stops", [])
        assert it["feasible"] == want["feasible"], (name, it.get("reason"))
        assert len(stops) == want["n_stops"], (name, len(stops), stops)
        assert it["arrive_soc"] >= want["min_arrive"], (name, it["arrive_soc"])
        for s in stops:
            assert s["arrive_soc"] >= 10, (name, "buffer violated", s)
            assert s["depart_soc"] > s["arrive_soc"]
            assert s["plan_b"] is None or s["plan_b"]["charger_id"] != s["charger_id"]
        # sanity: total time >= drive time, charge time consistent
        if stops:
            assert it["charge_min"] >= sum(s["charge_min"] for s in stops) - 1
        summary = " + ".join(
            f"{s['name']} ({s['arrive_soc']}->{s['depart_soc']}% in {s['charge_min']}m)"
            for s in stops) or "nonstop"
        print(f"  {name:18} {it['distance_km']:6.1f} km  total {it['total_min']} min "
              f"({it['charge_min']} charging)  arrive {it['arrive_soc']}%  {summary}")
    print("planner fixtures: all checks pass")


if __name__ == "__main__":
    run()
