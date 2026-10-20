"""Local data-quality viewer (not deployed). Run: streamlit run quality_app.py

Reads a copy of the VM database (default data/vm_causeway.db). Two views:
- Bus interval walkthrough: one hour followed from raw LTA rows to the dashboard's "A bus every N min".
- Data quality: polling health, GPS coverage, trips kept / dropped per day and direction.
"""
import sqlite3

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from config import (CAUSEWAY_LAT, COLLECT_STOPS, DIRECTIONS, GAP_SEC, GROUPS, MAX_LOST_MIN, MIN_TRIPS_PER_SLOT,
                    SERVICE_DAY_START_HOUR, SERVICES)
import dashboard as dash
import trips as tr

STOP_NAMES = {"46219": "46219 JB Checkpt (board JB→SG)", "46109": "46109 Woodlands Checkpt (alight JB→SG)",
              "46101": "46101 Woodlands Checkpt (board SG→JB)", "46211": "46211 JB Checkpt (alight SG→JB)"}
W = "stretch"


def service_day(ts):
    return (pd.to_datetime(ts) - pd.Timedelta(hours=SERVICE_DAY_START_HOUR)).dt.strftime("%Y-%m-%d")


@st.cache_data
def load(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        runs = pd.read_sql("SELECT * FROM poll_runs", con)
        polls = pd.read_sql("SELECT * FROM polls", con)
    finally:
        con.close()
    runs["day"], polls["day"] = service_day(runs.polled_at), service_day(polls.polled_at)
    return runs, polls


@st.cache_data
def all_trips(runs, polls):
    return pd.concat([tr.measure_trips(runs, polls, SERVICES, d).assign(direction=d) for d in DIRECTIONS],
                     ignore_index=True)


@st.cache_data
def points_tracks_deps(polls, direction):
    """The dashboard's own steps 1-3: GPS points, linked tracks, departures."""
    pts = tr.gps_points(polls, SERVICES, direction)
    tracks = tr.link_tracks(pts)
    deps = tr.find_departures(tracks, direction)
    deps["special"] = deps.service_day.map(dash.special_day)
    return pts, tracks, deps


st.set_page_config(page_title="Causeway data quality", layout="wide")
path = st.sidebar.text_input("Database", "data/vm_causeway.db")
try:
    runs, polls = load(path)
except Exception as e:
    st.error(f"Can't open {path}: {e}")
    st.stop()
days = sorted(runs.day.unique())
lo, hi = st.sidebar.select_slider("Service days", days, value=(days[0], days[-1]))
runs = runs[(runs.day >= lo) & (runs.day <= hi)]
polls = polls[(polls.day >= lo) & (polls.day <= hi)]
view = st.sidebar.radio("View", ["Bus interval walkthrough", "Data quality"])

# ====================== Bus interval walkthrough ======================
if view == "Bus interval walkthrough":
    st.title("From raw polls to “A bus every N min”")
    st.caption("Pick one service day and hour. Each step below is the same function the dashboard runs, "
               "shown on that hour only.")
    c1, c2, c3, c4 = st.columns(4)
    direction = c1.selectbox("Direction", list(DIRECTIONS), format_func=lambda d: DIRECTIONS[d]["name"])
    group = c2.selectbox("Bus", list(GROUPS))
    day = c3.selectbox("Service day", days[::-1][days[::-1].index(hi):days[::-1].index(lo) + 1],
                       index=1 if hi != lo else 0, format_func=dash.day_label)  # last full day
    hour = c4.selectbox("Hour", list(range(5, 24)) + [0, 1, 2], index=13, format_func=lambda h: f"{h:02d}:00")
    d = DIRECTIONS[direction]
    svcs = GROUPS[group]
    start = pd.Timestamp(day) + pd.Timedelta(hours=hour + (24 if hour < SERVICE_DAY_START_HOUR else 0))
    end = start + pd.Timedelta(hours=1)
    pad = pd.Timedelta(minutes=MAX_LOST_MIN + 15)  # context either side: a departure needs fixes after it

    # ---- Step 1: raw rows ----
    st.header("1. Raw LTA rows")
    st.markdown(f"The collector asks LTA every minute which buses are coming to **{d['board']}** (board) and "
                f"**{d['alight']}** (alight). Each answer lists up to 3 buses per service. A row has a GPS "
                "position only when `monitored = 1`; timetable-only rows can't be followed.")
    t_polls = pd.to_datetime(polls.polled_at)
    raw = polls[polls.stop.isin([d["board"], d["alight"]]) & polls.service.isin(svcs)
                & (t_polls >= start) & (t_polls < end)]
    k1, k2, k3 = st.columns(3)
    k1.metric("Rows in this hour", len(raw))
    k2.metric("With GPS", int(raw.lat.notna().sum()))
    k3.metric("Timetable only (ignored)", int(raw.lat.isna().sum()))
    with st.expander("Show the raw rows"):
        st.dataframe(raw.drop(columns="day"), width=W)

    # ---- Step 2 + 3: points and tracks ----
    pts, tracks, deps = points_tracks_deps(polls, direction)
    win = tracks[tracks.service.isin(svcs) & (tracks.t >= start - pad) & (tracks.t < end + pad)]
    st.header("2. GPS points → 3. tracks")
    st.markdown(f"`gps_points`: GPS rows only; the same bus listed at both stops in one minute is kept once. "
                f"Each point is marked **at the boarding stop** if within {d['board_m']} m of it.  \n"
                "`link_tracks`: there is no bus ID, so each minute's points are joined to the nearest track that "
                "could have driven there (≤ 25 m/s + 200 m), closest first. A track survives "
                f"{MAX_LOST_MIN} min without a fix. Each line below is one bus as the method sees it.")
    fig = px.line(win.sort_values("t"), x="t", y="lat", color="track", markers=True,
                  hover_data=["service", "dist_a", "at_a"], height=480)
    fig.add_hrect(y0=CAUSEWAY_LAT[0], y1=CAUSEWAY_LAT[1], fillcolor="grey", opacity=.12, line_width=0,
                  annotation_text="Causeway", annotation_position="top left")
    fig.add_vrect(x0=start, x1=end, fillcolor="gold", opacity=.12, line_width=0, annotation_text="chosen hour")
    at_a = win[win.at_a]
    fig.add_scatter(x=at_a.t, y=at_a.lat, mode="markers", name="at boarding stop",
                    marker=dict(symbol="square-open", size=11, color="black"))
    wd_ = deps[(deps.group == group) & (pd.to_datetime(deps.left_a) >= start - pad)
               & (pd.to_datetime(deps.left_a) < end + pad)]
    left_fix = win.set_index(["track", "t"]).lat
    fig.add_scatter(x=pd.to_datetime(wd_.left_a),
                    y=[left_fix.get((k, pd.Timestamp(t)), None) for k, t in zip(wd_.track, wd_.left_a)],
                    mode="markers", name="departure", marker=dict(symbol="triangle-down", size=14, color="red"))
    fig.update_layout(yaxis_title="latitude (north = JB)", xaxis_title="")
    st.plotly_chart(fig, width=W)
    st.caption(f"{win.track.nunique()} tracks in the window (chosen hour ± {pad.seconds // 60} min).")

    # ---- Step 4: departures ----
    st.header("4. Departures")
    st.markdown("`find_departures`: in each track, the **last fix at the boarding stop** before the bus is seen on "
                f"the Causeway (or at the alighting stop) within {MAX_LOST_MIN} min. The bus needn't be seen "
                "arriving. Buses with no GPS when they leave are invisible.")
    gd = deps[(deps.group == group) & (deps.service_day == day)].copy()
    gd["left"] = pd.to_datetime(gd.left_a)
    gd = gd.sort_values("left")
    st.caption("Departures are the red triangles on the chart above.")

    # ---- Step 5: gaps ----
    st.header("5. Gaps between buses")
    st.markdown("Sort the group's departures for the whole service day; each gap = next departure − previous "
                "(any service in the group). A gap counts in the hour the **earlier** bus left. Gaps overlapping "
                "a polling gap are dropped; special days (holidays, eves) are left out of the dashboard.")
    gd["gap_min"] = gd.left.diff().dt.total_seconds() / 60
    gd["prev_left"] = gd.left.shift()
    pg = tr.poll_gaps(runs[["polled_at", "stop", "ok"]], direction)
    pg_from, pg_to = pd.to_datetime(pg.gap_from), pd.to_datetime(pg.gap_to)
    gd["polling gap"] = [pd.notna(p) and bool(((pg_from < l) & (pg_to > p)).any())
                         for p, l in zip(gd.prev_left, gd.left)]
    in_hour = gd[(gd.prev_left >= start) & (gd.prev_left < end)]
    used = in_hour[~in_hour["polling gap"]]
    show = in_hour.assign(**{"left (earlier bus)": in_hour.prev_left.dt.strftime("%H:%M:%S"),
                             "next bus left": in_hour.left.dt.strftime("%H:%M:%S")})
    st.dataframe(show[["left (earlier bus)", "next bus left", "service", "track", "gap_min", "polling gap"]]
                 .rename(columns={"service": "next bus service", "track": "next bus track"}), width=W,
                 hide_index=True)
    k1, k2, k3 = st.columns(3)
    k1.metric("Gaps starting this hour", len(used))
    k2.metric("Median gap (this day)", f"{used.gap_min.median():.1f} min" if len(used) else "—")
    k3.metric("Special day?", "yes – excluded" if dash.special_day(day) else "no")
    gb = go.Figure(go.Bar(x=in_hour.prev_left, y=in_hour.gap_min, width=40_000,
                          text=in_hour.gap_min.round(1), textposition="outside"))
    gb.update_layout(height=260, yaxis_title="min to next bus", xaxis_title="earlier bus left", margin=dict(t=20))
    st.plotly_chart(gb, width=W)

    # ---- Step 6: typical ----
    wd = pd.Timestamp(day).strftime("%a")
    slot = f"{hour:02d}:00"
    st.header(f"6. Typical {wd} {slot}: what the dashboard shows")
    st.markdown(f"`bus_gaps` pools every **{wd}** in the data: median (and P25–P75) of all gaps that started in "
                f"{slot}. Shown only with ≥ {MIN_TRIPS_PER_SLOT} gaps"
                + ("" if (direction, group) in dash.GAP_SHOWN else
                   " — **not shown on the dashboard for this bus and direction** (too few departures seen)") + ".")
    all_g = deps[deps.group == group].assign(left=lambda x: pd.to_datetime(x.left_a)).sort_values("left")
    all_g["gap_min"] = all_g.groupby("service_day").left.diff().dt.total_seconds() / 60
    all_g["prev_left"] = all_g.left - pd.to_timedelta(all_g.gap_min, unit="min")
    same = all_g[(pd.to_datetime(all_g.service_day).dt.strftime("%a") == wd)
                 & (all_g.prev_left.dt.strftime("%H:00") == slot) & ~all_g.special.astype(bool)]
    same = same[[not ((pg_from < l) & (pg_to > p)).any() for p, l in zip(same.prev_left, same.left)]]
    per_day = same.groupby("service_day").gap_min.agg(n="size", median="median").reset_index()
    st.dataframe(per_day.assign(service_day=per_day.service_day.map(dash.day_label)), hide_index=True, width=W)
    s = dash.bus_gaps(deps, pg, direction)
    row = s[(s.weekday == wd) & (s.group == group) & (s.slot == slot)]
    if row.empty:
        st.info(f"Fewer than {MIN_TRIPS_PER_SLOT} gaps: the dashboard shows “Not enough data yet”.")
    else:
        r = row.iloc[0]
        st.success(f"**A bus every {r['median']:.0f} min** (median of {r.n} gaps; usually "
                   f"{r.p25:.0f}–{r.p75:.0f} min). Check: median of the gaps pooled above = "
                   f"{same.gap_min.median():.1f} min.")
    st.stop()

# ====================== Data quality ======================
st.title("Causeway bus data quality")
st.caption(f"{runs.polled_at.min()} → {runs.polled_at.max()} · {len(runs):,} API calls · {len(polls):,} bus rows")

st.header("1. Polling")
r = runs.copy()
r["t"] = pd.to_datetime(r.polled_at)
r = r.sort_values("t")
r["gap_s"] = r.groupby("stop").t.diff().dt.total_seconds()
per_day = r.groupby(["day", "stop"]).agg(calls=("ok", "size"), ok=("ok", "sum"),
                                          max_gap_min=("gap_s", lambda x: round(x.max() / 60, 1))).reset_index()
per_day["ok %"] = (100 * per_day.ok / per_day.calls).round(1)
c1, c2 = st.columns(2)
c1.plotly_chart(px.bar(per_day, x="day", y="calls", color="stop", barmode="group",
                       title="Calls per day (1440 = every minute)"), width=W)
c2.plotly_chart(px.line(per_day, x="day", y="max_gap_min", color="stop", markers=True,
                        title="Longest gap between calls (min)"), width=W)
gaps = r[r.gap_s > GAP_SEC][["stop", "polled_at", "gap_s"]].assign(gap_min=lambda x: (x.gap_s / 60).round(1))
st.subheader(f"Gaps longer than {GAP_SEC // 60} min: {len(gaps)}")
st.dataframe(gaps.drop(columns="gap_s").rename(columns={"polled_at": "resumed_at"}), width=W)
errs = runs[runs.ok == 0]
st.subheader(f"Failed calls: {len(errs)}")
if len(errs):
    st.dataframe(errs.groupby(["day", "stop", "error"]).size().rename("n").reset_index(), width=W)

st.header("2. GPS coverage")
st.caption("Share of listed buses with a live GPS position (Monitored = 1). Trips can only be measured from GPS.")
p = polls[polls.service.isin(SERVICES)]
cov = p.groupby(["day", "stop", "service"]).monitored.mean().mul(100).round(1).rename("gps %").reset_index()
stop = st.selectbox("Stop", COLLECT_STOPS, format_func=lambda s: STOP_NAMES.get(s, s))
st.plotly_chart(px.line(cov[cov.stop == stop], x="day", y="gps %", color="service", markers=True, range_y=[0, 100]),
                width=W)
p_hour = p[p.stop == stop].assign(hour=pd.to_datetime(p.polled_at).dt.hour)
st.plotly_chart(px.density_heatmap(p_hour.groupby(["day", "hour"]).monitored.mean().mul(100).reset_index(),
                                   x="hour", y="day", z="monitored", nbinsx=24, color_continuous_scale="Blues",
                                   title=f"GPS % by hour at {stop}"), width=W)

st.header("3. Trips found")
with st.spinner("Finding trips (a few seconds)…"):
    t = all_trips(runs[["polled_at", "stop", "ok"]], polls)
direction = st.radio("Direction", list(DIRECTIONS), horizontal=True)
td = t[t.direction == direction].copy()
td["outcome"] = td.reason.replace("", "kept")
k1, k2, k3, k4 = st.columns(4)
k1.metric("Trips found", len(td))
k2.metric("Kept", f"{td.ok.mean() * 100:.0f}%" if len(td) else "—")
k3.metric("Blind (> 5 min no fix)", f"{td.blind.mean() * 100:.0f}%" if len(td) else "—")
k4.metric("Median ride (kept)", f"{td[td.ok].ride_min.median():.1f} min" if td.ok.any() else "—")
c1, c2 = st.columns(2)
c1.plotly_chart(px.histogram(td, x="service_day", color="outcome", title="Trips per day by outcome"), width=W)
c2.plotly_chart(px.histogram(td[td.ok], x="service_day", color="service", title="Kept trips per day by service"),
                width=W)
c1, c2 = st.columns(2)
c1.plotly_chart(px.box(td[td.ok], x="service", y="ride_min", points="outliers", title="Ride minutes (kept)"), width=W)
td["hour"] = pd.to_datetime(td.left_a).dt.hour
c2.plotly_chart(px.histogram(td[td.ok], x="hour", nbins=24, title="Kept trips by hour left"), width=W)
st.subheader("All trips")
only = st.multiselect("Show outcomes", sorted(td.outcome.unique()), default=sorted(td.outcome.unique()))
st.dataframe(td[td.outcome.isin(only)].sort_values("left_a", ascending=False), width=W)
