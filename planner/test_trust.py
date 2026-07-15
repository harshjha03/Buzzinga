"""M4 checks (offline): python3 -m planner.test_trust

Score behaviour + planner gating/steering on a synthetic 100 km corridor
with two equivalent chargers, one of which has fresh failures.
"""
from planner.plan import solve
from planner.trust import GATE, PRIOR, score

# --- score behaviour -------------------------------------------------------
assert score() == PRIOR                                   # no data -> prior
assert score(verify_age_days=10) > PRIOR                  # fresh OCM verify boosts
assert score(verify_age_days=500) > score(verify_age_days=10) - 0.1  # stale fades, doesn't tank
assert score([(1, 1), (2, 1)]) > 0.75                     # fresh successes lift the prior
assert score([(1, 0)]) < 0.45                             # one fresh failure hurts
assert score([(1, 0), (2, 0)]) < GATE                     # two fresh failures -> gated
assert score([(300, 0)]) > GATE                           # old failure decays back
assert score([(1, 1)], verify_age_days=1000) > score([(1, 0)], verify_age_days=10)

# --- planner steering ------------------------------------------------------
# straight ~111 km corridor (1 degree of latitude), two 50 kW chargers at
# the midpoint. Vehicle: 30 kWh usable, 150 Wh/km -> full trip needs ~55%.
def ctx(bad_first):
    pts = [(12 + i / 100, 77.0) for i in range(101)]
    chargers = []
    for cid, lat, checkins in ((1, 12.50, [[1, 0], [2, 0]] if bad_first else []),
                               (2, 12.51, [] if bad_first else [[1, 0], [2, 0]])):
        chargers.append({"id": cid, "name": f"c{cid}", "operator": "t", "status": "operational",
                         "last_verified": "2026-07-01", "verify_age_days": 10.0,
                         "lat": lat, "lon": 77.0, "detour_m": 0.0, "dc_kw": 50.0,
                         "kw_is_assumed": False, "checkins": checkins})
    return {"vehicle": {"id": "test", "usable_kwh": 30.0, "base_wh_km": 150.0,
                        "mass_kg": 1500.0, "max_dc_kw": 50.0, "tier": 1},
            "curve": [(0, 50.0), (100, 50.0)],
            "route": {"distance_m": 111_000, "duration_s": 4440,
                      "steps": [(111_000, 4440)]},
            "pts": pts, "elevs": [100.0] * 101, "chargers": chargers}

# start at 40%: must charge once mid-way; the failed charger must be avoided
for bad_first in (True, False):
    it = solve(ctx(bad_first), start_soc=40, temp_c=25)
    good = 2 if bad_first else 1
    assert it["feasible"], it
    assert len(it["stops"]) == 1
    assert it["stops"][0]["charger_id"] == good, (bad_first, it["stops"])
    assert it["stops"][0]["trust"] > 0.6
    pb = it["stops"][0]["plan_b"]
    assert pb is None, "gated charger must not be offered as plan-B"  # other one is bad

print("trust: all checks pass")
