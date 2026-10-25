"""Stage 1: poll BusArrival for both stops and store the raw snapshot. Run by cron every minute."""
# Big picture: this is the only script that talks to LTA. cron starts it once a minute; it asks LTA about each of
# the 4 stops in config.COLLECT_STOPS, turns each answer into table rows and appends them to data/causeway.db, then
# exits. It calculates nothing: trips.py and dashboard.py work everything out later from these raw rows.
#
# The file has two halves:
#   - pure parsing (no network, no database), so the tests can feed it saved API answers;
#   - thin I/O: the HTTP call, the database, and main() that ties them together.
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from config import COLLECT_SERVICES, COLLECT_STOPS

URL = "https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival"
SGT = ZoneInfo("Asia/Singapore")
TIME_FMT = "%Y-%m-%d %H:%M:%S"  # every time in the database is Singapore time in this text format
DB_PATH = Path(__file__).parent / "data" / "causeway.db"

# Two tables (created the first time; "IF NOT EXISTS" makes it safe to run every minute):
#   poll_runs: one row per API call, even a failed or empty one. This is how we later tell "we asked and no bus
#              was listed" (normal at night) apart from "we didn't ask" (a gap: VM down, network error).
#   polls:     one row per bus LTA listed: which stop and service, its place in the queue (seq 1-3), ETA, and its
#              GPS position (lat/lon) when the bus reports one.
SCHEMA = """
CREATE TABLE IF NOT EXISTS poll_runs (
  polled_at  TEXT NOT NULL,
  stop       TEXT NOT NULL,
  ok         INTEGER NOT NULL,
  n_services INTEGER,
  error      TEXT,
  PRIMARY KEY (polled_at, stop)
);
CREATE TABLE IF NOT EXISTS polls (
  polled_at  TEXT NOT NULL,
  stop       TEXT NOT NULL,
  service    TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  eta        TEXT,
  monitored  INTEGER,
  lat REAL, lon REAL,
  load TEXT, bus_type TEXT,
  origin TEXT, destination TEXT,
  PRIMARY KEY (polled_at, stop, service, seq)
);
"""


# ---------- Pure parsing ----------

def blank_to_none(value):
    # LTA sends "" for missing values; the database stores those as NULL
    return None if value == "" else value


def to_coord(value):
    """'1.4654' -> 1.4654. '0.0', '' or missing -> None (no GPS)."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    # timetable-only buses (Monitored = 0) come with "0.0": that means "no position", not a place in the ocean
    return None if x == 0.0 else x


def to_sgt_text(iso):
    """'2026-10-08T22:42:21+08:00' -> '2026-10-08 22:42:21' in Singapore time."""
    return datetime.fromisoformat(iso).astimezone(SGT).strftime(TIME_FMT)


def parse_bus_arrival(payload, stop, polled_at, services):
    """One row per listed bus of our services. Blank NextBus2/3 produce no row."""
    # An LTA answer looks like {"Services": [{"ServiceNo": "170", "NextBus": {...}, "NextBus2": {...}, ...}, ...]}:
    # per service, up to 3 upcoming buses. Each listed bus becomes one row.
    rows = []
    for svc in (payload or {}).get("Services", []):
        if svc.get("ServiceNo") not in services:  # the stop serves other buses too; keep only ours
            continue
        for seq, field in enumerate(("NextBus", "NextBus2", "NextBus3"), start=1):
            bus = svc.get(field) or {}
            if not bus.get("EstimatedArrival"):  # an empty slot (e.g. only 1 bus coming): no row
                continue
            monitored = blank_to_none(bus.get("Monitored"))
            rows.append({
                "polled_at": polled_at,
                "stop": stop,
                "service": svc["ServiceNo"],
                "seq": seq,
                "eta": to_sgt_text(bus["EstimatedArrival"]),
                "monitored": None if monitored is None else int(monitored),  # 1 = live GPS, 0 = timetable guess
                "lat": to_coord(bus.get("Latitude")),
                "lon": to_coord(bus.get("Longitude")),
                "load": blank_to_none(bus.get("Load")),
                "bus_type": blank_to_none(bus.get("Type")),
                "origin": blank_to_none(bus.get("OriginCode")),
                "destination": blank_to_none(bus.get("DestinationCode")),
            })
    return rows


def count_services(payload, services):
    """How many of OUR services the API listed (even if all their buses were blank)."""
    return sum(s.get("ServiceNo") in services for s in (payload or {}).get("Services", []))


# ---------- Thin I/O ----------

def fetch(stop, key):
    # One HTTP GET to LTA for one stop. raise_for_status() turns an HTTP error (e.g. 401 bad key, 500) into an
    # exception, which poll_stop catches and records.
    r = requests.get(URL, params={"BusStopCode": stop},
                     headers={"AccountKey": key, "accept": "application/json"}, timeout=15)
    r.raise_for_status()
    return r.json() if r.text.strip() else {}  # no body at all = nothing running


def open_db(path=DB_PATH):
    path.parent.mkdir(exist_ok=True)
    # timeout=30: if the dashboard build is reading at the same moment, wait up to 30 s instead of failing
    con = sqlite3.connect(path, timeout=30)
    # WAL ("write-ahead log") lets readers and this writer work at the same time without blocking each other
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def save(con, run, rows):
    with con:  # one transaction per stop
        con.execute("INSERT INTO poll_runs VALUES (:polled_at, :stop, :ok, :n_services, :error)", run)
        con.executemany("INSERT INTO polls VALUES (:polled_at, :stop, :service, :seq, :eta, :monitored,"
                        " :lat, :lon, :load, :bus_type, :origin, :destination)", rows)


def poll_stop(con, stop, key):
    """Fetch, parse and store one stop. Returns a short log fragment and whether it worked."""
    polled_at = datetime.now(SGT).strftime(TIME_FMT)
    try:
        payload = fetch(stop, key)
        rows = parse_bus_arrival(payload, stop, polled_at, COLLECT_SERVICES)
        n = count_services(payload, COLLECT_SERVICES)
        save(con, {"polled_at": polled_at, "stop": stop, "ok": 1, "n_services": n, "error": None}, rows)
        return f"{stop} ok {n}svc {len(rows)}rows", True
    except Exception as e:  # one stop failing must not stop the other
        # still record the failed call (ok = 0, with the error text), so the analysis knows this minute is missing
        error = f"{type(e).__name__}: {e}"[:500]
        try:
            save(con, {"polled_at": polled_at, "stop": stop, "ok": 0, "n_services": None, "error": error}, [])
        except sqlite3.Error:
            pass
        return f"{stop} FAIL {error}", False


def main():
    # The key comes from the environment (cron loads it from .env); it is never written in the code
    key = os.environ.get("LTA_ACCOUNT_KEY")
    if not key:
        print("LTA_ACCOUNT_KEY is not set")
        return 1
    started = datetime.now(SGT).strftime(TIME_FMT)
    con = open_db()
    results = [poll_stop(con, stop, key) for stop in COLLECT_STOPS]
    con.close()
    # one log line per run, e.g. "2026-10-25 21:41:01 46219 ok 4svc 11rows | 46109 ok 4svc 8rows | ..."
    print(started, " | ".join(text for text, _ in results), flush=True)
    return 0 if all(ok for _, ok in results) else 1  # exit code 1 if any stop failed


if __name__ == "__main__":
    sys.exit(main())
