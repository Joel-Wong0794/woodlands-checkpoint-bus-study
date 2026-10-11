"""Tests for trips.py (GPS-track method). Run from the project root: python -m pytest"""
import pandas as pd
import pytest

from tests.simulate import random_buses, simulate
from trips import day_quality, departures_seen, gps_points, link_tracks, measure_trips, poll_gaps, slot_stats

DAY = "2026-10-09"


def bus(at_a, left_a, at_b, service="170", **kw):
    return {"service": service, "at_a": f"{DAY} {at_a}", "left_a": f"{DAY} {left_a}", "at_b": f"{DAY} {at_b}", **kw}


def run(buses, start="05:30", end="23:00", **kw):
    runs, polls = simulate(buses, f"{DAY} {start}", f"{DAY} {end}", **kw)
    return measure_trips(runs, polls, services=("170", "950"))


def matches(trip, b, tol_min=1.5):
    """Recovered trip ~ true bus: same service, left 46219 and reached 46109 within tol_min."""
    def close(x, y):
        return abs((pd.Timestamp(x) - pd.Timestamp(y)).total_seconds()) <= tol_min * 60
    return trip.service == b["service"] and close(trip.left_a, b["left_a"]) and close(trip.reached_b, b["at_b"])


def assert_recovered(trips, buses):
    """Every true bus recovered, one-to-one, in departure order."""
    ok = trips[trips.ok].sort_values("left_a")
    assert len(ok) == len(buses)
    for t, b in zip(ok.itertuples(), sorted(buses, key=lambda b: pd.Timestamp(b["left_a"]))):
        assert matches(t, b), (t, b)


# ---------- simulated ----------

def test_round_trip_clean():
    # at most 3 buses on the bridge at once, so the 3-bus lists always show every bus
    buses = random_buses(40, headway=(8, 15), wait=(0, 5), ride=(8, 22), seed=1)
    trips = run(buses)
    assert_recovered(trips, buses)
    for t, b in zip(trips.sort_values("left_a").itertuples(), buses):
        true_ride = (pd.Timestamp(b["at_b"]) - pd.Timestamp(b["left_a"])).total_seconds() / 60
        assert abs(t.ride_min - true_ride) <= 2  # each end is only known to the minute


def test_round_trip_with_gps_noise():
    buses = random_buses(20, seed=2)
    assert_recovered(run(buses, noise_m=20), buses)


def check_jam(headway, seed):
    """A morning jam: 30-40 min rides. 46109 lists only the next 3 buses, so with more buses than that on the
    bridge the newest ones are invisible for a while. Some buses are missed; the ride times found must stay right."""
    buses = random_buses(60, headway=headway, ride=(30, 40), seed=seed)
    ok = run(buses, end="23:59")
    ok = ok[ok.ok]
    true = pd.Series([(pd.Timestamp(b["at_b"]) - pd.Timestamp(b["left_a"])).total_seconds() / 60 for b in buses])
    assert len(ok) >= 0.7 * len(buses)
    assert abs(ok.ride_min.median() - true.median()) <= 3
    assert ok.ride_min.between(25, 45).all()


@pytest.mark.parametrize("seed", [3, 4, 5])
def test_jam_about_3_buses_on_bridge(seed):
    """Like the real 170 in the morning peak (9 Oct: 30-40 min rides, a bus every ~8-15 min)."""
    check_jam((8, 15), seed)


@pytest.mark.xfail(strict=True, reason="Known limit: with 4+ buses on the bridge, tracks swap to the bus ahead and "
                                       "rides come out several minutes short. Not seen in the real 170 data so far.")
def test_jam_4_plus_buses_on_bridge():
    check_jam((4, 8), 3)


def test_wait_at_46219_is_separate_from_ride():
    trips = run([bus("10:12", "10:34", "10:45")])
    t = trips.iloc[0]
    assert abs(t.ride_min - 11) <= 1.5
    assert abs(t.wait_min - 22) <= 2.5  # the approach zone adds up to ~2 min


def test_loop_bus_from_singapore_counts_once():
    """A 950 passes 46109 on its way to 46219 first: only the 46219 -> 46109 leg is a trip."""
    b = bus("10:12", "10:34", "10:45", service="950", via_b=True)
    trips = run([b])
    assert len(trips) == 1 and matches(trips.iloc[0], b)


def test_flicker_mid_crossing_still_one_trip():
    b = bus("10:00", "10:02", "10:20")
    trips = run([b], flicker=[(0, f"{DAY} 10:08", f"{DAY} 10:14")])
    assert len(trips) == 1 and matches(trips.iloc[0], b)
    assert trips.iloc[0].blind_min >= 6 and trips.iloc[0].blind


def test_bunching_gives_two_trips():
    buses = [bus("10:00", "10:03", "10:20"), bus("10:00:30", "10:03:30", "10:20:30")]
    assert_recovered(run(buses), buses)


def test_no_jump_back_to_a_bus_switching_gps_on():
    """Real 170X pattern: bus X drops out of the lists mid-Causeway; bus W turns its GPS on 2 min after leaving JB,
    ~340 m behind X's last fix. X's lost track must not grab W (the 'bus' would move back north), which would
    give a false 26-min trip from X's departure to W's arrival. Neither bus is seen end to end: no trip at all."""
    x = bus("10:00", "10:02", "10:20")
    w = bus("10:09", "10:10", "10:28")
    flicker = [(0, f"{DAY} 10:08", f"{DAY} 10:25"), (1, f"{DAY} 09:00", f"{DAY} 10:11:59")]
    trips = run([x, w], flicker=flicker)
    assert trips.ok.sum() == 0, trips[["left_a", "reached_b", "ride_min"]]


def test_northbound_bus_can_turn_at_jb():
    """A loop bus coming north from Singapore turns round at the JB checkpoint: that reversal is allowed.
    (Its stop at 46109 on the way north ends a track by design, so start after it has left 46109.)"""
    b = bus("10:12", "10:34", "10:45", service="950", via_b=True)
    polls = simulate([b], f"{DAY} 10:02", f"{DAY} 11:00")[1]
    tracks = link_tracks(gps_points(polls, ("950",)))
    assert tracks.track.nunique() == 1 and tracks.lat.max() > 1.465


def test_no_gps_no_trip():
    assert len(run([bus("10:00", "10:03", "10:20", gps=False)])) == 0


def test_polling_gap_drops_only_that_trip():
    buses = [bus("08:00", "08:02", "08:20"), bus("10:00", "10:02", "10:20")]
    missing = [f"{DAY} 10:{m:02d}" for m in range(5, 10)]  # 5-min gap during the second ride
    trips = run(buses, missing=missing)
    assert trips.ok.sum() == 1 and matches(trips[trips.ok].iloc[0], buses[0])
    assert trips[~trips.ok].iloc[0].reason == "polling gap"


def test_poll_gaps():
    runs = [{"polled_at": f"{DAY} 10:0{m}:01", "stop": "46219", "ok": 1} for m in (0, 1, 5, 6)]
    runs.append({"polled_at": f"{DAY} 10:02:01", "stop": "46219", "ok": 0})  # a failed call doesn't fill a gap
    gaps = poll_gaps(runs)
    assert len(gaps) == 1
    assert str(gaps.iloc[0].gap_from) == f"{DAY} 10:01:01" and str(gaps.iloc[0].gap_to) == f"{DAY} 10:05:01"


def test_implausible_trip_dropped():
    trips = run([bus("10:00", "10:02", "10:04")])  # a 2-minute crossing
    assert trips.ok.sum() == 0 and trips.iloc[0].reason == "implausible"


def test_service_day_boundary():
    trips = run([bus("00:30", "00:40", "01:05")], start="00:00", end="02:00")
    assert trips.iloc[0].service_day == "2026-10-08"  # before 03:00 belongs to the previous day


def test_same_bus_in_both_lists_is_one_point():
    polls = [{"polled_at": f"{DAY} 10:00:01", "stop": "46219", "service": "170", "seq": 1, "lat": 1.4600, "lon": 103.7660},
             {"polled_at": f"{DAY} 10:00:02", "stop": "46109", "service": "170", "seq": 2, "lat": 1.4601, "lon": 103.7661},
             {"polled_at": f"{DAY} 10:00:01", "stop": "46219", "service": "170", "seq": 2, "lat": 1.4560, "lon": 103.7680}]
    assert len(gps_points(polls, ("170",))) == 2  # the first two are one bus ~15 m apart


def test_link_tracks_follows_two_buses():
    polls = []
    for m in range(5):  # two buses 600 m apart, both moving south 200 m a minute
        for seq, lat0 in ((1, 1.4560), (2, 1.4614)):
            polls.append({"polled_at": f"{DAY} 10:0{m}:01", "stop": "46219", "service": "170", "seq": seq,
                          "lat": lat0 - m * 0.0018, "lon": 103.7680})
    tracks = link_tracks(gps_points(polls, ("170",)))
    assert tracks.track.nunique() == 2
    assert all(g.lat.is_monotonic_decreasing for _, g in tracks.groupby("track"))


def test_day_quality():
    buses = [bus("08:00", "08:02", "08:20"), bus("10:00", "10:02", "10:20"), bus("12:00", "12:02", "12:04")]
    runs, polls = simulate(buses, f"{DAY} 05:30", f"{DAY} 23:00", missing=[f"{DAY} 10:{m:02d}" for m in range(5, 10)])
    trips = measure_trips(runs, polls, services=("170",))
    q = day_quality(trips, poll_gaps(runs)).iloc[0]
    assert (q.service, q.service_day, q.n_trips, q.n_dropped, q.n_gaps) == ("170", DAY, 1, 2, 2)  # a gap per stop
    runs, polls = simulate(buses[:1], f"{DAY} 05:30", f"{DAY} 23:00")  # no gaps at all
    q = day_quality(measure_trips(runs, polls, services=("170",)), poll_gaps(runs)).iloc[0]
    assert (q.n_trips, q.n_gaps) == (1, 0)


def test_empty_inputs():
    trips = measure_trips([], [], services=("170",))
    assert trips.empty and "ride_min" in trips.columns


# ---------- real rows ----------

# Real 950 GPS rows, Fri 9 Oct 2026 10:04-10:49 (VM database): time, stop list, seq, lat, lon.
# Three 950s wait in the JB checkpoint bus area. Hand-checked against the raw rows: two leave at 10:34 and
# 10:35 and reach 46109 at 10:44 and 10:45 (which bus is which is ambiguous: one may overtake the other);
# the third leaves ~10:40 and is still on the Causeway at 10:49.
REAL_950 = """
10:04:01 46219 1 1.445825 103.768327
10:05:01 46219 1 1.446230 103.768497
10:06:01 46219 1 1.446230 103.768497
10:07:01 46219 1 1.449701 103.770033
10:08:01 46219 1 1.456993 103.767619
10:09:01 46219 1 1.462956 103.765233
10:10:01 46219 1 1.464704 103.765524
10:11:01 46219 1 1.464912 103.765916
10:12:01 46219 1 1.465430 103.767288
10:13:01 46219 1 1.465430 103.767288
10:14:01 46219 1 1.465430 103.767288
10:15:01 46219 1 1.465430 103.767288
10:16:02 46219 1 1.465430 103.767288
10:17:01 46219 1 1.465430 103.767288
10:18:02 46219 1 1.465430 103.767288
10:19:01 46219 1 1.465430 103.767288
10:20:01 46219 1 1.465430 103.767288
10:21:01 46219 1 1.465430 103.767288
10:22:01 46219 1 1.465430 103.767288
10:22:01 46219 2 1.445794 103.768405
10:23:01 46219 1 1.465430 103.767288
10:23:01 46219 2 1.446175 103.768627
10:24:02 46219 1 1.465479 103.767328
10:24:02 46219 2 1.465262 103.766743
10:24:02 46219 3 1.446271 103.768701
10:25:01 46219 1 1.465524 103.767624
10:25:01 46219 2 1.465308 103.767076
10:25:01 46219 3 1.446860 103.768903
10:26:01 46219 1 1.465499 103.767250
10:26:01 46219 2 1.465155 103.767357
10:26:01 46219 3 1.448402 103.769779
10:27:02 46219 1 1.465572 103.766484
10:27:02 46219 2 1.465187 103.767563
10:27:02 46219 3 1.454389 103.768565
10:28:02 46219 1 1.465739 103.766176
10:28:02 46219 2 1.465224 103.767417
10:28:02 46219 3 1.457438 103.767593
10:29:01 46219 1 1.465739 103.766176
10:29:01 46219 2 1.465407 103.767530
10:29:01 46219 3 1.464721 103.765652
10:30:01 46219 1 1.465730 103.765905
10:30:01 46219 2 1.465287 103.766961
10:30:01 46219 3 1.464797 103.765815
10:31:01 46219 1 1.465918 103.765349
10:31:01 46219 2 1.465287 103.766961
10:31:01 46219 3 1.464797 103.765815
10:32:01 46219 1 1.465870 103.765303
10:32:01 46219 2 1.465721 103.767718
10:32:01 46219 3 1.465269 103.767074
10:33:01 46219 1 1.465503 103.765174
10:33:01 46219 2 1.466403 103.766243
10:33:01 46219 3 1.465269 103.767074
10:34:01 46219 1 1.466407 103.766172
10:34:01 46219 2 1.465269 103.767074
10:35:01 46219 1 1.463109 103.765338
10:35:01 46219 2 1.465480 103.764313
10:35:01 46219 3 1.465269 103.767074
10:36:01 46219 1 1.461927 103.766080
10:36:01 46219 2 1.463005 103.765312
10:36:01 46219 3 1.465269 103.767074
10:36:02 46109 1 1.462378 103.765849
10:37:01 46219 1 1.461735 103.766178
10:37:01 46219 2 1.462698 103.765618
10:37:01 46219 3 1.465329 103.767123
10:37:02 46109 1 1.461735 103.766178
10:37:02 46109 2 1.462698 103.765618
10:38:01 46219 1 1.461043 103.766481
10:38:01 46219 2 1.462084 103.765965
10:38:01 46219 3 1.465270 103.766591
10:38:02 46109 1 1.461043 103.766481
10:38:02 46109 2 1.462084 103.765965
10:39:01 46219 1 1.459762 103.767227
10:39:01 46219 2 1.461263 103.766362
10:39:01 46219 3 1.465314 103.766150
10:39:02 46109 1 1.459762 103.767227
10:39:02 46109 2 1.461263 103.766362
10:40:01 46219 1 1.459204 103.767454
10:40:01 46219 2 1.456104 103.768104
10:40:01 46219 3 1.465253 103.766072
10:40:02 46109 1 1.459204 103.767454
10:40:02 46109 2 1.456104 103.768104
10:41:01 46219 1 1.457194 103.767617
10:41:01 46219 2 1.455177 103.768485
10:41:01 46219 3 1.462450 103.765808
10:41:02 46109 1 1.455382 103.768423
10:41:02 46109 2 1.457194 103.767617
10:41:02 46109 3 1.462600 103.765690
10:42:01 46219 1 1.456046 103.768072
10:42:01 46219 2 1.461557 103.766326
10:42:02 46109 1 1.456046 103.768072
10:42:02 46109 2 1.461557 103.766326
10:43:01 46219 1 1.448856 103.770230
10:43:01 46219 2 1.461313 103.766411
10:43:02 46109 1 1.448856 103.770230
10:43:02 46109 2 1.461313 103.766411
10:44:02 46109 1 1.448899 103.770184
10:44:02 46109 2 1.460718 103.766650
10:44:02 46219 1 1.447325 103.769330
10:44:02 46219 2 1.448899 103.770184
10:44:02 46219 3 1.460718 103.766650
10:45:01 46109 1 1.447646 103.769580
10:45:01 46109 2 1.459701 103.767278
10:45:01 46219 1 1.447083 103.769238
10:45:01 46219 2 1.447507 103.769454
10:45:01 46219 3 1.459701 103.767278
10:46:01 46109 1 1.447421 103.769377
10:46:01 46109 2 1.458991 103.767633
10:46:01 46219 1 1.447008 103.769218
10:46:01 46219 2 1.447421 103.769377
10:46:01 46219 3 1.457227 103.767744
10:47:01 46219 1 1.447276 103.769277
10:48:01 46219 1 1.449254 103.770188
10:48:02 46109 1 1.449254 103.770188
10:49:02 46109 1 1.447976 103.769830
10:49:02 46219 1 1.447976 103.769830
"""


def real_950():
    polls = []
    for line in REAL_950.strip().splitlines():
        t, stop, seq, lat, lon = line.split()
        polls.append({"polled_at": f"{DAY} {t}", "stop": stop, "service": "950", "seq": int(seq),
                      "lat": float(lat), "lon": float(lon)})
    runs = [{"polled_at": f"{DAY} 10:{m:02d}:01", "stop": s, "ok": 1} for m in range(0, 51) for s in ("46219", "46109")]
    return runs, polls


def test_real_950_bunch():
    trips = measure_trips(*real_950(), services=("950",))
    ok = trips[trips.ok].sort_values("left_a")
    assert len(ok) == 2

    def mins(series, hhmm):  # each time within 1 min of the hand-checked one
        return all(abs((t - pd.Timestamp(f"{DAY} {h}")).total_seconds()) <= 60 for t, h in zip(series, hhmm))
    assert mins(ok.left_a, ["10:34", "10:35"])
    assert mins(sorted(ok.reached_b), ["10:44", "10:45"])
    assert ok.ride_min.between(8, 13).all()


# ---------- slot_stats ----------

def test_slot_stats():
    rows = []
    # Fri 9 Oct, 07:xx: rides 10, 20, 30, 40 -> median 25, P25 17.5, P75 32.5
    for i, ride in enumerate((10, 20, 30, 40)):
        rows.append({"service": "170", "service_day": DAY, "left_a": pd.Timestamp(f"{DAY} 07:{10 + i * 10}"),
                     "ride_min": ride, "ok": True})
    rows.append({"service": "170", "service_day": DAY, "left_a": pd.Timestamp(f"{DAY} 07:55"), "ride_min": 99, "ok": False})
    # Sat 10 Oct 01:00 belongs to service day Fri 9 Oct; only 2 trips -> hidden
    for ride in (5, 6):
        rows.append({"service": "170", "service_day": DAY, "left_a": pd.Timestamp("2026-10-10 01:20"),
                     "ride_min": ride, "ok": True})
    s = slot_stats(pd.DataFrame(rows))
    assert list(s.columns) == ["group", "weekday", "slot", "n", "median", "p25", "p75"]
    assert len(s) == 1
    r = s.iloc[0]
    assert (r.group, r.weekday, r.slot, r.n, r["median"], r.p25, r.p75) == ("160/170/170X", "Fri", "07:00", 4, 25, 17.5, 32.5)
    assert slot_stats(pd.DataFrame(rows), by=("slot",), slot_min=30).empty  # 2 + 2 trips: both slots too thin


def test_slot_stats_pools_a_group_not_950():
    """160, 170 and 170X leave from the same berth, so they pool into one group; 950 stays separate."""
    rows = [{"service": svc, "service_day": DAY, "left_a": pd.Timestamp(f"{DAY} 09:05"), "ride_min": r, "ok": True}
            for svc, r in (("160", 10), ("170", 12), ("170X", 14), ("950", 30), ("950", 31), ("950", 32))]
    s = slot_stats(pd.DataFrame(rows)).set_index("group")
    assert s.loc["160/170/170X", "n"] == 3 and s.loc["160/170/170X", "median"] == 12
    assert s.loc["950", "n"] == 3 and s.loc["950", "median"] == 31


def test_measure_trips_adds_group():
    trips = run([bus("10:00", "10:02", "10:20", service="950"), bus("11:00", "11:02", "11:20")])
    assert sorted(trips.group) == ["160/170/170X", "950"]


def test_departures_seen():
    rows = [{"service": svc, "service_day": DAY, "left_a": pd.Timestamp(f"{DAY} {hhmm}"), "ok": ok}
            for svc, hhmm, ok in (("160", "09:05", True), ("170X", "09:40", True), ("170", "09:59", False),
                                  ("950", "09:10", True), ("170", "10:01", True))]
    d = departures_seen(pd.DataFrame(rows)).set_index(["group", "slot"])
    assert d.loc[("160/170/170X", "09:00"), "departures"] == 3  # a dropped trip still left the berth
    assert d.loc[("160/170/170X", "10:00"), "departures"] == 1
    assert d.loc[("950", "09:00"), "departures"] == 1
    assert list(departures_seen(pd.DataFrame(rows)).columns) == ["group", "service_day", "slot", "departures"]


def test_slot_stats_empty():
    assert slot_stats(pd.DataFrame(columns=["service", "service_day", "left_a", "ride_min", "ok"])).empty


@pytest.mark.parametrize("weekday_day, name", [("2026-10-12", "Mon"), ("2026-10-11", "Sun")])
def test_slot_stats_weekday_names(weekday_day, name):
    rows = [{"service": "170", "service_day": weekday_day, "left_a": pd.Timestamp(f"{weekday_day} 09:05"),
             "ride_min": r, "ok": True} for r in (10, 11, 12)]
    assert slot_stats(pd.DataFrame(rows)).weekday.tolist() == [name]
