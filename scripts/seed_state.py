#!/usr/bin/env python3
"""One-time state seed for the Railway worker.

Loads seed_state.json (exported from the local state.db) into Postgres, but ONLY
if the sent table is empty — so it's a no-op on every run after the first.
Also seeds `watches` from config.yaml accounts (channel subscriber) when empty.

Wired into the worker startCommand: `python3 scripts/seed_state.py && python3 ping.py`
Delete this file + seed_state.json once the hosted run is proven.
"""
import json
import os
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CHANNEL = "C0BLK715N31"  # #account-signals — original subscriber


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("seed: no DATABASE_URL — skipping")
        return 0
    import psycopg
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("""CREATE TABLE IF NOT EXISTS sent (
        subscriber_id TEXT NOT NULL, url_hash TEXT NOT NULL, title_hash TEXT,
        account TEXT, title TEXT, first_sent_at TEXT,
        PRIMARY KEY (subscriber_id, url_hash))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS watches (
        id SERIAL PRIMARY KEY, subscriber_id TEXT NOT NULL,
        canonical_name TEXT NOT NULL, aliases TEXT NOT NULL,
        active INTEGER DEFAULT 1, created_at TEXT)""")

    n = conn.execute("SELECT count(*) FROM sent").fetchone()[0]
    if n == 0:
        rows = json.loads((HERE / "seed_state.json").read_text())
        for r in rows:
            conn.execute(
                "INSERT INTO sent (subscriber_id,url_hash,title_hash,account,title,first_sent_at)"
                " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (r["subscriber_id"], r["url_hash"], r["title_hash"],
                 r["account"], r["title"], r["first_sent_at"]))
        print(f"seed: inserted {len(rows)} sent rows")
    else:
        print(f"seed: sent already has {n} rows — skipping")

    w = conn.execute("SELECT count(*) FROM watches").fetchone()[0]
    if w == 0:
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
        import datetime as dt
        ts = dt.datetime.now(dt.timezone.utc).isoformat()
        for a in cfg.get("accounts", []):
            conn.execute(
                "INSERT INTO watches (subscriber_id,canonical_name,aliases,active,created_at)"
                " VALUES (%s,%s,%s,1,%s)",
                (CHANNEL, a["name"], json.dumps(a["aliases"]), ts))
        print(f"seed: inserted {len(cfg.get('accounts', []))} watch rows for {CHANNEL}")
    else:
        print(f"seed: watches already has {w} rows — skipping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
