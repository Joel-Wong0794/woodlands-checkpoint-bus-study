"""Stage 0: one-off checks that LTA DataMall has the data this project needs.

Run during the day, when buses are running:
    python probe.py      # all checks
    python probe.py 1    # only checks 1-2 (BusArrival), e.g. on the VM
"""
import json
import os
import sys
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

from config import SERVICES, STOP_A, STOP_B

BASE = "https://datamall2.mytransport.sg/ltaodataservice"
ROOT = Path(__file__).parent
FIXTURES = ROOT / "tests" / "fixtures"
OD_CSV = ROOT / "data" / f"od_{STOP_A}_{STOP_B}.csv"
PAGE = 500


def load_key():
    """LTA key from the env var, or from .env (handy on Windows, where cron's `. ./.env` isn't used)."""
    key = os.environ.get("LTA_ACCOUNT_KEY")
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "LTA_ACCOUNT_KEY":
                return value.strip().strip("\"'")
    sys.exit("LTA_ACCOUNT_KEY is not set (env var or .env file)")


def get(path, key):
    """GET one DataMall endpoint. `path` includes the query string. Empty body -> {}."""
    r = requests.get(f"{BASE}/{path}", headers={"AccountKey": key, "accept": "application/json"}, timeout=30)
    r.raise_for_status()
    return r.json() if r.text.strip() else {}


def get_all(path, key):
    """Page through a 500-records-per-call dataset with $skip."""
    rows, skip = [], 0
    while True:
        batch = get(f"{path}?$skip={skip}", key).get("value", [])
        rows += batch
        if len(batch) < PAGE:
            return rows
        skip += PAGE


def is_real_coord(value):
    try:
        return float(value) != 0.0
    except (TypeError, ValueError):
        return False


# ---------- Checks 1 + 2: BusArrival ----------

def check_arrivals(key):
    """Save raw fixtures; count listed / monitored / GPS buses per stop x service."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    rows = []
    for stop in (STOP_A, STOP_B):
        payload = get(f"v3/BusArrival?BusStopCode={stop}", key)
        (FIXTURES / f"busarrival_{stop}.json").write_text(json.dumps(payload, indent=2))
        listed = {s["ServiceNo"]: s for s in payload.get("Services", [])}
        print(f"  {stop}: services listed = {sorted(listed)}")
        for svc in SERVICES:
            s = listed.get(svc)
            buses = [s.get(k) for k in ("NextBus", "NextBus2", "NextBus3")] if s else []
            buses = [b for b in buses if b and b.get("EstimatedArrival")]
            rows.append({
                "stop": stop,
                "service": svc,
                "listed": s is not None,
                "buses": len(buses),
                "monitored": sum(str(b.get("Monitored")) == "1" for b in buses),
                "with_gps": sum(is_real_coord(b.get("Latitude")) and is_real_coord(b.get("Longitude")) for b in buses),
                "etas": ", ".join(b["EstimatedArrival"][11:16] for b in buses),
            })
    table = pd.DataFrame(rows)
    print(table.to_string(index=False))
    return table


# ---------- Check 3: BusRoutes ----------

def check_routes(key):
    """Is STOP_A immediately followed by STOP_B in the same Direction?"""
    routes = pd.DataFrame(get_all("BusRoutes", key))
    print(f"  {len(routes)} route rows. Fields: {list(routes.columns)}")
    routes = routes[routes["ServiceNo"].isin(SERVICES)]
    ok = {}
    for svc in SERVICES:
        ok[svc] = False
        svc_routes = routes[routes["ServiceNo"] == svc]
        if svc_routes.empty:
            print(f"  {svc}: not in BusRoutes")
            continue
        for direction, r in svc_routes.groupby("Direction"):
            r = r.sort_values("StopSequence").reset_index(drop=True)
            codes = list(r["BusStopCode"])
            if STOP_A not in codes:
                print(f"  {svc} dir {direction}: {STOP_A} not on this direction")
                continue
            i = codes.index(STOP_A)
            nxt = codes[i + 1] if i + 1 < len(codes) else None
            ok[svc] = ok[svc] or nxt == STOP_B
            around = [f"[{c}]" if j == i else c for j, c in enumerate(codes) if i - 3 <= j <= i + 3]
            print(f"  {svc} dir {direction}: {' > '.join(around)}   next after {STOP_A} = {nxt}")
    return ok


# ---------- Check 4: BusStops ----------

def check_stops(key):
    stops = pd.DataFrame(get_all("BusStops", key))
    print(f"  {len(stops)} stops. Fields: {list(stops.columns)}")
    print(stops[stops["BusStopCode"].isin([STOP_A, STOP_B])].to_string(index=False))


# ---------- Check 5: BusServices ----------

def check_services(key):
    services = pd.DataFrame(get_all("BusServices", key))
    print(f"  Fields: {list(services.columns)}")
    print(services[services["ServiceNo"].isin(SERVICES)].to_string(index=False))


# ---------- Check 6: PV/ODBus ----------

def months_back(n):
    y, m = date.today().year, date.today().month - n
    while m < 1:
        y, m = y - 1, m + 12
    return f"{y}{m:02d}"


def check_od(key):
    """Download the latest OD file, keep only STOP_A -> STOP_B, print trips per hour."""
    for n in (1, 2, 3):
        ym = months_back(n)
        try:
            data = get(f"PV/ODBus?Date={ym}", key)
        except requests.HTTPError as e:  # LTA answers 404 for a month not yet published
            print(f"  {ym}: {e.response.status_code}, trying an earlier month")
            continue
        link = (data.get("value") or [{}])[0].get("Link")
        if link:
            break
        print(f"  {ym}: no link (response: {str(data)[:200]})")
    else:
        print("  No OD file available")
        return
    print(f"  Downloading OD file for {ym} ...")
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "od.zip"
        with requests.get(link, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        with zipfile.ZipFile(zip_path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            with z.open(name) as f:
                chunks = pd.read_csv(f, dtype=str, chunksize=500_000)
                od = pd.concat(c[(c["ORIGIN_PT_CODE"] == STOP_A) & (c["DESTINATION_PT_CODE"] == STOP_B)]
                               for c in chunks)
    OD_CSV.parent.mkdir(exist_ok=True)
    od.to_csv(OD_CSV, index=False)
    print(f"  Saved {len(od)} rows to {OD_CSV.relative_to(ROOT)}")
    od["TOTAL_TRIPS"] = od["TOTAL_TRIPS"].astype(int)
    od["TIME_PER_HOUR"] = od["TIME_PER_HOUR"].astype(int)
    by_hour = od.pivot_table(index="TIME_PER_HOUR", columns="DAY_TYPE", values="TOTAL_TRIPS",
                             aggfunc="sum", fill_value=0)
    print(f"  Passenger trips {STOP_A} -> {STOP_B} per hour, whole month {ym}:")
    print(by_hour.to_string())


# ---------- Main ----------

def run(label, fn, key):
    print(f"\n=== {label} ===")
    try:
        return fn(key)
    except Exception as e:  # a probe should report every check, not stop at the first failure
        print(f"  FAILED: {type(e).__name__}: {e}")
        return None


def main():
    key = load_key()
    only_arrivals = sys.argv[1:] == ["1"]
    print(f"Probe run at {datetime.now():%Y-%m-%d %H:%M:%S} (local time)")

    arrivals = run("Checks 1-2: BusArrival", check_arrivals, key)
    if only_arrivals:
        return
    routes_ok = run("Check 3: BusRoutes", check_routes, key) or {}
    run("Check 4: BusStops (copy coordinates into config.py)", check_stops, key)
    run("Check 5: BusServices (frequencies, minutes)", check_services, key)
    run("Check 6: PV/ODBus", check_od, key)

    print("\n=== GO / NO-GO ===")
    summary = []
    for svc in SERVICES:
        a = arrivals[arrivals["service"] == svc].set_index("stop") if arrivals is not None else None
        c1 = a is not None and bool(a["listed"].all())
        c2 = a is not None and int(a.loc[STOP_A, "monitored"]) > 0
        c3 = routes_ok.get(svc, False)
        summary.append({"service": svc, "1 listed both": c1, f"2 monitored @{STOP_A}": c2,
                        "3 A->B adjacent": c3, "verdict": "GO" if c1 and c2 and c3 else "NO-GO"})
    summary = pd.DataFrame(summary)
    print(summary.to_string(index=False))
    if not summary[f"2 monitored @{STOP_A}"].any():
        print(f"\n!! No service has Monitored = 1 at {STOP_A}. STOP: the design needs to change.")


if __name__ == "__main__":
    main()
