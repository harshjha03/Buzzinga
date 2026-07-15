# Buzzinga — Build Plan (backend-first)

Status: design in progress. This is the build sequence, not the build.
Companion docs: `buzzinga-blueprint.md` (architecture), `problem-solution-user-behaviour.md` (product).

## Principle

The blueprint describes the Phase-3 system. Launch needs ~1/3 of it.
Build one monolith with clean internal modules (`ingest/`, `planner/`, `api/`);
split into services only when a scaling reason exists. The planner is the
product — everything else feeds it.

## Deferred from the blueprint (v1 cuts)

| Blueprint | v1 |
|---|---|
| 3 services + API gateway | One monolith, modular internally |
| Full dedup (EVSE → proximity → review queue) | One source = no cross-source dedup. Upsert on OCM ID; normalize + store EVSE IDs; keep `canonical_station_id` as own column. Fuzzy matcher arrives with source #2. |
| Redis + route cache + TimescaleDB | Postgres+PostGIS only. Add caches when latency is measured. |
| Bayesian reliability + snapshots | Recency-weighted success ratio with a prior. Same gating, 20 lines. |
| Adapter registry w/ admin toggles & health | Adapter *interface* yes; registry UI no. Cron per adapter + `enabled` column. |
| Occupancy-by-hour wait model | `wait = 0` at launch; `checkins` table enables it later. |

Never simplify: canonical-ID discipline, per-stall data + `kw_is_assumed`,
charging-curve integration (never flat kWh÷kW), safety buffers, plan-B per stop.

## Milestones

### M0 — Validation spikes (throwaway scripts, before real code)
1. **OCM quality for India**: pull `/v3/poi` for Delhi–Jaipur, Mumbai–Pune,
   Bangalore–Chennai. Measure station count, % kW populated, % EVSE IDs,
   % connector types, `DateLastVerified` freshness. Decides buffer sizes and
   whether corridor coverage supports the promise.
2. **Vehicle catalog**: charging curves for Indian EVs (Nexon, ZS, XUV400,
   BE 6, Atto 3, Ioniq 5…) are not freely available. Decide: license dataset
   vs. hand-curate ~15 variants. Likely the hardest data problem.
3. **Routing + elevation cost**: Google vs. Mapbox vs. self-hosted
   Valhalla/OSRM (India extract). Planner makes many routing calls per plan
   (candidate detours) — cost multiplies. Lean: self-hosted Valhalla
   (elevation included).

### M1 — Schema + OCM sync — DONE 2026-07-13
Postgres+PostGIS. Tables: `data_sources`, `station_sources`, `chargers`,
`charger_stalls`, `checkins`, `vehicles_catalog`, `charging_curves`,
`user_vehicles`. One sync script: OCM bbox-grid over India → normalize →
upsert, 6-hourly cron.
**Exit:** `GET /chargers?bbox=` returns real chargers with provenance + kw flags. ✓

Built: `db/schema.sql`, `ingest/ocm_sync.py` (all-India 4×4 grid,
`compact=false` so OCM inlines type/status titles — no ID lookup tables),
`ingest/seed_vehicles.py`, `api/main.py` (FastAPI). Local: Homebrew
postgresql@18 + postgis, venv in `.venv`. First sync: 2,017 chargers,
2,724 stalls (only 90 kw_assumed — matches M0's ~95% kW coverage; 132 EVSE
ids, confirming EVSE-first dedup is dead for India).

Run: `brew services start postgresql@18`, then from repo root:
`.venv/bin/python -m ingest.ocm_sync` (cron: `0 */6 * * *`, not installed —
pointless until hosted), `.venv/bin/uvicorn api.main:app`.

**Tier-2 vehicle catalog (2026-07-13):** Kazam `ev_vehicles` scrape
(`data/kazam_ev_vehicles.csv`) imported via `ingest/import_kazam_vehicles.py`
— 182 4W BEVs, 22 brands. Curves are SYNTHETIC: one conservative taper
template anchored to spec max DC kW (or 1C when spec absent — 125 of 182),
usable = 0.95 × gross. All estimates flagged in `data` jsonb
(`tier: 2, curve_source: synthetic`); planner eligibility stays tier-1-only
until a vehicle gets a measured/fitted curve. Variant rule: one entry per
(brand, model, battery kWh). Known ceiling: template overestimates 800V
fast-chargers (EV6 est 12 min vs real ~18) and understates LFP plateaus —
acceptable because tier-2 is picker/prioritization, not planner input.

Tier-2 consumption is also estimated: highway Wh/km = k × cycle consumption
(kWh ÷ brochure range), k calibrated at import time against tier-1 measured
values (1.44 on 2026-07 data, spread 1.28–1.97 → ±30%). Clamped to
100–250 Wh/km; junk brochure ranges hit the clamp. Flags in `data` jsonb
(`consumption_est`, `consumption_k`). Display-grade only.

**Charger source #2 — Kazam network (2026-07-15):** MongoDB
`device-details` export (98,247 devices, converted dataset outside repo)
imported via `ingest/kazam_import.py`. Scope gate: not test, valid coords,
not private, segment=**Enroute** only (Home/Workplace/Fleet/RWA excluded —
not usable by a passing driver) → 4,166 devices → 3,601 sites (devices
within ~11 m / 4-decimal coords collapse into one charger; each device keeps
its own `station_sources` row, source_ref = device_id). Idempotent upsert.
DB now 5,618 chargers (2,017 OCM + 3,601 Kazam), Kazam adds 2,233 DC-fast
(≥24 kW) sites. Connector slugs mapped (ccs/gbt/chademo→DC, three_pin etc→AC);
per-socket kW, else total/num_sockets, else assumed 25 DC / 7 AC (flagged).
Cross-source dedup: only 3 Kazam-branded chargers existed in OCM; proximity
attach (100 m) restricted to OCM-sourced rows — an unrestricted version
self-merged neighboring Kazam sites and clobbered stalls (fixed 2026-07-15).
Most Kazam rows have no verification timestamp → trust prior (0.6) applies.

### M2 — Energy model (no chargers) — DONE 2026-07-13
Routing integration + per-segment consumption (speed, elevation, temp) +
reachability check.
**Exit:** origin/destination/vehicle/SoC → "arrive with X%" or "unreachable". ✓
Validate against real-world consumption for 2–3 vehicles before M3 —
15% error here makes everything downstream fiction. ✓ (see below)

Built: `routing/google_maps.py` (sole Google touchpoint: Routes API +
Elevation API, stdlib urllib), `planner/energy.py` (per-step speed factor
with v² aero + 0.85 city floor; climb term mgh/0.9 up, 60% regen down,
1.5 m noise threshold; temp step factor; 30%-of-flat floor),
`GET /route/reachability` in `api/main.py`. Offline checks in
`planner/test_energy.py`.

Validation (2026-07-13, temp 32°C): Mumbai→Pune Nexon 45 = 142 Wh/km vs
Pune→Mumbai 121 (ghat asymmetry matches owner logs ~21/18 kWh);
Atto 3 B'lore→Chennai 352 km = unreachable on one charge (real range
300–320 km — correct); ZS EV Delhi→Jaipur from 85% = −4.4% (reality:
barely — model errs conservative, the right direction). Errors ~10–15%.
Tier-2 vehicles get reachability with an "estimates only" warning.

### M3 — Planner (core IP, longest milestone) — DONE 2026-07-13
`POST /route/plan`: candidate filter → state graph (location × SoC, 5%
buckets) → Dijkstra/A* with 80% rule → charge time by curve integration →
itinerary + plan-B per stop.
**Exit:** corridor fixture suite passes (known trips, hand-computed stops). ✓

Built: `planner/plan.py`. Key design: ONE Routes call (+alternatives) + one
elevation profile per route; energy along the route is a prefix sum, so any
charger-pair leg is a subtraction — no per-pair routing calls. Off-route
chargers costed as round-trip detours at 40 km/h. gather() does all I/O and
returns serializable contexts; solve() is pure, so `planner/test_plan.py`
replays recorded fixtures offline (`planner/fixtures/`). The 80% rule is
emergent: curve taper makes >60-80% targets a losing trade, so Dijkstra
avoids them without a hard cap.

Findings that changed the design:
1. **Alternative routes are mandatory, not optional.** Google's default
   B'lore→Chennai route (via Chittoor) is a 314 km charger desert in OCM;
   the NH48/Vellore road has the chargers. Planner solves all ~3
   alternatives, picks fastest feasible (`routes_tried` in response).
2. **10 km detour radius** (was 5): Indian highway chargers sit in towns
   off the carriageway (Vellore lesson).
3. **Mumbai-side gap confirmed** (M0 finding 4): first DC charger on
   Mumbai→Pune is at km 123 — OCM lists nothing in Mumbai/expressway. A
   Tiago below ~75% SoC correctly gets "infeasible". Check-in seeded
   discovery (M4/source #2) is the real fix.

Fixtures: Nexon Mum→Pune 90% = nonstop arrive 40%; Atto 3 B'lore→Chennai
95% = 1 stop (120 kW, 45→55%, 16 min — hand-check: 6.05 kWh @ ~65 kW curve
+ 10 min overhead ✓); ZS Delhi→Jaipur 70% = 1 stop 30→60% 24 min; Tiago
Mum→Pune 75% = 1 stop at km-123 cluster (18 kW cap honoured).

**Custom stops (2026-07-15):** `POST /route/plan` accepts optional
`charger_ids` — the search then stops only at the user's picks (their explicit
choice bypasses the trust gate) and every number (SoC targets, curve-integrated
charge minutes, totals) recomputes through the same solve(). Each stop now
carries `alternatives` (up to 6 within ±30 km along-route, from the full pool
even when pinned, low-trust included since trust is shown). `gather()` results
are cached per (origin, dest, vehicle) for the process lifetime, so replans are
instant and don't re-bill Google. Infeasible picks return a friendly reason; no
route repair on pinned plans. PWA: "Change this charger" on each stop card →
alternatives picker → replan; "Reset to fastest plan" clears pins.

### M4 — Trust layer — DONE 2026-07-13
`POST /chargers/{id}/checkin`, simple reliability score, planner gating,
staleness penalty. Seed from OCM comment/check-in history.

Built: `planner/trust.py` (~20 lines as planned). Recency-weighted success
ratio with prior 0.6 (weight 2): check-ins decay 60-day half-life,
failures weigh 2× (stronger evidence), OCM DateLastVerified acts as one
half-credit synthetic success decaying 1-year half-life (this IS the OCM
seeding — comment history isn't in our compact sync; fetch
`includecomments` later if needed). Planner: trust < 0.30 → won't charge
there or offer as plan-B (~2 fresh failures); above gate, (1−trust)×30 min
risk padding steers search cost but is NOT reported as clock time. Tuning
rationale: 1 failure penalizes only — over-gating on thin corridors
(Mumbai–Pune's single km-123 cluster) would kill feasibility.
Tests: `planner/test_trust.py` (score behaviour + synthetic-corridor
steering). Live demo verified: 2 failure check-ins on the B'lore–Chennai
stop → replan rerouted to a fresher Vellore charger at +1 min total.

### M5 — PWA + trip loop — DONE 2026-07-13
MapLibre map, 4-input plan flow, itinerary cards, navigate handoff, one-tap
check-in. Trip monitoring / re-planning is a fast-follow post-launch.

Built: `web/` (single-file vanilla-JS PWA + manifest + service worker),
served by the API at `/`. Implements the Claude Design handoff
("Buzzinga Driver Journey" project → `Buzzinga Prototype.dc.html` + README
with tokens/motion/copy) pixel-faithfully: all 8 screens — splash (bolt
supergraphic), home (4-input sheet), itinerary (arrive hero, battery chart
with 80% cap + 15% floor, stop cards with reliability + Plan B, Start trip
/ Send to Maps), in-trip, charging, free-to-go, arrival + one-tap check-in,
home-again with saved route. Wired real: /vehicles picker (187 EVs,
MEASURED/ESTIMATED badges), POST /route/plan, real trust scores +
last-verified per stop, real check-ins (returns updated score), Google
Maps handoff with charger waypoints, geolocation for FROM.

Deviations (deliberate): in-trip/charging screens run the design's
~100×/20× simulation off the real plan's legs (live telemetry is the
post-launch fast-follow); charging cost is a flat ₹21/kWh labelled "est.";
fake amenity card (Third Wave Coffee) dropped — no data source behind it.

**Map + search made real (2026-07-13, post-M5):** Google Maps JS (via
`GET /config` key — restrict by HTTP referrer before pilot) on home/home2,
warm-styled to the brand — real geolocation marker, clustered live charger
pins from `GET /chargers?bbox` on every idle (amber = DC-fast ≥24 kW,
grey = AC-only; info window shows kW/operator/verified). Locate-me
buttons: crosshair in the FROM row + floating button on the map. Places
Autocomplete (New) on FROM/TO via `GET /geo/autocomplete` proxy (that key
stays server-side); selections plan by `place_id`, so ambiguous names
can't mis-geocode; degrades to free text if Places is down. Browser
geolocation needs HTTPS (or localhost) — fine at deploy.

**Pilot mode (2026-07-13):** all trip simulation removed — no timer-driven
screen transitions. In-trip shows the leg's plan numbers and waits for the
driver: "I've plugged in at the charger" → charging screen shows target %,
expected minutes and the plan rationale → "Reached N% — I'm done" →
free-to-go → "I've unplugged — resume trip" → final "I've arrived — finish
trip" → arrival + check-in. Splash keeps its 2.2 s brand moment
(tap-to-skip). Real telemetry replaces these buttons post-launch.

Also added this milestone: CORS middleware, GET /vehicles, stop lat/lon in
plan response, and **route repair** in the planner — Google's
computeAlternativeRoutes is best-effort (returned 3 routes in the morning,
1 in the afternoon for the same trip), so when every returned route is
infeasible the planner finds the biggest charger gap, searches 45 km
around its midpoint, and re-routes via the best charger there (2 attempts,
`rerouted via a charger corridor` warning). B'lore→Chennai now always
finds the Vellore road.

## Stack

- Backend: one service — FastAPI (Python) or Node/TS; pick by team fluency.
- DB: Postgres + PostGIS. Nothing else until measured.
- Routing: **DECIDED 2026-07-10 — Google Maps Platform** (Routes + Elevation +
  Places/Geocoding). Prototype scope, ≤100 users: ~15k calls/mo ≈ $25–50/mo
  after the 10k free cap. Revisit self-hosted Valhalla only if plan volume
  makes the bill hurt; the planner never talks to Google directly (goes
  through one internal routing module), so swapping later is contained.
- Jobs: cron. No queue infra.
- Client: PWA + MapLibre.

## M0 spike 1 results (OCM India, 2026-07-10, `spikes/ocm_spike.py`)

| Corridor (bbox incl. metros) | Stations | kW populated | Conn type | EVSE/ref | DC fast ≥50kW | Verified <1y | Median verify age |
|---|---|---|---|---|---|---|---|
| Delhi–Jaipur | 81 | 94% | 97% | 66% | 31 | 12% | 533 d |
| Mumbai–Pune | 26 | 90% | 94% | 0% | 4 | 81% | 92 d |
| Bangalore–Chennai | 213 | 99% | 96% | 1% | 167 | 4% | 490 d |

Findings → design impact:
1. **Field quality is good** (kW/connector ~90-99%) → `kw_is_assumed` fallback
   will be rare; energy math can trust OCM fields.
2. **Staleness is the real problem** — "100% operational" but median
   verification 1.3–1.5 years old on two corridors. Validates check-ins as
   essential; reliability gating must penalize staleness, not exclude on it
   (else almost nothing qualifies at launch). Fat buffers + plan-B confirmed.
3. **EVSE IDs mostly absent in India** (0–66%, mostly 0) → EVSE-ID-first
   dedup won't fire here; proximity+fingerprint becomes the *primary* match
   path when source #2 arrives. Revisit blueprint §1 assumption.
4. **Coverage is thin & OCM misses stations** — OSM has 48 Mumbai–Pune
   stations vs OCM's 26 (but OSM tags are useless: 2/48 socket types,
   0/48 power). Corridor viability needs a highway-gap check (max distance
   between DC-fast chargers along the route), esp. Mumbai–Pune (4 DC fast).
5. OSM ruled out as a data source; possible later use as a *discovery* hint
   list only.

## Open decisions (settle during design)

1. Launch geography: all-India vs. 3–5 corridors (corridors = easier validation).
2. ~~Vehicle catalog~~ DONE 2026-07-11: 5 variants hand-curated in
   `data/vehicles.json` (Nexon EV LR, Tiago EV LR, ZS EV, XUV400, Atto 3) —
   specs + DC curves (2 measured, 3 fitted to verified anchors), validated by
   `data/validate_vehicles.py` (recomputes published charge benchmarks from
   curves; all within 30%, most within 12%).
3. ~~Routing provider~~ DECIDED: Google Maps Platform (see Stack).
4. Anonymous planning? (accounts only for saved vehicles/check-ins — protects
   the four-input activation rule).
5. Hosting: with Google routing, everything fits Vercel (API + PWA + cron
   sync worker) — no VM needed for the prototype.
