"""Stage 3: measure trips from GPS tracks, in either direction (config.DIRECTIONS). Pure functions: data in, data out.

polls -> gps_points (one row per bus per minute) -> link_tracks (one track ~ one bus)
      -> find_trips (left the boarding stop -> reached the alighting stop) -> check_trips (flags) -> slot_stats
"""
import math

import numpy as np
import pandas as pd

from config import (BACK_M, BLIND_FLAG_MIN, CAUSEWAY_LAT, DIRECTIONS, GAP_SEC, GROUPS, LINK_SLACK_M, MAX_LINK_M, POLL_SEC,
                    MAX_LOST_MIN, MAX_SPEED_MS, MAX_TRIP_MIN, MIN_TRIP_MIN, MIN_TRIPS_PER_SLOT, SERVICE_DAY_START_HOUR,
                    SERVICES, SLOT_MIN, STOP_COORDS)

DUP_M = 60  # the same bus in both stops' lists, polled a second apart, can differ by ~20 m
WOBBLE_M = 50  # GPS noise: a backward step up to this is treated as standing still
POINT_COLS = ["t", "minute", "service", "lat", "lon", "dist_a", "dist_b", "at_a", "at_b"]
TRIP_COLS = ["direction", "service", "group", "track", "service_day", "reached_a", "left_a", "reached_b", "wait_min", "ride_min",
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

def gps_points(polls, services=SERVICES, direction="jb-sg"):
    """One row per bus per minute, with its distance to the boarding (a) and alighting (b) stop of `direction`
    and whether it is 'at' each of them."""
    d = DIRECTIONS[direction]
    stop_a, stop_b = d["board"], d["alight"]
    df = to_frame(polls, ["polled_at", "stop", "service", "lat", "lon"])
    # only this direction's two stops: the collector polls both directions
    df = df[df.stop.isin([stop_a, stop_b]) & df.service.isin(services) & df.lat.notna() & df.lon.notna()].copy()
    if df.empty:
        return pd.DataFrame(columns=POINT_COLS)
    df["t"] = pd.to_datetime(df.polled_at)
    df["minute"] = df.t.dt.floor("min")
    df = df.drop_duplicates(["minute", "service", "stop", "lat", "lon"])
    # A bus appears at most once per list, so match 46109 rows to 46219 rows one-to-one, closest first,
    # and drop the matched 46109 rows. Only pairs within DUP_M can match, so find those first (vectorised).
    a = df[df.stop == stop_a].reset_index()
    b = df[df.stop == stop_b].reset_index()
    pairs = a.merge(b, on=["minute", "service"], suffixes=("_a", "_b"))
    pairs["d"] = metres(pairs.lat_a.values, pairs.lon_a.values, pairs.lat_b.values, pairs.lon_b.values)
    used_a, dup_b = set(), set()
    for ia, ib in pairs[pairs.d <= DUP_M].sort_values("d")[["index_a", "index_b"]].itertuples(index=False):
        if ia not in used_a and ib not in dup_b:
            used_a.add(ia)
            dup_b.add(ib)
    df = df.drop(index=list(dup_b)).sort_index()
    df["dist_a"] = metres(*STOP_COORDS[stop_a], df.lat, df.lon)
    df["dist_b"] = metres(*STOP_COORDS[stop_b], df.lat, df.lon)
    df["at_a"] = (df.dist_a <= d["board_m"]) & df.lat.between(*d["board_lat"])
    df["at_b"] = (df.dist_b <= d["alight_m"]) & df.lat.between(*d["alight_lat"])
    return df[POINT_COLS].sort_values(["service", "t"]).reset_index(drop=True)


# ---------- 2. Tracks ----------

def link_tracks(points):
    """Add a 'track' id: link each minute's points to the nearest reachable track, one-to-one, closest first.

    A track ends when it has had no fix for MAX_LOST_MIN. A bus whose last fix was at the alighting stop has
    arrived: its track only takes a point still standing at the stop (within DUP_M) in the next minute.
    Points go first to tracks seen in the previous minute (closest first), then to arrived buses, and only
    then to tracks that have been lost for a while, so a lost track can't take over a bus being followed.
    On the Causeway (CAUSEWAY_LAT) a track can't move more than BACK_M against its direction: buses only turn round
    off it, so such a link would be a jump to another bus (e.g. one that just switched its GPS on). Smaller
    backward steps (more than WOBBLE_M) are allowed but come last, so two buses close together don't swap.
    """
    if points.empty:
        return points.assign(track=pd.Series(dtype=str))
    # plain arrays: row-by-row pandas access was ~90% of the run time
    svc_all = points.service.to_numpy()
    minute_all = points.minute.to_numpy()
    lat_all, lon_all = points.lat.to_numpy(float), points.lon.to_numpy(float)
    at_b_all = points.at_b.to_numpy(bool)
    track = np.empty(len(points), dtype=object)
    k = 111_195
    for svc in sorted(set(svc_all)):
        rows = np.flatnonzero(svc_all == svc)
        rows = rows[np.argsort(minute_all[rows], kind="stable")]
        active = []  # [id, last minute, lat, lon, last fix at the alighting stop, heading: -1 south / +1 north / 0 unknown]
        n = 0
        bounds = np.flatnonzero(np.diff(minute_all[rows].astype("int64"))) + 1
        for g in np.split(rows, bounds):
            minute = minute_all[g[0]]
            active = [a for a in active
                      if (minute - a[1]) / np.timedelta64(1, "s") <= (60 if a[4] else MAX_LOST_MIN * 60)]
            cand = []
            for i in g:
                lat, lon = lat_all[i], lon_all[i]
                for j, (_, last_t, la, lo, arrived, heading) in enumerate(active):
                    on_causeway = CAUSEWAY_LAT[0] <= la < CAUSEWAY_LAT[1]
                    ahead = heading * (lat - la) * k  # metres moved in the track's direction
                    if on_causeway and ahead < -BACK_M:
                        continue
                    back = on_causeway and ahead < -WOBBLE_M
                    gap = (minute - last_t) / np.timedelta64(1, "s")
                    d = math.hypot((lon - lo) * k * math.cos(math.radians(la)), (lat - la) * k)  # = metres()
                    reach = DUP_M if arrived else min(MAX_SPEED_MS * gap + LINK_SLACK_M, MAX_LINK_M)
                    if d <= reach:
                        rank = 1 if arrived else (0 if gap <= 60 else 2)
                        cand.append((rank, back, d, i, j))
            used_i, used_j = set(), set()
            for _, _, d, i, j in sorted(cand):
                if i not in used_i and j not in used_j:
                    used_i.add(i)
                    used_j.add(j)
                    track[i] = active[j][0]
                    move = (lat_all[i] - active[j][2]) * k
                    heading = active[j][5] if abs(move) < BACK_M else int(np.sign(move))  # a wobble never flips it
                    active[j][1:] = [minute, lat_all[i], lon_all[i], at_b_all[i], heading]
            for i in g:
                if i not in used_i:
                    n += 1
                    track[i] = f"{svc}-{n}"
                    active.append([track[i], minute, lat_all[i], lon_all[i], at_b_all[i], 0])
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
    # Polling correction: a bus is only seen once a minute, so on average it really left the boarding stop half a
    # poll after its last sighting there and reached the alighting stop half a poll before its first sighting there.
    # Measured rides are therefore ~1 poll too long and measured waits ~1 poll too short; correct both by one poll.
    # (reached_a / left_a / reached_b stay the sighting times.)
    poll_min = POLL_SEC / 60
    df["wait_min"] = (df.left_a - df.reached_a).dt.total_seconds() / 60 + poll_min if len(df) else []
    df["ride_min"] = (df.reached_b - df.left_a).dt.total_seconds() / 60 - poll_min if len(df) else []
    return df


def find_departures(tracks, direction="jb-sg"):
    """Buses seen leaving the boarding stop: in each track, the last fix there before a fix on the Causeway (or at the
    alighting stop) within MAX_LOST_MIN. Unlike find_trips, the bus needn't be seen arriving: LTA drops a bus from
    the alighting stop's list once its ETA has passed, even mid-Causeway (44% of SG -> JB departures, 13 Oct).
    """
    rows = []
    for tid, g in tracks.sort_values("t").groupby("track"):
        left = None
        for r in g.itertuples():
            if r.at_a:
                left = r.t
            elif left is not None and (r.t - left).total_seconds() > MAX_LOST_MIN * 60:
                left = None
            elif left is not None and (r.at_b or CAUSEWAY_LAT[0] <= r.lat < CAUSEWAY_LAT[1]):
                rows.append({"service": r.service, "track": tid, "left_a": left})
                left = None
    d = to_frame(rows, ["service", "track", "left_a"])
    return d.assign(direction=direction, group=d.service.map(group_of),
                    service_day=(pd.to_datetime(d.left_a) - pd.Timedelta(hours=SERVICE_DAY_START_HOUR))
                    .dt.strftime("%Y-%m-%d"))


# ---------- 4. Gaps and flags ----------

def poll_gaps(poll_runs, direction="jb-sg"):
    """Stretches where consecutive successful polls of a stop of `direction` are more than GAP_SEC apart."""
    runs = to_frame(poll_runs, ["polled_at", "stop", "ok"])
    stops = [DIRECTIONS[direction]["board"], DIRECTIONS[direction]["alight"]]
    runs = runs[(runs.ok == 1) & runs.stop.isin(stops)].copy()
    runs["t"] = pd.to_datetime(runs.polled_at)
    rows = []
    for stop, g in runs.sort_values("t").groupby("stop"):
        for prev, curr in zip(g.t, g.t.iloc[1:]):
            if (curr - prev).total_seconds() > GAP_SEC:
                rows.append({"stop": stop, "gap_from": prev, "gap_to": curr})
    return to_frame(rows, ["stop", "gap_from", "gap_to"])


def check_trips(trips, gaps, direction="jb-sg"):
    """Add service_day and quality flags. A bad trip only drops itself, never the whole day."""
    t = trips.copy()
    if t.empty:
        return pd.DataFrame(columns=TRIP_COLS)
    t["direction"] = direction
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


def measure_trips(poll_runs, polls, services=SERVICES, direction="jb-sg"):
    """All trips found in `direction`, with flags. Use trips[trips.ok] for stats."""
    tracks = link_tracks(gps_points(polls, services, direction))
    return check_trips(find_trips(tracks), poll_gaps(poll_runs, direction), direction)


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
    """Buses seen leaving the berth per group, service-day and slot (from find_departures, or trips).

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
