# Buzzinga — run it locally

EV route planner (India, prototype). Backend FastAPI + Postgres/PostGIS, frontend PWA served by the API.

## One-time setup

```sh
# 1. Postgres 18 + PostGIS (Homebrew installs both; postgis no longer pulls the server)
brew install postgresql@18 postgis
brew services start postgresql@18

# 2. Database + schema
/opt/homebrew/opt/postgresql@18/bin/createdb buzzinga
/opt/homebrew/opt/postgresql@18/bin/psql -d buzzinga -f db/schema.sql

# 3. Python env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 4. Secrets — .env in repo root (gitignored), needs:
#    OCM_API_KEY=...            (free: openchargemap.org → my apps)
#    GOOGLE_MAPS_API_KEY=...    (Elevation)
#    GOOGLE_ROUTES_API_KEY=...  (Routes)
#    DATABASE_URL=...           (optional; defaults to local dbname=buzzinga)

# 5. Data
.venv/bin/python -m ingest.ocm_sync                 # ~2,000 chargers, all India (~1 min)
.venv/bin/python -m ingest.seed_vehicles            # 5 curated tier-1 EVs
.venv/bin/python -m ingest.import_kazam_vehicles    # 182 tier-2 EVs from data/kazam_ev_vehicles.csv
```

## Every time

```sh
brew services start postgresql@18        # if not already running
.venv/bin/uvicorn api.main:app --port 8000
```

Open **http://localhost:8000** — that's the PWA (add to home screen on a phone
via your machine's LAN IP). API docs at http://localhost:8000/docs.

## Useful commands

```sh
# tests (all offline; planner fixtures re-record from live APIs if deleted)
.venv/bin/python -m ingest.test_normalize
.venv/bin/python -m planner.test_energy
.venv/bin/python -m planner.test_trust
.venv/bin/python -m planner.test_plan

# refresh charger data (cron this 6-hourly when hosted: 0 */6 * * *)
.venv/bin/python -m ingest.ocm_sync

# poke the API directly
curl "localhost:8000/chargers?bbox=18.3,72.7,19.3,74.1"
curl "localhost:8000/route/reachability?origin=Mumbai&destination=Pune&vehicle_id=tata-nexon-ev-lr-45&soc=60"
curl -X POST localhost:8000/route/plan -H 'Content-Type: application/json' \
  -d '{"origin":"Bangalore","destination":"Chennai","vehicle_id":"byd-atto3-60","soc":95}'
```

Plan requests take a few seconds — each one makes live Google Routes + Elevation
calls (route repair can add a couple more).

Docs: `PLAN.md` is the build log and source of truth for design decisions.
