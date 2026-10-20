"""Local data-quality viewer (not deployed). Run: streamlit run quality_app.py

Reads a copy of the VM database (default data/vm_causeway.db) and shows polling health, GPS coverage and
how many trips the method keeps or drops, per day and direction.
"""
import sqlite3

import pandas as pd
import plotly.express as px
import streamlit as st

from config import COLLECT_STOPS, DIRECTIONS, GAP_SEC, SERVICE_DAY_START_HOUR, SERVICES
import trips as tr

STOP_NAMES = {"46219": "46219 JB Checkpt (board JBâ†’SG)", "46109": "46109 Woodlands Checkpt (alight JBâ†’SG)",
              "46101": "46101 Woodlands Checkpt (board SGâ†’JB)", "46211": "46211 JB Checkpt (alight SGâ†’JB)"}


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
    out = []
    for d in DIRECTIONS:
        t = tr.measure_trips(runs, polls, SERVICES, d)
        out.append(t.assign(direction=d))
    return pd.concat(out, ignore_index=True)


st.set_page_config(page_title="Causeway data quality", layout="wide")
st.title("Causeway bus data quality")
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
st.caption(f"{runs.polled_at.min()} â†’ {runs.polled_at.max()} Â· {len(runs):,} API calls Â· {len(polls):,} bus rows")

# ---------- 1. Polling ----------
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
                       title="Calls per day (1440 = every minute)"), width="stretch")
c2.plotly_chart(px.line(per_day, x="day", y="max_gap_min", color="stop", markers=True,
                        title="Longest gap between calls (min)"), width="stretch")
gaps = r[r.gap_s > GAP_SEC][["stop", "polled_at", "gap_s"]].assign(gap_min=lambda d: (d.gap_s / 60).round(1))
st.subheader(f"Gaps longer than {GAP_SEC // 60} min: {len(gaps)}")
st.dataframe(gaps.drop(columns="gap_s").rename(columns={"polled_at": "resumed_at"}), width="stretch")
errs = runs[runs.ok == 0]
st.subheader(f"Failed calls: {len(errs)}")
if len(errs):
    st.dataframe(errs.groupby(["day", "stop", "error"]).size().rename("n").reset_index(), width="stretch")

# ---------- 2. GPS coverage ----------
st.header("2. GPS coverage")
st.caption("Share of listed buses with a live GPS position (Monitored = 1). Trips can only be measured from GPS.")
p = polls[polls.service.isin(SERVICES)]
cov = p.groupby(["day", "stop", "service"]).monitored.mean().mul(100).round(1).rename("gps %").reset_index()
stop = st.selectbox("Stop", COLLECT_STOPS, format_func=lambda s: STOP_NAMES.get(s, s))
st.plotly_chart(px.line(cov[cov.stop == stop], x="day", y="gps %", color="service", markers=True, range_y=[0, 100]),
                width="stretch")
p_hour = p[p.stop == stop].assign(hour=pd.to_datetime(p.polled_at).dt.hour)
st.plotly_chart(px.density_heatmap(p_hour.groupby(["day", "hour"]).monitored.mean().mul(100).reset_index(),
                                   x="hour", y="day", z="monitored", nbinsx=24, color_continuous_scale="Blues",
                                   title=f"GPS % by hour at {stop}"), width="stretch")

# ---------- 3. Trips ----------
st.header("3. Trips found")
with st.spinner("Finding trips (a few seconds)â€¦"):
    t = all_trips(runs[["polled_at", "stop", "ok"]], polls)
direction = st.radio("Direction", list(DIRECTIONS), horizontal=True)
td = t[t.direction == direction].copy()
td["outcome"] = td.reason.replace("", "kept")
k1, k2, k3, k4 = st.columns(4)
k1.metric("Trips found", len(td))
k2.metric("Kept", f"{td.ok.mean() * 100:.0f}%" if len(td) else "â€”")
k3.metric("Blind (> 5 min no fix)", f"{td.blind.mean() * 100:.0f}%" if len(td) else "â€”")
k4.metric("Median ride (kept)", f"{td[td.ok].ride_min.median():.1f} min" if td.ok.any() else "â€”")
c1, c2 = st.columns(2)
c1.plotly_chart(px.histogram(td, x="service_day", color="outcome", title="Trips per day by outcome"),
                width="stretch")
c2.plotly_chart(px.histogram(td[td.ok], x="service_day", color="service", title="Kept trips per day by service"),
                width="stretch")
c1, c2 = st.columns(2)
c1.plotly_chart(px.box(td[td.ok], x="service", y="ride_min", points="outliers", title="Ride minutes (kept)"),
                width="stretch")
td["hour"] = pd.to_datetime(td.left_a).dt.hour
c2.plotly_chart(px.histogram(td[td.ok], x="hour", nbins=24, title="Kept trips by hour left"),
                width="stretch")
st.subheader("All trips")
only = st.multiselect("Show outcomes", sorted(td.outcome.unique()), default=sorted(td.outcome.unique()))
st.dataframe(td[td.outcome.isin(only)].sort_values("left_a", ascending=False), width="stretch")
