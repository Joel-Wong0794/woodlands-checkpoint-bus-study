"""Fake poll generator for testing trips.py. Given TRUE buses, write poll_runs/polls rows as the collector would.

A bus is a dict: {"service", "at_a", "left_a", "at_b"} (times as "YYYY-MM-DD HH:MM[:SS]"): it reaches the boarding
stop at at_a, leaves it at left_a and reaches the alighting stop at at_b. Optional: "gps" (default True; False =
timetable only, no position) and "via_b" (default False; True = the bus passes the alighting stop first, like
the 950 loop from Singapore).

direction ("jb-sg" or "sg-jb", see config.DIRECTIONS) picks the two stops. Each minute both are polled. The
boarding stop lists the next 3 buses that haven't left it yet, the alighting stop the next 3 that haven't reached it
yet (plus 1 min standing at the stop). Like the real API, the same bus in both lists has the same position.
"""
import random
from datetime import datetime, timedelta

from config import DIRECTIONS, STOP_COORDS

FMT = "%Y-%m-%d %H:%M:%S"


def to_dt(s):
    return s if isinstance(s, datetime) else datetime.fromisoformat(s)


def toward(frm, to, metres):
    """The point `metres` from `frm` in the direction of `to`."""
    k = metres / (((to[0] - frm[0]) * 111_195) ** 2 + ((to[1] - frm[1]) * 111_195) ** 2) ** 0.5
    return frm[0] + k * (to[0] - frm[0]), frm[1] + k * (to[1] - frm[1])


def waypoints(bus, direction="jb-sg"):
    """(time, lat, lon) points the bus passes through; positions in between are interpolated.

    The bus approaches the boarding stop for 10 minutes (so even a short stop is seen inside the zone), leaves the
    zone exactly at left_a and enters the alighting zone exactly at at_b (it jumps across each edge in 1 s), so the
    only error left in the ride is the once-a-minute polling, which trips.py corrects for.
    """
    d = DIRECTIONS[direction]
    A, B = STOP_COORDS[d["board"]], STOP_COORDS[d["alight"]]
    at_a, left_a, at_b = to_dt(bus["at_a"]), to_dt(bus["left_a"]), to_dt(bus["at_b"])
    m, s1 = timedelta(minutes=1), timedelta(seconds=1)
    before, after = toward(A, B, -3000), toward(B, A, -1000)  # ~3 km before boarding, ~1 km after alighting
    if bus.get("via_b"):
        start = [(at_a - 16 * m, *after), (at_a - 12 * m, *B), (at_a - 11 * m, *B)]
    else:
        start = [(at_a - 10 * m, *before)]
    return start + [(at_a, *A), (left_a, *A), (left_a + s1, *toward(A, B, d["board_m"] + 50)),
                    (at_b - s1, *toward(B, A, d["alight_m"] + 50)), (at_b, *B), (at_b + m, *B)]


def position(wps, t):
    for (t0, la0, lo0), (t1, la1, lo1) in zip(wps, wps[1:]):
        if t0 <= t <= t1:
            f = 0 if t1 == t0 else (t - t0) / (t1 - t0)
            return la0 + f * (la1 - la0), lo0 + f * (lo1 - lo0)
    return None


def simulate(buses, start, end, missing=(), flicker=(), noise_m=0.0, seed=0, direction="jb-sg"):
    """Return (poll_runs, polls) as lists of dicts.

    missing: minutes ("YYYY-MM-DD HH:MM") with no poll at either stop, so no poll_runs row (a gap).
    flicker: (bus index, from, to): that bus isn't listed at either stop in this window.
    noise_m: GPS noise in metres (uniform, each axis).
    """
    rnd = random.Random(seed)
    missing = {to_dt(x) for x in missing}
    flicker = [(i, to_dt(a), to_dt(b)) for i, a, b in flicker]
    board, alight = DIRECTIONS[direction]["board"], DIRECTIONS[direction]["alight"]
    wps = [waypoints(b, direction) for b in buses]
    runs, polls = [], []
    t = to_dt(start)
    while t <= to_dt(end):
        if t not in missing:
            noise = {}
            # both stops at the same instant: the real collector polls them ~1 s apart, but here a bus crosses a
            # zone edge in 1 s, so a 1-s offset could show one bus in two places and break its track
            for stop, sec in ((board, 1), (alight, 1)):
                pt = t + timedelta(seconds=sec)
                listed = []
                for i, (bus, wp) in enumerate(zip(buses, wps)):
                    if any(j == i and a <= pt <= b for j, a, b in flicker):
                        continue
                    if not wp[0][0] <= pt <= wp[-1][0]:
                        continue
                    if stop == board and pt <= to_dt(bus["left_a"]):
                        listed.append((to_dt(bus["at_a"]), i))
                    if stop == alight and pt <= to_dt(bus["at_b"]) + timedelta(minutes=1):
                        listed.append((to_dt(bus["at_b"]), i))
                listed = sorted(listed)[:3]
                for seq, (eta, i) in enumerate(listed, start=1):
                    bus = buses[i]
                    lat = lon = None
                    if bus.get("gps", True):
                        if i not in noise:  # same noise in both stops' lists, as in real data
                            noise[i] = (rnd.uniform(-noise_m, noise_m) / 111_000, rnd.uniform(-noise_m, noise_m) / 111_000)
                        la, lo = position(wps[i], pt)
                        # each bus a couple of metres apart, like neighbouring spots in a bay: two real buses never
                        # report the exact same position, and identical rows would be merged as duplicates
                        lat, lon = la + noise[i][0], lo + noise[i][1] + (i % 7) * 0.00002
                    polls.append({"polled_at": pt.strftime(FMT), "stop": stop, "service": bus["service"], "seq": seq,
                                  "eta": eta.strftime(FMT), "monitored": int(bus.get("gps", True)), "lat": lat,
                                  "lon": lon, "load": "SEA", "bus_type": "SD", "origin": None, "destination": None})
                runs.append({"polled_at": pt.strftime(FMT), "stop": stop, "ok": 1,
                             "n_services": len({buses[i]["service"] for _, i in listed}), "error": None})
        t += timedelta(minutes=1)
    return runs, polls


def random_buses(n, service="170", first="2026-10-09 06:00", headway=(8, 15), wait=(0, 5), ride=(8, 22),
                 overtake=False, seed=0):
    """n buses with random headways, waits at 46219 and rides, on whole seconds.

    overtake=False keeps arrivals in departure order (at least 2 min apart), as in a single-lane queue.
    """
    rnd = random.Random(seed)
    t = to_dt(first)
    buses = []
    prev_arr = t
    for _ in range(n):
        t += timedelta(seconds=rnd.randint(headway[0] * 60, headway[1] * 60))
        left = t + timedelta(seconds=rnd.randint(wait[0] * 60, wait[1] * 60))
        arr = left + timedelta(seconds=rnd.randint(ride[0] * 60, ride[1] * 60))
        if not overtake:
            arr = max(arr, prev_arr + timedelta(minutes=2))
        prev_arr = arr
        buses.append({"service": service, "at_a": t.strftime(FMT), "left_a": left.strftime(FMT), "at_b": arr.strftime(FMT)})
    return buses
