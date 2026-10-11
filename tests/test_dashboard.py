"""Tests for dashboard.py. Run from the project root: python -m pytest"""
from datetime import datetime

import pandas as pd

import dashboard as D
from collector import open_db
from tests.simulate import random_buses, simulate
from trips import gps_points, link_tracks

DAY = "2026-10-09"  # a Friday


def bus(at_a, left_a, at_b, service="170"):
    return {"service": service, "at_a": f"{DAY} {at_a}", "left_a": f"{DAY} {left_a}", "at_b": f"{DAY} {at_b}"}


def fill_db(path, buses, start, end):
    runs, polls = simulate(buses, start, end)
    con = open_db(path)
    with con:
        con.executemany("INSERT INTO poll_runs VALUES (:polled_at, :stop, :ok, :n_services, :error)", runs)
        con.executemany("INSERT INTO polls VALUES (:polled_at, :stop, :service, :seq, :eta, :monitored,"
                        " :lat, :lon, :load, :bus_type, :origin, :destination)", polls)
    con.close()


def test_smoke(tmp_path):
    db = tmp_path / "causeway.db"
    buses = (random_buses(40, service="170", first="2026-10-08 06:00", headway=(20, 40), seed=1)
             + random_buses(25, service="950", first="2026-10-08 06:00", headway=(40, 70), seed=2))
    fill_db(db, buses, "2026-10-08 05:30", f"{DAY} 18:00")
    html = D.build(db, now=datetime.fromisoformat(f"{DAY} 18:00:30"))
    for text in (D.PROFILE_TITLE, D.HEATMAP_TITLE, "Now", "Typical", "On the bridge now",
                 "Bus 160 / 170 / 170X", "Bus 950", "Best and worst times", "Data quality", D.WAIT_TITLE):
        assert text in html, text
    assert "No data yet" not in html


def test_empty_db(tmp_path):
    db = tmp_path / "causeway.db"
    open_db(db).close()
    html = D.build(db, now=datetime.fromisoformat(f"{DAY} 12:00"))
    assert "No data yet" in html


def test_on_bridge_now():
    # 5 buses left 46219; by 10:30 three have reached 46109 and two are on the Causeway
    buses = [bus("09:50", "09:52", "10:05"), bus("09:55", "09:58", "10:12"), bus("10:00", "10:03", "10:20"),
             bus("10:10", "10:14", "10:40"), bus("10:18", "10:22", "10:50")]
    _, polls = simulate(buses, f"{DAY} 09:30", f"{DAY} 10:30")
    now = datetime.fromisoformat(f"{DAY} 10:30:30")
    b = D.on_bridge(link_tracks(gps_points(polls, ("170",))), now)
    assert len(b) == 2
    # now - left 46219 (10:22, 10:14) = 8.5 and 16.5 min; the last fix at the checkpoint is known to ~1 min
    assert all(abs(x - true) <= 1.5 for x, true in zip(sorted(b.elapsed_min), [8.5, 16.5]))
    assert set(b.group) == {"160/170/170X"}


def test_on_bridge_ignores_stale_tracks():
    # a bus last seen on the Causeway 20 min ago (dropped off the lists) is not "on the bridge now"
    _, polls = simulate([bus("10:00", "10:02", "10:40")], f"{DAY} 09:50", f"{DAY} 10:10")
    b = D.on_bridge(link_tracks(gps_points(polls, ("170",))), datetime.fromisoformat(f"{DAY} 10:30"))
    assert b.empty


def test_on_bridge_ignores_fixes_after_now():
    # rebuilding for an earlier time: a bus that only leaves later must not count
    _, polls = simulate([bus("10:00", "10:02", "10:20"), bus("10:30", "10:32", "10:50")], f"{DAY} 09:50", f"{DAY} 11:00")
    b = D.on_bridge(link_tracks(gps_points(polls, ("170",))), datetime.fromisoformat(f"{DAY} 10:10"))
    assert len(b) == 1 and 7 <= b.elapsed_min.iloc[0] <= 9.5


def test_best_worst():
    stats = pd.DataFrame({"group": "160/170/170X", "day_type": "weekday",
                          "slot": [f"{h:02d}:00" for h in range(5, 24)], "n": 5,
                          "median": [float(h) for h in range(5, 24)]})
    t = D.best_worst(stats, "160/170/170X")
    wk = t[t.day_type == "weekday"]
    assert wk[wk.kind == "best"].slot.tolist() == ["06:00", "07:00", "08:00"]  # 05:00 is outside 06-23
    assert wk[wk.kind == "worst"].slot.tolist() == ["22:00", "21:00", "20:00"]
    few = D.best_worst(stats[stats.slot.isin(["08:00", "09:00", "10:00"])], "160/170/170X")
    assert few[few.kind == "best"].slot.tolist() == ["08:00"] and few[few.kind == "worst"].slot.tolist() == ["10:00"]


def test_berth_wait_uses_only_services_with_gps_while_waiting():
    # 170 has GPS while it waits at 46219; 160/170X only switch it on when they leave, so their "wait" is ignored
    rows = [{"service": svc, "service_day": DAY, "left_a": pd.Timestamp(f"{DAY} 17:{10 + i}"), "wait_min": w,
             "ride_min": 10, "ok": True, "day_type": "weekday"}
            for i, (svc, w) in enumerate((("170", 20), ("170", 22), ("170", 24), ("170X", 1), ("160", 2)))]
    s = D.berth_wait(pd.DataFrame(rows))
    r = s[(s.group == "160/170/170X") & (s.slot == "17:00")].iloc[0]
    assert (r.n, r["median"]) == (3, 22)


def test_departures_per_hour():
    # Fri 9 Oct: 4 departures at 08:xx, 1 at 09:xx; Mon 12 Oct: 2 at 08:xx, none at 09:xx -> weekday 08:00 = 3/h,
    # 09:00 = 0.5/h. Thu 8 Oct was polled only at 01:xx, so it doesn't dilute the 08:00 and 09:00 averages.
    rows = [{"service": "170", "service_day": d, "left_a": pd.Timestamp(f"{d} {t}"), "ok": True}
            for d, t in ((DAY, "08:05"), (DAY, "08:20"), (DAY, "08:35"), (DAY, "08:50"),
                         ("2026-10-12", "08:10"), ("2026-10-12", "08:40"), (DAY, "09:10"))]
    runs = pd.DataFrame([{"polled_at": f"{d} {h}:{m:02d}:01", "stop": "46219", "ok": 1}
                         for d, hours in ((DAY, ("08", "09")), ("2026-10-12", ("08", "09")), ("2026-10-09", ("01",)))
                         for h in hours for m in range(0, 60, 10)])
    d = D.departures_per_hour(pd.DataFrame(rows), runs).set_index(["group", "day_type", "slot"])
    assert d.loc[("160/170/170X", "weekday", "08:00"), "per_hour"] == 3
    assert d.loc[("160/170/170X", "weekday", "09:00"), "per_hour"] == 0.5


def test_service_day_labels():
    assert D.service_day(datetime.fromisoformat("2026-10-10 01:30")) == "2026-10-09"
    assert D.day_label("2026-10-12") == "Mon 12 Oct"
    assert D.day_type("2026-10-10") == "weekend" and D.day_type("2026-10-09") == "weekday"
