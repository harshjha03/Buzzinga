"""M4 trust: recency-weighted success ratio with a prior — the blueprint's
Bayesian reliability layer reduced to the ~20 lines the plan called for.

Seeding: OCM's DateLastVerified acts as one synthetic successful check-in
decaying with a 1-year half-life (M0: median verify age 490-533 days on two
corridors, so it must fade, not gate). Real check-ins decay with a 60-day
half-life. 'occupied' counts as working. The prior keeps unknown chargers
usable (penalize staleness, don't exclude on it).
"""
PRIOR = 0.6
PRIOR_WEIGHT = 2.0
CHECKIN_HALF_LIFE_D = 60
VERIFY_HALF_LIFE_D = 365
VERIFY_CREDIT = 0.5      # an OCM verification is weaker evidence than a user check-in
FAIL_WEIGHT = 2.0        # a failure report is stronger evidence than a success
GATE = 0.30              # ~2 fresh failures gate a charger; 1 only penalizes
RISK_PENALTY_MIN = 30    # search-cost padding per stop, scaled by (1 - trust)

OK_RESULTS = {"success": 1, "occupied": 1, "failure": 0}


def score(checkins=(), verify_age_days=None):
    """checkins: iterable of (age_days, ok 0/1). -> 0..1"""
    num, den = PRIOR_WEIGHT * PRIOR, PRIOR_WEIGHT
    if verify_age_days is not None:
        w = VERIFY_CREDIT * 0.5 ** (verify_age_days / VERIFY_HALF_LIFE_D)
        num, den = num + w, den + w
    for age_days, ok in checkins:
        w = 0.5 ** (age_days / CHECKIN_HALF_LIFE_D) * (1 if ok else FAIL_WEIGHT)
        num, den = num + w * ok, den + w
    return num / den


def charger_trust(cur, charger_id):
    cur.execute(
        """SELECT extract(epoch FROM now() - last_verified_at) / 86400 age
           FROM chargers WHERE id = %s""", (charger_id,))
    row = cur.fetchone()
    if not row:
        raise KeyError(charger_id)
    cur.execute(
        """SELECT extract(epoch FROM now() - created_at) / 86400 age, result
           FROM checkins WHERE charger_id = %s""", (charger_id,))
    events = [(float(r["age"]), OK_RESULTS.get(r["result"], 0)) for r in cur.fetchall()]
    return score(events, float(row["age"]) if row["age"] is not None else None)
