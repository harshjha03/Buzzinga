-- Buzzinga M1 schema. Apply: psql -d buzzinga -f db/schema.sql (idempotent)
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS data_sources (
    id           serial PRIMARY KEY,
    name         text UNIQUE NOT NULL,
    enabled      boolean NOT NULL DEFAULT true,
    last_sync_at timestamptz
);
INSERT INTO data_sources (name) VALUES ('ocm') ON CONFLICT (name) DO NOTHING;

-- chargers.id IS the canonical_station_id (own column per plan; one source
-- today so canonical == our serial. Fuzzy cross-source matching arrives with
-- source #2 and maps into this same id.)
CREATE TABLE IF NOT EXISTS chargers (
    id               serial PRIMARY KEY,
    name             text,
    operator         text,
    address          text,
    location         geography(Point, 4326) NOT NULL,
    status           text NOT NULL DEFAULT 'unknown',  -- operational|partly_operational|not_operational|planned|unknown
    last_verified_at timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chargers_location_idx ON chargers USING gist (location);

-- provenance: one row per (source, source's station id) -> canonical charger
CREATE TABLE IF NOT EXISTS station_sources (
    id         serial PRIMARY KEY,
    source_id  int NOT NULL REFERENCES data_sources(id),
    source_ref text NOT NULL,                     -- OCM POI ID
    charger_id int NOT NULL REFERENCES chargers(id) ON DELETE CASCADE,
    raw        jsonb,                             -- raw POI, for re-normalizing without refetch
    fetched_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, source_ref)
);

CREATE TABLE IF NOT EXISTS charger_stalls (
    id             serial PRIMARY KEY,
    charger_id     int NOT NULL REFERENCES chargers(id) ON DELETE CASCADE,
    connector_type text NOT NULL DEFAULT 'unknown',
    current_type   text NOT NULL DEFAULT 'unknown', -- AC|DC|unknown
    power_kw       numeric,
    kw_is_assumed  boolean NOT NULL DEFAULT false,
    evse_id        text,                            -- normalized ref; mostly absent in India (M0)
    quantity       int NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS charger_stalls_charger_idx ON charger_stalls (charger_id);

-- empty at launch; enables reliability scores (M4) and wait model later
CREATE TABLE IF NOT EXISTS checkins (
    id         serial PRIMARY KEY,
    charger_id int NOT NULL REFERENCES chargers(id) ON DELETE CASCADE,
    user_ref   text,
    result     text NOT NULL,  -- success|failure|occupied
    note       text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vehicles_catalog (
    id                text PRIMARY KEY,      -- e.g. tata-nexon-ev-lr-45
    make              text NOT NULL,
    model             text NOT NULL,
    variant           text,
    usable_kwh        numeric NOT NULL,
    max_dc_kw         numeric NOT NULL,
    max_ac_kw         numeric,
    dc_connector      text,
    ac_connector      text,
    highway_wh_per_km numeric,
    data              jsonb                  -- full curated record incl. sources/notes
);

CREATE TABLE IF NOT EXISTS charging_curves (
    vehicle_id text NOT NULL REFERENCES vehicles_catalog(id) ON DELETE CASCADE,
    soc_pct    numeric NOT NULL,
    kw         numeric NOT NULL,
    PRIMARY KEY (vehicle_id, soc_pct)
);

CREATE TABLE IF NOT EXISTS user_vehicles (
    id         serial PRIMARY KEY,
    user_ref   text NOT NULL,
    vehicle_id text NOT NULL REFERENCES vehicles_catalog(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
