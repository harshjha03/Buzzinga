"""Shared bits: .env loading + DB connection. Stdlib .env parser — no dotenv dep."""
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).parent


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def connect():
    load_env()
    return psycopg.connect(
        os.environ.get("DATABASE_URL", "dbname=buzzinga"), row_factory=dict_row
    )
