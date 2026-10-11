"""Stage 3: measure 46219 -> 46109 trips from GPS tracks. Pure functions: data in, data out.

polls -> gps_points (one row per bus per minute) -> link_tracks (one track ~ one bus)
      -> find_trips (left the JB checkpoint -> reached 46109) -> check_trips (flags) -> slot_stats
"""
import numpy as np
import pandas as pd

from config import (BACK_M, BLIND_FLAG_MIN, GAP_SEC, GROUPS, GATE_A_LAT, LINK_SLACK_M, MAX_LINK_M, MAX_LOST_MIN, MAX_SPEED_MS, MAX_TRIP_MIN,
                    MIN_TRIP_MIN, MIN_TRIPS_PER_SLOT, SERVICE_DAY_START_HOUR, SERVICES, SLOT_MIN, STOP_A, STOP_B,
                    STOP_COORDS, ZONE_A_M, ZONE_B_M)

DUP_M = 60  # the same bus in both stops' lists, polled a second apart, can differ by ~20 m
POINT_COLS = ["t", "minute", "service", "lat", "lon", "dist_a", "dist_b", "at_a", "at_b"]
TRIP_COLS = ["service", "group", "track", "service_day", "reached_a", "left_a", "reached_b", "wait_min", "ride_min",
             "blind_min", "n_between", "blind", "ok", "reason"]


def metres(lat1, lon1, lat2, lon2):
    """Distance in metres. A flat-earth approximation is accurate to well under 1% at this scale."""
    k = 111_195
    dy = (np.asarray(lat2) - lat1) * k
    dx = (np.asarray(lon2) - lon1) * k * np.cos(np.radians(lat1))
    return np.hypot(dx, dy)


def group_of(service):
    return next(g for g, services in GROUPS.items() if service in services)


def to_frame(rows, cols):
    df = pd.DataFrame(rows)
    return df if not df.empty else pd.DataFrame(columns=cols)


# ---------- 1. GPS points ----------

def gps_points(polls, services=SERVICES):
    """One row per bus per minute, with its distance to each stop and whether it is 'at' a stop."""
    df = to_frame(polls, ["polled_at", "stop", "service", "lat", "lon"])
    df = df[df.service.isin(services) & df.lat.notna() & df.lon.notna()].copy()
    if df.empty:
        return pd.DataFrame(columns=POINT_COLS)
    df["t"] = pd.to_datetime(df.polled_at)
    df["minute"] = df.t.dt.floor("min")
    df = df.drop_duplicates(["minute", "service", "stop", "lat", "lon"])
    keep = []
    for _, g in df.groupby(["minute", "service"]):
        a, b = g[g.stop == STOP_A], g[g.stop == STOP_B]
        keep.extend(a.index)
        # a bus appears at most once per list, so match 46109 rows to 46219 rows one-to-one, closest first
        pairs = sorted((float(metres(ra.lat, ra.lon, rb.lat, rb.lon)), ia, ib)
                       for ia, ra in a.iterrows() for ib, rb in b.iterrows())
        used_a, dup_b = set(), set()
        for d, ia, ib in pairs:
            if d <= DUP_M and ia not in used_a and ib not in dup_b:
                used_a.add(ia)
                dup_b.add(ib)
        keep.extend(i for i in b.index if i not in dup_b)
    df = df.loc[sorted(keep)]
    df["dist_a"] = metres(*STOP_COORDS[STOP_A], df.lat, df.lon)
    df["dist_b"] = metres(*STOP_COORDS[STOP_B], df.lat, df.lon)
    df["at_a"] = (df.dist_a <= ZONE_A_M) & (df.lat >= GATE_A_LAT)
    df["at_b"] = df.dist_b <= ZONE_B_M
    return df[POINT_COLS].sort_values(["service", "t"]).reset_index(drop=True)


# ---------- 2. Tracks ----------

def link_tracks(points):
    """Add a 'track' id: link each minute's points to the nearest reachable track, one-to-one, closest first.

    A track ends when it has had no fix for MAX_LOST_MIN. A bus whose last fix was at 46109 has arrived: its
    track only takes a point still standing at the stop (within DUP_M) in the next minute.
    Points go first to tracks seen in the previous minute (closest first), then to arrived buses, and only
    then to tracks that have been lost for a while, so a lost track can't take over a bus being followed.
    South of the JB checkpoint a track can't move more than BACK_M against its direction: buses only turn round
    at the checkpoint, so such a link would be a jump to another bus (e.g. one that just switched its GPS on).
    """
    if points.empty:
        return points.assign(track=pd.Series(dtype=str))
    track = pd.Series("", index=points.index)
    for svc, ps in points.groupby("service"):
        active = []  # [id, last_t, lat, lon, last fix was at 46109, heading: -1 south / +1 north / 0 unknown]
        n = 0
        for minute, g in ps.groupby("minute"):
            active = [a for a in active if (minute - a[1]).total_seconds() <= (60 if a[4] else MAX_LOST_MIN * 60)]
            cand = []
            for i, r in g.iterrows():
                for j, (_, last_t, la, lo, arrived, heading) in enumerate(active):
                    if la < GATE_A_LAT and heading * (r.lat - la) * 111_195 < -BACK_M:
                        continue
                    gap = (minute - last_t).total_seconds()
                    d = float(metres(la, lo, r.lat, r.lon))
                    reach = DUP_M if arrived else min(MAX_SPEED_MS * gap + LINK_SLACK_M, MAX_LINK_M)
                    if d <= reach:
                        rank = 1 if arrived else (0 if gap <= 60 else 2)
                        cand.append((rank, d, i, j))
            used_i, used_j = set(), set()
            for _, d, i, j in sorted(cand):
                if i not in used_i and j not in used_j:
                    used_i.add(i)
                    used_j.add(j)
                    track[i] = active[j][0]
                    move = (g.at[i, "lat"] - active[j][2]) * 111_195
                    heading = active[j][5] if abs(move) < BACK_M else int(np.sign(move))  # a wobble never flips it
                    active[j][1:] = [minute, g.at[i, "lat"], g.at[i, "lon"], g.at[i, "at_b"], heading]
            for i in g.index:
                if i not in used_i:
                    n += 1
                    track[i] = f"{svc}-{n}"
                    active.append([track[i], minute, g.at[i, "lat"], g.at[i, "lon"], g.at[i, "at_b"], 0])
    return points.assign(track=track)


# ---------- 3. Trips ----------

def find_trips(tracks):
    """In each track: the last fix at the JB checkpoint before the first fix at 46109 after it.

    A new checkpoint visit starts when the previous fix there is more than MAX_LOST_MIN old.
    """
    rows = []
    for tid, g in tracks.sort_values("t").groupby("track"):
        reached_a = left_a = None
        times = g.t.tolist()
        for k, r in enumerate(g.itertuples()):
            if r.at_a:
                if left_a is None or (r.t - left_a).total_seconds() > MAX_LOST_MIN * 60:
                    reached_a = r.t
                left_a = r.t
            elif r.at_b and left_a is not None:
                ride = [x for x in times[:k + 1] if x >= left_a]
                blind = max(np.diff(ride)).total_seconds() / 60 if len(ride) > 1 else 0.0
                rows.append({"service": r.service, "track": tid, "reached_a": reached_a, "left_a": left_a,
                             "reached_b": r.t, "blind_min": blind, "n_between": len(ride) - 2})
                reached_a = left_a = None
    df = to_frame(rows, ["service", "track", "reached_a", "left_a", "reached_b", "blind_min", "n_between"])
    df["wait_min"] = (df.left_a - df.reached_a).dt.total_seconds() / 60 if len(df) else []
    df["ride_min"] = (df.reached_b - df.left_a).dt.total_seconds() / 60 if len(df) else []
    return df


# ---------- 4. Gaps and flags ----------

def poll_gaps(poll_runs):
    """Stretches where consecutive successful polls of a stop are more than GAP_SEC apart."""
    runs = to_frame(poll_runs, ["polled_at", "stop", "ok"])
    runs = runs[runs.ok == 1].copy()
    runs["t"] = pd.to_datetime(runs.polled_at)
    rows = []
    for stop, g in runs.sort_values("t").groupby("stop"):
        for prev, curr in zip(g.t, g.t.iloc[1:]):
            if (curr - prev).total_seconds() > GAP_SEC:
                rows.append({"stop": stop, "gap_from": prev, "gap_to": curr})
    return to_frame(rows, ["stop", "gap_from", "gap_to"])


def check_trips(trips, gaps):
    """Add service_day and quality flags. A bad trip only drops itself, never the whole day."""
    t = trips.copy()
    if t.empty:
        return pd.DataFrame(columns=TRIP_COLS)
    t["group"] = t.service.map(group_of)
    t["service_day"] = (t.left_a - pd.Timedelta(hours=SERVICE_DAY_START_HOUR)).dt.strftime("%Y-%m-%d")
    t["blind"] = t.blind_min > BLIND_FLAG_MIN
    in_gap = [any((gaps.gap_from < r.reached_b) & (gaps.gap_to > r.left_a)) for r in t.itertuples()]
    implausible = (t.ride_min < MIN_TRIP_MIN) | (t.ride_min > MAX_TRIP_MIN)
    # no fix between leaving 46219 and reaching 46109: the track jumped from one bus to another
    unseen = t.n_between < 1
    t["reason"] = np.where(implausible, "implausible", np.where(in_gap, "polling gap",
                           np.where(unseen, "not seen crossing", "")))
    t["ok"] = t.reason == ""
    return t[TRIP_COLS].sort_values(["service", "left_a"]).reset_index(drop=True)


def measure_trips(poll_runs, polls, services=SERVICES):
    """All trips found, with flags. Use trips[trips.ok] for stats."""
    tracks = link_tracks(gps_points(polls, services))
    return check_trips(find_trips(tracks), poll_gaps(poll_runs))


# ---------- 5. Stats ----------

def slot_stats(trips, by=("group", "weekday", "slot"), slot_min=SLOT_MIN):
    """n, median, P25, P75 of ride minutes per group. Slot = time left 46219, rounded down. Thin slots hidden."""
    t = trips[trips.ok.astype(bool)].copy() if "ok" in trips else trips.copy()
    cols = [*by, "n", "median", "p25", "p75"]
    if t.empty:
        return pd.DataFrame(columns=cols)
    t["group"] = t.service.map(group_of)
    t["weekday"] = pd.to_datetime(t.service_day).dt.strftime("%a")
    t["slot"] = pd.to_datetime(t.left_a).dt.floor(f"{slot_min}min").dt.strftime("%H:%M")
    s = t.groupby(list(by)).ride_min.agg(n="size", median="median", p25=lambda x: x.quantile(.25),
                                          p75=lambda x: x.quantile(.75)).reset_index()
    return s[s.n >= MIN_TRIPS_PER_SLOT][cols].reset_index(drop=True)


def departures_seen(trips, slot_min=SLOT_MIN):
    """Buses seen leaving the berth per group, service-day and slot (dropped trips included: they still left).

    A lower bound on the real frequency: buses without GPS when they leave are invisible.
    """
    cols = ["group", "service_day", "slot", "departures"]
    if trips.empty:
        return pd.DataFrame(columns=cols)
    t = trips.assign(group=trips.service.map(group_of),
                     slot=pd.to_datetime(trips.left_a).dt.floor(f"{slot_min}min").dt.strftime("%H:%M"))
    return t.groupby(["group", "service_day", "slot"]).size().rename("departures").reset_index()[cols]


def day_quality(trips, gaps):
    """Per service and service-day: trips kept, dropped (and why), flagged blind, polling gaps."""
    cols = ["service", "service_day", "n_trips", "n_dropped", "n_blind", "n_gaps"]
    if trips.empty:
        return pd.DataFrame(columns=cols)
    q = trips.groupby(["service", "service_day"]).agg(n_trips=("ok", "sum"), n_dropped=("ok", lambda x: (~x).sum()),
                                                      n_blind=("blind", "sum")).reset_index()
    gap_from = pd.to_datetime(gaps.gap_from)  # an empty gaps table has no datetime type
    g = gaps.assign(service_day=(gap_from - pd.Timedelta(hours=SERVICE_DAY_START_HOUR)).dt.strftime("%Y-%m-%d"))
    q["n_gaps"] = q.service_day.map(g.groupby("service_day").size()).fillna(0).astype(int)
    return q[cols]
