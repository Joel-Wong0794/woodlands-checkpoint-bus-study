"""Tests for dashboard.py. Run from the project root: python -m pytest"""
from datetime import datetime

import pandas as pd

import dashboard as D
import trips as T
from collector import open_db
from tests.simulate import random_buses, simulate

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
    for text in (D.PROFILE_TITLE, D.HEATMAP_TITLE, "Now", "Typical",
                 "Bus 160 / 170 / 170X", "Bus 950", "How it's measured",
                 "Waiting at the JB berth"):
        assert text in html, text
    assert "No data yet" not in html
    assert "On the bridge now" not in html  # removed: it could mislead
    assert "Data quality" not in html  # removed: replaced by a two-sentence note on how it's measured


def test_plan_your_day(tmp_path):
    db = tmp_path / "causeway.db"
    fill_db(db, random_buses(30, service="170", first=f"{DAY} 06:00", headway=(10, 20), seed=3),
            f"{DAY} 05:30", f"{DAY} 18:00")
    html = D.build(db, now=datetime.fromisoformat(f"{DAY} 18:00:30"))
    assert D.PLAN_TITLE in html
    assert "Select a day to see typical wait (past 4 weeks)" in html
    assert all(f'data-day="{d}"' in html for d in D.WEEKDAYS)
    assert 'data-default="Fri"' in html  # opens on today's weekday
    assert "Going to JB" in html and "Returning to Singapore" in html
    assert "No Fri data yet" in html  # only JB -> SG was simulated, so the SG -> JB page says so instead of guessing
    assert "Fastest:" in html


def test_plan_stats_counts_days_per_weekday_and_direction():
    rows = [{"direction": dr, "service": "170", "service_day": d, "left_a": pd.Timestamp(f"{d} 09:{10 + i}"),
             "ride_min": 10 + i, "ok": True, "day_type": "weekday"}
            for dr, d in (("jb-sg", DAY), ("jb-sg", "2026-10-16"), ("sg-jb", DAY)) for i in range(3)]
    stats, dates = D.plan_stats(pd.DataFrame(rows))
    assert dates[("jb-sg", "Fri")] == [DAY, "2026-10-16"] and dates[("sg-jb", "Fri")] == [DAY]
    s = stats.set_index(["direction", "weekday", "group", "slot"])
    assert s.loc[("jb-sg", "Fri", "160/170/170X", "09:00"), "n"] == 6
    assert s.loc[("sg-jb", "Fri", "160/170/170X", "09:00"), "median"] == 11


def test_one_page_per_direction(tmp_path):
    db = tmp_path / "causeway.db"
    fill_db(db, random_buses(20, service="170", first=f"{DAY} 06:00", headway=(10, 20), seed=4),
            f"{DAY} 05:30", f"{DAY} 18:00")
    html = D.build(db, now=datetime.fromisoformat(f"{DAY} 18:00:30"))
    assert '<div class="dirs" data-default="sg-jb">' in html  # opens on "Going to JB"
    assert html.count('<section class="dirpage"') == 2
    assert 'data-dir="sg-jb"' in html and 'data-dir="jb-sg"' in html
    assert html.count(f"<h2>{D.PLAN_TITLE}</h2>") == 2  # each page has its own Plan your day
    jb_sg = html[html.index('<section class="dirpage" data-dir="jb-sg"'):]
    assert 'class="grp" data-grp="160-170-170X"' in jb_sg  # details sit on their direction's page


def test_bus_toggle_defaults_to_160_170_170X(tmp_path):
    db = tmp_path / "causeway.db"
    fill_db(db, random_buses(20, service="950", first=f"{DAY} 06:00", headway=(10, 20), seed=5)
            + random_buses(20, service="170", first=f"{DAY} 06:05", headway=(10, 20), seed=6),
            f"{DAY} 05:30", f"{DAY} 18:00")
    html = D.build(db, now=datetime.fromisoformat(f"{DAY} 18:00:30"))
    assert '<div class="grps" data-default="160-170-170X">' in html
    assert html.index('data-grp="160-170-170X"') < html.index('data-grp="950"')  # 160/170/170X button first
    assert 'grp: "bus"' in html  # new key: an old saved "950" doesn't override the new default
    # one block per bus on each of the two pages
    assert html.count('class="grp" data-grp="950"') == html.count('class="grp" data-grp="160-170-170X"') == 2


def test_live_cameras(tmp_path):
    db = tmp_path / "causeway.db"
    fill_db(db, random_buses(5, service="170", first=f"{DAY} 06:00", seed=7), f"{DAY} 05:30", f"{DAY} 09:00")
    html = D.build(db, now=datetime.fromisoformat(f"{DAY} 09:00:30"))
    assert "View from Woodlands Causeway (Towards Johor)" in html     # camera 2701
    assert "View from Woodlands Checkpoint (Towards BKE)" in html      # camera 2702
    assert 'id="cam-2701"' in html and 'id="cam-2702"' in html
    assert D.CAMERA_API in html  # the page fetches the latest images itself
    assert "data.gov.sg" in html  # attribution
    # tapping an image opens it in a pop-up viewer on the page (zoom, pan, close), not a new page
    assert '<dialog class="lb"' in html and "showModal" in html
    assert 'target="_blank"' not in html


def test_browser_picks_today_and_marks_now(tmp_path):
    db = tmp_path / "causeway.db"
    fill_db(db, random_buses(5, service="170", first=f"{DAY} 06:00", seed=8), f"{DAY} 05:30", f"{DAY} 09:00")
    html = D.build(db, now=datetime.fromisoformat(f"{DAY} 09:00:30"))
    assert 'timeZone: "Asia/Singapore"' in html  # today and the current hour come from the viewer's clock, in SGT
    assert "function sgtNow()" in html and "function markNow()" in html
    assert "showDay(first(\"day\", todayName(), false))" in html  # the weekday isn't restored from a past visit


def test_ride_and_waiting_are_tabs(tmp_path):
    db = tmp_path / "causeway.db"
    fill_db(db, random_buses(20, service="170", first=f"{DAY} 06:00", headway=(10, 20), seed=9),
            f"{DAY} 05:30", f"{DAY} 18:00")
    html = D.build(db, now=datetime.fromisoformat(f"{DAY} 18:00:30"))
    jb_sg = html[html.index('<section class="dirpage" data-dir="jb-sg"'):]
    block = jb_sg[jb_sg.index('class="grp" data-grp="160-170-170X"'):]
    assert block.index('data-tab="ride"') < block.index(D.PROFILE_TITLE) < block.index(D.HEATMAP_TITLE)
    assert block.index('class="panel" data-tab="wait"') < block.index("<h3>Waiting at the JB berth</h3>")
    # waiting comes first: left tab, pressed, shown; ride time hidden until tapped
    assert block.index('<button type="button" data-tab="wait" aria-pressed="true">') < block.index('data-tab="ride"')
    assert 'class="panel" data-tab="ride" hidden' in block and 'class="panel" data-tab="wait" hidden' not in block


def test_one_day_selector_drives_everything(tmp_path):
    db = tmp_path / "causeway.db"
    fill_db(db, random_buses(30, service="170", first=f"{DAY} 06:00", headway=(10, 20), seed=10),
            f"{DAY} 05:30", f"{DAY} 18:00")
    html = D.build(db, now=datetime.fromisoformat(f"{DAY} 18:00:30"))
    jb_sg = html[html.index('<section class="dirpage" data-dir="jb-sg"'):]
    block = jb_sg[jb_sg.index('class="grp" data-grp="160-170-170X"'):jb_sg.index('class="grp" data-grp="950"')]
    ride = block[block.index('class="panel" data-tab="ride"'):block.index('class="panel" data-tab="wait"')]
    wait = block[block.index('class="panel" data-tab="wait"'):]
    for part in (ride, wait):  # both tabs have a view per weekday
        assert all(f'class="day" data-day="{d}"' in part for d in D.WEEKDAYS)
    assert "Typical" in block and block.count('class="day" data-day="Fri"') == 3  # tile, ride, wait
    assert "Weekend median" not in html  # the weekday-vs-weekend chart is gone
    assert html.count('<div class="days"') == 2  # one day selector per page


def test_charts_cannot_zoom_but_still_hover():
    import plotly.graph_objects as go
    fig = D.base_layout(go.Figure(go.Scatter(x=[5, 6], y=[1, 2], hovertemplate="%{y}")), 300)
    assert fig.layout.xaxis.fixedrange and fig.layout.yaxis.fixedrange  # no drag / pinch zoom
    assert fig.layout.dragmode is False
    assert fig.layout.hovermode == "closest"  # tapping a point still shows its details
    assert '"doubleClick": false' in D.fig_html(fig) or '"doubleClick":false' in D.fig_html(fig)


def test_ride_now_names_its_hour():
    trips = pd.DataFrame([{"ok": True, "group": "160/170/170X", "ride_min": m,
                           "reached_b": pd.Timestamp(f"{DAY} 14:{5 + 5 * i:02d}")} for i, m in enumerate((4, 6, 8))])
    assert D.now_tile(trips, "160/170/170X", pd.Timestamp(f"{DAY} 14:20")) == ("6 min", "3 trips, 13:20–14:20")
    assert D.now_tile(trips, "950", pd.Timestamp(f"{DAY} 14:20")) == ("—", "no trips 13:20–14:20")


def test_best_times_are_shaded():
    stats = pd.DataFrame({"direction": "jb-sg", "weekday": "Sat", "group": "160/170/170X",
                          "slot": [f"{h:02d}:00" for h in range(6, 23)], "n": 5, "p25": 1.0, "p75": 2.0,
                          "median": [10.0 + (h % 7) for h in range(6, 23)]})
    fig = D.day_fig(stats, "jb-sg", "Sat", "160/170/170X")
    best = [sh for sh in fig.layout.shapes if sh.name == "best"]
    assert sorted(sh.x0 for sh in best) == [7, 14, 21]  # the three lowest-median hours, shaded across their hour
    assert all(sh.x1 - sh.x0 == 1 for sh in best)
    assert "Best times" in [t.name for t in fig.data]  # legend entry for the shading
    flat = stats.assign(median=10.0)  # all hours the same: nothing is "best"
    assert not [sh for sh in D.day_fig(flat, "jb-sg", "Sat", "160/170/170X").layout.shapes if sh.name == "best"]


def test_now_line_keeps_the_shading(tmp_path):
    db = tmp_path / "causeway.db"
    fill_db(db, random_buses(5, service="170", first=f"{DAY} 06:00", seed=12), f"{DAY} 05:30", f"{DAY} 09:00")
    html = D.build(db, now=datetime.fromisoformat(f"{DAY} 09:00:30"))
    assert 's.name !== "now"' in html  # markNow swaps only its own line, not the best-times bands


def test_reloads_when_a_new_page_is_uploaded(tmp_path):
    db = tmp_path / "causeway.db"
    fill_db(db, random_buses(5, service="170", first=f"{DAY} 06:00", seed=13), f"{DAY} 05:30", f"{DAY} 09:00")
    html = D.build(db, now=datetime.fromisoformat(f"{DAY} 09:00:30"))
    assert 'method: "HEAD"' in html and "location.reload()" in html  # reloads only when the uploaded file changed
    assert 'first("tab", null, false)' in html  # the open tab survives the reload


def test_empty_db(tmp_path):
    db = tmp_path / "causeway.db"
    open_db(db).close()
    html = D.build(db, now=datetime.fromisoformat(f"{DAY} 12:00"))
    assert "No data yet" in html


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


def next_wait(departures, gaps=(), **extra):
    rows = [{"service": svc, "group": T.group_of(svc), "service_day": DAY, "left_a": pd.Timestamp(f"{DAY} {t}"),
             "ok": ok, **extra} for svc, t, ok in departures]
    g = pd.DataFrame([{"stop": "46219", "gap_from": pd.Timestamp(f"{DAY} {a}"), "gap_to": pd.Timestamp(f"{DAY} {b}")}
                      for a, b in gaps], columns=["stop", "gap_from", "gap_to"])
    return D.next_bus_wait(pd.DataFrame(rows), g)


def test_next_bus_wait_from_gaps_between_any_bus_of_the_group():
    # 160, 170X and 170 all count (dropped trips too: they still left). Gaps 10, 2, 6 min from 17:00:
    # commuters arriving each minute wait 9.5..0.5, 1.5, 0.5, 5.5..0.5 -> 18 waits, median 3.5
    s = next_wait((("160", "17:00", True), ("170X", "17:10", False), ("170", "17:12", True), ("170", "17:18", True)))
    r = s[(s.group == "160/170/170X") & (s.weekday == "Fri") & (s.slot == "17:00")].iloc[0]
    assert (r.direction, r.n, r["median"]) == ("jb-sg", 3, 3.5)
    assert s[s.group == "950"].empty  # another berth


def test_next_bus_wait_splits_a_gap_by_the_hour_people_arrive(monkeypatch):
    # 16:40 -> 17:10: 20 commuters arrive in the 16:00 hour (waits 29.5..10.5), 10 in the 17:00 hour (9.5..0.5)
    monkeypatch.setattr(D, "MIN_TRIPS_PER_SLOT", 1)
    s = next_wait((("170", "16:40", True), ("170", "17:10", True)))
    s = s.set_index("slot")
    assert s.loc["16:00", "median"] == 20 and s.loc["17:00", "median"] == 5


def test_next_bus_wait_skips_polling_gaps_and_special_days():
    deps = (("170", "17:00", True), ("170", "17:10", True), ("170", "17:20", True), ("170", "17:30", True))
    assert len(next_wait(deps)) == 1
    assert next_wait(deps, gaps=[("17:05", "17:09")]).empty  # one gap left -> n = 2, hidden
    assert next_wait(deps, special="ph_eve").empty


def test_departures_per_hour():
    # Fri 9 and Fri 16 Oct: 4 + 2 departures at 08:xx, 1 + 0 at 09:xx -> Fri 08:00 = 3/h, 09:00 = 0.5/h.
    # Thu 8 Oct was polled only at 01:xx, so it doesn't count as a day for 08:00 or 09:00.
    fri2 = "2026-10-16"
    rows = [{"service": "170", "service_day": d, "left_a": pd.Timestamp(f"{d} {t}"), "ok": True}
            for d, t in ((DAY, "08:05"), (DAY, "08:20"), (DAY, "08:35"), (DAY, "08:50"),
                         (fri2, "08:10"), (fri2, "08:40"), (DAY, "09:10"))]
    runs = pd.DataFrame([{"polled_at": f"{d} {h}:{m:02d}:01", "stop": "46219", "ok": 1}
                         for d, hours in ((DAY, ("08", "09")), (fri2, ("08", "09")), ("2026-10-09", ("01",)))
                         for h in hours for m in range(0, 60, 10)])
    d = D.departures_per_hour(pd.DataFrame(rows), runs).set_index(["group", "weekday", "slot"])
    assert d.loc[("160/170/170X", "Fri", "08:00"), "per_hour"] == 3
    assert d.loc[("160/170/170X", "Fri", "09:00"), "per_hour"] == 0.5
    assert list(D.departures_per_hour(pd.DataFrame(rows), runs).columns) == ["group", "weekday", "slot", "per_hour"]


def test_best_text_says_flat_when_differences_are_noise():
    stats = pd.DataFrame({"direction": "sg-jb", "weekday": "Mon", "group": "160/170/170X",
                          "slot": ["08:00", "12:00", "16:00", "20:00"], "n": 4, "median": [4.0, 3.0, 4.0, 5.0],
                          "p25": 3.0, "p75": 5.0})
    assert D.best_text(stats, "sg-jb", "Mon", "160/170/170X") == "About the same all day (~4 min)"
    late = pd.DataFrame({"direction": ["sg-jb"], "weekday": "Mon", "group": "160/170/170X", "slot": ["23:00"], "n": 3,
                         "median": [17.0], "p25": 15.0, "p75": 18.0})  # outside 06:00-23:00: doesn't count
    assert D.best_text(pd.concat([stats, late]), "sg-jb", "Mon", "160/170/170X").startswith("About the same")
    stats["median"] = [4.0, 3.0, 12.0, 5.0]
    assert D.best_text(stats, "sg-jb", "Mon", "160/170/170X").startswith("Fastest: 12:00 (3 min)")


def test_heatmap_labels_mark_public_holidays():
    assert D.heat_label("2026-12-25") == "Fri 25 Dec ★"
    assert D.heat_label("2026-10-13") == "Tue 13 Oct"


def test_plan_stats_leaves_out_holidays_eves_and_last_days():
    rows = [{"direction": "jb-sg", "service": "170", "service_day": d, "left_a": pd.Timestamp(f"{d} 09:{10 + i}"),
             "ride_min": 10, "ok": True, "day_type": D.day_type(d), "special": D.special_day(d)}
            for d in ("2026-12-17", "2026-12-24", "2026-12-31") for i in range(3)]  # Thu; Christmas eve; Thu
    stats, dates = D.plan_stats(pd.DataFrame(rows))
    assert dates[("jb-sg", "Thu")] == ["2026-12-17"]  # both eves (24 and 31 Dec) left out


def test_service_day_labels():
    assert D.service_day(datetime.fromisoformat("2026-10-10 01:30")) == "2026-10-09"
    assert D.day_label("2026-10-12") == "Mon 12 Oct"
    assert D.day_type("2026-10-10") == "weekend" and D.day_type("2026-10-09") == "weekday"
    assert D.day_type("2026-12-25") == "holiday"
