"""Stage 3: measure trips from GPS tracks, in either direction (config.DIRECTIONS). Pure functions: data in, data out.

polls -> gps_points (one row per bus per minute) -> link_tracks (one track ~ one bus)
      -> find_trips (left the boarding stop -> reached the alighting stop) -> check_trips (flags) -> slot_stats
"""
# Big picture: LTA tells us where each GPS bus is once a minute, but not WHICH bus it is. This file:
#   1. gps_points:  keeps the position readings ("fixes") for one direction's two stops and notes, for each fix,
#                   whether the bus is in the boarding zone or the alighting zone;
#   2. link_tracks: joins each minute's fixes to the fixes of the minute before, so each bus gets one "track";
#   3. find_trips:  reads each track: last fix at the boarding stop = left, first fix at the alighting stop =
#                   arrived, so ride = arrived - left (minus 1 min, see the polling correction);
#      find_departures: same idea, but only "when did it leave" (used for the gap between buses);
#   4. check_trips: drops trips that can't be trusted (too short/long, polling gap, never seen crossing);
#   5. slot_stats:  median and spread of ride times per group, weekday and hour.
# Everything is a pure function (tables in, tables out), so tests/simulate.py can feed it made-up buses whose true
# times are known and check the answers.
import math

import numpy as np
import pandas as pd

from config import (BACK_M, BLIND_FLAG_MIN, CAUSEWAY_LAT, DIRECTIONS, EXTRAPOLATE, GAP_SEC, GROUPS, LINK_SLACK_M,
                    MAP_BAND_DEG, MAX_LINK_M, MIN_MAP_TRIPS, POLL_SEC, MAX_LOST_MIN, MAX_SPEED_MS, MAX_TRIP_MIN, MIN_TRIP_MIN, MIN_TRIPS_PER_SLOT, SERVICE_DAY_START_HOUR,
                    SERVICES, SLOT_MIN, STOP_COORDS)

DUP_M = 60  # the same bus in both stops' lists, polled a second apart, can differ by ~20 m
WOBBLE_M = 50  # GPS noise: a backward step up to this is treated as standing still
POINT_COLS = ["t", "minute", "service", "lat", "lon", "dist_a", "dist_b", "at_a", "at_b"]
TRIP_COLS = ["direction", "service", "group", "track", "service_day", "reached_a", "left_a", "reached_b", "wait_min", "ride_min",
             "blind_min", "n_between", "blind", "ok", "reason", "estimated"]


def metres(lat1, lon1, lat2, lon2):
    """Distance in metres. A flat-earth approximation is accurate to well under 1% at this scale."""
    k = 111_195  # metres per degree of latitude
    dy = (np.asarray(lat2) - lat1) * k
    dx = (np.asarray(lon2) - lon1) * k * np.cos(np.radians(lat1))  # a degree of longitude shrinks away from the equator
    return np.hypot(dx, dy)


def group_of(service):
    # "170X" -> "160/170/170X" (buses sharing a berth are analysed together)
    return next(g for g, services in GROUPS.items() if service in services)


def to_frame(rows, cols):
    # a DataFrame with the right columns even when there are no rows, so later steps don't crash on empty input
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
    # (and only rows with a position: a bus without GPS gives nothing to follow)
    df = df[df.stop.isin([stop_a, stop_b]) & df.service.isin(services) & df.lat.notna() & df.lon.notna()].copy()
    if df.empty:
        return pd.DataFrame(columns=POINT_COLS)
    df["t"] = pd.to_datetime(df.polled_at)
    df["minute"] = df.t.dt.floor("min")  # the two stops are polled a few seconds apart: treat them as one minute
    df = df.drop_duplicates(["minute", "service", "stop", "lat", "lon"])
    # A bus appears at most once per list, so match 46109 rows to 46219 rows one-to-one, closest first,
    # and drop the matched 46109 rows. Only pairs within DUP_M can match, so find those first (vectorised).
    # (A bus on the Causeway is "coming" to both stops, so LTA lists it at both: without this it would be 2 buses.)
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
    # Where is each fix? "at_a" = in the boarding zone, "at_b" = in the alighting zone (radius + latitude band,
    # see config.DIRECTIONS). A fix on the Causeway is in neither.
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
    # In plain words: walk through time minute by minute, one service at a time. For each new fix, list every
    # track it could belong to (close enough for a bus to have driven there), then hand out fixes best match first.
    # A fix nobody can claim starts a new track (a bus that just appeared).
    if points.empty:
        return points.assign(track=pd.Series(dtype=str))
    # plain arrays: row-by-row pandas access was ~90% of the run time
    svc_all = points.service.to_numpy()
    minute_all = points.minute.to_numpy()
    lat_all, lon_all = points.lat.to_numpy(float), points.lon.to_numpy(float)
    at_b_all = points.at_b.to_numpy(bool)
    track = np.empty(len(points), dtype=object)  # the answer: a track id for every fix
    k = 111_195
    for svc in sorted(set(svc_all)):  # a 170 can never turn into a 950, so each service is linked on its own
        rows = np.flatnonzero(svc_all == svc)
        rows = rows[np.argsort(minute_all[rows], kind="stable")]
        active = []  # [id, last minute, lat, lon, last fix at the alighting stop, heading: -1 south / +1 north / 0 unknown]
        n = 0
        # split this service's fixes into one group per minute
        bounds = np.flatnonzero(np.diff(minute_all[rows].astype("int64"))) + 1
        for g in np.split(rows, bounds):
            minute = minute_all[g[0]]
            # forget tracks not seen for too long (15 min; 1 min for a bus that already arrived)
            active = [a for a in active
                      if (minute - a[1]) / np.timedelta64(1, "s") <= (60 if a[4] else MAX_LOST_MIN * 60)]
            # 1) every possible (fix, track) pairing, with how good it is
            cand = []
            for i in g:
                lat, lon = lat_all[i], lon_all[i]
                for j, (_, last_t, la, lo, arrived, heading) in enumerate(active):
                    on_causeway = CAUSEWAY_LAT[0] <= la < CAUSEWAY_LAT[1]
                    ahead = heading * (lat - la) * k  # metres moved in the track's direction
                    if on_causeway and ahead < -BACK_M:  # would mean a U-turn on the bridge: not the same bus
                        continue
                    back = on_causeway and ahead < -WOBBLE_M
                    gap = (minute - last_t) / np.timedelta64(1, "s")
                    d = math.hypot((lon - lo) * k * math.cos(math.radians(la)), (lat - la) * k)  # = metres()
                    reach = DUP_M if arrived else min(MAX_SPEED_MS * gap + LINK_SLACK_M, MAX_LINK_M)
                    if d <= reach:  # close enough for the bus to have driven there
                        # rank 0 = seen last minute (best), 1 = just arrived, 2 = lost for a while (last resort)
                        rank = 1 if arrived else (0 if gap <= 60 else 2)
                        cand.append((rank, back, d, i, j))
            # 2) hand out pairings best first (rank, then no backward step, then shortest distance);
            #    each fix and each track can be used once
            used_i, used_j = set(), set()
            for _, _, d, i, j in sorted(cand):
                if i not in used_i and j not in used_j:
                    used_i.add(i)
                    used_j.add(j)
                    track[i] = active[j][0]
                    move = (lat_all[i] - active[j][2]) * k
                    heading = active[j][5] if abs(move) < BACK_M else int(np.sign(move))  # a wobble never flips it
                    active[j][1:] = [minute, lat_all[i], lon_all[i], at_b_all[i], heading]
            # 3) fixes no track could claim start a new track
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
    # Walk along each track in time order, remembering when the bus was last seen at the boarding stop (left_a).
    # The first time it then shows up at the alighting stop, that's one trip.
    rows = []
    for tid, g in tracks.sort_values("t").groupby("track"):
        reached_a = left_a = None
        times = g.t.tolist()
        for k, r in enumerate(g.itertuples()):
            if r.at_a:
                if left_a is None or (r.t - left_a).total_seconds() > MAX_LOST_MIN * 60:
                    reached_a = r.t  # first fix of this visit to the boarding stop
                left_a = r.t  # keeps moving forward while the bus waits; the last value = when it left
            elif r.at_b and left_a is not None:
                ride = [x for x in times[:k + 1] if x >= left_a]  # the fixes from leaving to arriving
                # longest stretch without a fix during the ride (a long one makes the time less precise)
                blind = max(np.diff(ride)).total_seconds() / 60 if len(ride) > 1 else 0.0
                rows.append({"service": r.service, "track": tid, "reached_a": reached_a, "left_a": left_a,
                             "reached_b": r.t, "blind_min": blind, "n_between": len(ride) - 2})
                reached_a = left_a = None  # ready for the bus's next trip
    return timed(to_frame(rows, ["service", "track", "reached_a", "left_a", "reached_b", "blind_min", "n_between"]))


def timed(df):
    """Add wait_min and ride_min to trips with reached_a / left_a / reached_b sighting times."""
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
    # Same walk as find_trips, but it stops as soon as the bus is seen on the Causeway: that's enough to know it left.
    # These departure times feed the "A bus every N min" figures (dashboard.bus_gaps).
    rows = []
    for tid, g in tracks.sort_values("t").groupby("track"):
        left = None
        for r in g.itertuples():
            if r.at_a:
                left = r.t
            elif left is not None and (r.t - left).total_seconds() > MAX_LOST_MIN * 60:
                left = None  # seen again too long after: don't trust that it's the same departure
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
    # e.g. the VM was down from 22 Oct 01:00 to 25 Oct 21:40: one gap per stop. A trip overlapping a gap may have
    # had its arrival (or the bus in between) missed, so check_trips drops it.
    runs = to_frame(poll_runs, ["polled_at", "stop", "ok"])
    stops = [DIRECTIONS[direction]["board"], DIRECTIONS[direction]["alight"]]
    runs = runs[(runs.ok == 1) & runs.stop.isin(stops)].copy()
    runs["t"] = pd.to_datetime(runs.polled_at)
    rows = []
    for stop, g in runs.sort_values("t").groupby("stop"):
        for prev, curr in zip(g.t, g.t.iloc[1:]):  # each successful poll and the next one
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
    # the three reasons to drop a trip:
    in_gap = [any((gaps.gap_from < r.reached_b) & (gaps.gap_to > r.left_a)) for r in t.itertuples()]
    implausible = (t.ride_min < MIN_TRIP_MIN) | (t.ride_min > MAX_TRIP_MIN)
    # no fix between leaving 46219 and reaching 46109: the track jumped from one bus to another
    unseen = t.n_between < 1
    # the first reason that applies is recorded; "" = a good trip
    t["reason"] = np.where(implausible, "implausible", np.where(in_gap, "polling gap",
                           np.where(unseen, "not seen crossing", "")))
    t["ok"] = t.reason == ""
    # True = arrival estimated by add_lost (the bus was dropped from LTA's list before it arrived)
    t["estimated"] = t.estimated.astype(bool) if "estimated" in t else False
    return t[TRIP_COLS].sort_values(["service", "left_a"]).reset_index(drop=True)


def on_causeway(lat):
    """True for latitudes on the Causeway (same half-open band link_tracks and find_departures use)."""
    return (CAUSEWAY_LAT[0] <= lat) & (lat < CAUSEWAY_LAT[1])


def band(lat):
    """Speed-map band of a latitude, as an integer key (~110 m wide; integers avoid float-rounding mismatches)."""
    return int(round(lat / MAP_BAND_DEG))


def add_lost(tracks, trips, gaps, direction="jb-sg"):
    """Add trips for buses that left and were seen on the Causeway but dropped off LTA's list before arriving.

    Their arrival = last Causeway fix + the median minutes that complete trips (ok) still needed from the same
    latitude band in the same hour (the "speed map"). Only for directions in EXTRAPOLATE, and only where the speed
    map has MIN_MAP_TRIPS complete trips for that band and hour; other lost buses stay lost. The new rows have
    estimated = True and go through check_trips like any trip (polling gaps, implausible times).
    """
    # Why: SG -> JB loses ~44% of departures this way, and leaving them out biased SG -> JB rides low.
    # Validated offline before building (complete trips cut mid-Causeway, see config.EXTRAPOLATE).
    if direction not in EXTRAPOLATE or trips.empty or not trips.ok.astype(bool).any():
        return trips
    by_track = {k: g for k, g in tracks.sort_values("t").groupby("track")}

    # 1) Speed map from complete trips: {(band, hour the bus left): median minutes still to go to the first sighting
    #    at the alighting stop}. Counted in distinct trips, so one bus standing in a jam can't fill a band alone.
    rows = []
    for tr in trips[trips.ok.astype(bool)].itertuples():
        g = by_track[tr.track]
        on = g[(g.t > tr.left_a) & (g.t < tr.reached_b) & on_causeway(g.lat)]
        rows += [(tr.Index, band(lat), tr.left_a.hour, (tr.reached_b - t).total_seconds() / 60)
                 for t, lat in zip(on.t, on.lat)]
    sm = (pd.DataFrame(rows, columns=["trip", "band", "hour", "to_go"])
          .groupby(["band", "hour"]).agg(to_go=("to_go", "median"), n=("trip", "nunique")))
    speed_map = sm[sm.n >= MIN_MAP_TRIPS].to_go.to_dict()

    # 2) Lost buses: the same walk as find_trips, but remember the Causeway fixes after leaving. A track that ends
    #    with Causeway fixes and no fix at the alighting stop is a lost bus. (Tracks have no gaps over MAX_LOST_MIN:
    #    link_tracks ends a track then, so "the track ended" = "LTA stopped listing the bus", except at the end of the
    #    data, see below.)
    # A track whose last fix is within MAX_LOST_MIN of the end of the data may still be going (e.g. a bus on the
    # Causeway as the dashboard is built): not lost yet, so no guess; the next build sees how it ends.
    data_end = tracks.t.max()
    est = []
    for tid, g in by_track.items():
        if (data_end - g.t.iloc[-1]).total_seconds() <= MAX_LOST_MIN * 60:
            continue
        reached_a = left_a = None
        seen = []  # Causeway fixes since the bus left the boarding stop
        for r in g.itertuples():
            if r.at_a:  # at (or back at) the boarding stop: same rules as find_trips
                if left_a is None or (r.t - left_a).total_seconds() > MAX_LOST_MIN * 60:
                    reached_a = r.t
                left_a, seen = r.t, []
            elif r.at_b:  # arrived: find_trips already has this trip
                reached_a, left_a, seen = None, None, []
            elif left_a is not None and on_causeway(r.lat):
                seen.append(r)
        if not seen:
            continue
        last = seen[-1]
        to_go = speed_map.get((band(last.lat), left_a.hour))
        if to_go is None:  # nobody (or too few) went through this spot in this hour: no basis for a guess
            continue
        times = [left_a] + [s.t for s in seen]
        est.append({"service": last.service, "track": tid, "reached_a": reached_a, "left_a": left_a,
                    "reached_b": last.t + pd.Timedelta(minutes=to_go),
                    # blind_min covers only the part we saw; the extrapolated stretch is flagged by `estimated`
                    "blind_min": max(np.diff(times)).total_seconds() / 60, "n_between": len(seen)})
    if not est:
        return trips
    extra = check_trips(timed(pd.DataFrame(est)).assign(estimated=True), gaps, direction)
    return pd.concat([trips, extra], ignore_index=True).sort_values(["service", "left_a"]).reset_index(drop=True)


def measure_trips(poll_runs, polls, services=SERVICES, direction="jb-sg"):
    """All trips found in `direction`, with flags. Use trips[trips.ok] for stats."""
    # the whole pipeline in one call: fixes -> tracks -> trips -> flags -> estimated trips for lost buses
    tracks = link_tracks(gps_points(polls, services, direction))
    gaps = poll_gaps(poll_runs, direction)
    return add_lost(tracks, check_trips(find_trips(tracks), gaps, direction), gaps, direction)


# ---------- 5. Stats ----------

def slot_stats(trips, by=("group", "weekday", "slot"), slot_min=SLOT_MIN):
    """n, median, P25, P75 of ride minutes per group. Slot = time left 46219, rounded down. Thin slots hidden."""
    # e.g. all good 160/170/170X rides that left on a Saturday between 18:00 and 19:00 -> one row:
    # n (how many), median (the middle one), p25-p75 (the middle half, shown as "usually a-b min")
    t = trips[trips.ok.astype(bool)].copy() if "ok" in trips else trips.copy()
    # est = how many of the n had an estimated arrival (add_lost); days = how many service days they came from
    cols = [*by, "n", "median", "p25", "p75", "est", "days"]
    if t.empty:
        return pd.DataFrame(columns=cols)
    t["est"] = t.estimated.astype(bool) if "estimated" in t else False
    t["group"] = t.service.map(group_of)
    t["weekday"] = pd.to_datetime(t.service_day).dt.strftime("%a")
    t["slot"] = pd.to_datetime(t.left_a).dt.floor(f"{slot_min}min").dt.strftime("%H:%M")
    s = t.groupby(list(by)).agg(n=("ride_min", "size"), median=("ride_min", "median"),
                                p25=("ride_min", lambda x: x.quantile(.25)), p75=("ride_min", lambda x: x.quantile(.75)),
                                est=("est", "sum"), days=("service_day", "nunique")).reset_index()
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
