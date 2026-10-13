"""Stage 4: build dashboard.html from the database. Run by cron every 15 min.

Everything is recomputed from the raw polls in the window: polls -> trips (trips.py) -> tiles, charts, tables.
"""
import json
import sqlite3
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go

import calendar_flags as C
import trips as T
from config import (CAUSEWAY_LAT, COLLECT_STOPS, DASH_DAYS, DIRECTIONS, GROUPS, MAX_TRIP_MIN, MIN_TRIPS_PER_SLOT, PLAN_DAYS,
                    SERVICE_DAY_START_HOUR, SLOT_MIN)

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "causeway.db"
OUT = ROOT / "dashboard.html"
SGT = ZoneInfo("Asia/Singapore")

PROFILE_TITLE = "Ride time through the day"
HEATMAP_TITLE = f"Last {DASH_DAYS} days by hour"
PLAN_TITLE = "Plan your day"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
PAGES = (("sg-jb", "Going to JB"), ("jb-sg", "Returning to Singapore"))  # one page each, in day-trip order
BERTH = {"jb-sg": "JB", "sg-jb": "Woodlands"}  # where you board
BUS_ORDER = ("160/170/170X", "950")  # bus toggle order; the first is the default
# LTA traffic cameras via data.gov.sg (camera ids checked against the images on 13 Oct 2026). The page fetches the
# latest images itself, in the viewer's browser (the API allows that), every minute while it is open.
CAMERA_API = "https://api.data.gov.sg/v1/transport/traffic-images"
CAMERAS = (("2701", "View from Woodlands Causeway (Towards Johor)"),
           ("2702", "View from Woodlands Checkpoint (Towards BKE)"))
# Services with GPS while they wait at the boarding berth. At JB (9-11 Oct data) 160 and 170X only switch it on
# when they leave, so their measured "wait" is just the last minute or two and is left out. At Woodlands all four
# report while waiting (12-13 Oct: median waits 8-13 min for every service).
DWELL_SERVICES = {"jb-sg": ("170", "950"), "sg-jb": ("160", "170", "170X", "950")}
FRESH_MIN = 5        # a bus counts as "on the bridge now" only if its last GPS fix is this recent
BEST_HOURS = (6, 23)  # best/worst times are picked from slots starting 06:00-22:59
MIN_SPREAD_MIN = 3    # name fastest/slowest hours only if their medians differ by this much (each ride is +-1 min)
HOURS = range(5, 26)  # x axis: 05:00 -> 01:00 (next day)

# Colours (dataviz reference palette). The page script swaps LIGHT -> DARK for dark mode.
LIGHT = {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "grid": "#e7e6e2",
         "weekday": "#2a78d6", "weekend": "#eb6834", "today": "#1baf7a",
         "weekday_band": "rgba(42,120,214,0.16)", "weekend_band": "rgba(235,104,52,0.16)",
         "today_band": "rgba(27,175,122,0.16)",
         "ramp": ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]}
DARK = {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "grid": "#33322f",
        "weekday": "#3987e5", "weekend": "#d95926", "today": "#199e70",
        "weekday_band": "rgba(57,135,229,0.22)", "weekend_band": "rgba(217,89,38,0.22)",
        "today_band": "rgba(25,158,112,0.22)",
        "ramp": ["#184f95", "#256abf", "#3987e5", "#6da7ec", "#b7d3f6"]}
GROUP_COLOURS = ("weekday", "today")  # LIGHT/DARK keys for the 1st and 2nd group in GROUPS


# ---------- Small pure helpers ----------

def service_day(dt):
    """A service day runs 03:00 -> 02:59, labelled by the date it starts."""
    return (pd.Timestamp(dt) - pd.Timedelta(hours=SERVICE_DAY_START_HOUR)).strftime("%Y-%m-%d")


def day_label(day):
    d = pd.Timestamp(day)
    return f"{d:%a} {d.day} {d:%b}"


@lru_cache(maxsize=None)
def day_type(day):
    """'holiday' (public holiday in Singapore or Johor), 'weekend' or 'weekday' (calendar_flags)."""
    return C.day_type(str(day)[:10])


@lru_cache(maxsize=None)
def special_day(day):
    """A public holiday, its eve or the last day of a long weekend: left out of Plan your day."""
    return C.is_special(str(day)[:10])


def heat_label(day):
    """Heatmap row label, e.g. 'Fri 25 Dec ★' for a public holiday (Singapore or Johor).
    School holidays are not shown for now (user, 13 Oct 2026); calendar_flags still supports them."""
    f = C.day_flags(str(day)[:10])
    return f"{day_label(day)} ★" if f["sg_ph"] or f["my_ph"] else day_label(day)


def hour_x(t):
    """Hour of day as a number on the service-day axis: 00:00-02:59 plots after 23:00."""
    t = pd.to_datetime(t)
    x = t.dt.hour + t.dt.minute / 60
    return x.where(x >= SERVICE_DAY_START_HOUR, x + 24)


def group_name(group):
    return "Bus " + " / ".join(GROUPS[group])


def group_key(group):
    """A group as an HTML attribute / link value, e.g. '160-170-170X'."""
    return group.replace("/", "-")


# ---------- Pure calculations ----------

def on_bridge(tracks, now):
    """Buses that left the boarding stop and haven't reached the alighting stop, whose latest GPS fix is on the
    Causeway and less than FRESH_MIN old. (A track that passed the boarding area and then went elsewhere, e.g.
    back into Singapore, is not on the bridge.)"""
    cols = ["group", "service", "track", "left_a", "elapsed_min"]
    rows = []
    for tid, g in tracks[tracks.t <= now].sort_values("t").groupby("track"):
        at_a = g[g.at_a]
        if at_a.empty:
            continue
        left = at_a.t.max()
        after = g[g.t > left]
        if after.empty or after.at_b.any() or (now - after.t.max()).total_seconds() > FRESH_MIN * 60:
            continue
        if not CAUSEWAY_LAT[0] <= after.lat.iloc[-1] < CAUSEWAY_LAT[1]:
            continue
        elapsed = (now - left).total_seconds() / 60
        if elapsed <= MAX_TRIP_MIN:
            rows.append({"group": T.group_of(g.service.iloc[0]), "service": g.service.iloc[0], "track": tid,
                         "left_a": left, "elapsed_min": elapsed})
    return T.to_frame(rows, cols)


def best_worst(stats, group, n=3):
    """The n slots with the lowest and highest median ride, per day type, between BEST_HOURS.

    With fewer than 2n slots, each list gets half of them, so no slot is both best and worst."""
    s = stats[(stats.group == group) & (stats.n >= MIN_TRIPS_PER_SLOT)]
    s = s[s.slot.str[:2].astype(int).between(BEST_HOURS[0], BEST_HOURS[1] - 1)]
    rows = []
    for dt, g in s.groupby("day_type"):
        k = min(n, len(g) // 2)
        for kind, part in (("best", g.nsmallest(k, "median")), ("worst", g.nlargest(k, "median"))):
            rows.append(part.assign(kind=kind))
    cols = ["day_type", "kind", "slot", "median", "n"]
    return pd.concat(rows)[cols].reset_index(drop=True) if rows else pd.DataFrame(columns=cols)


def berth_wait(trips, direction="jb-sg"):
    """How long buses sit at the boarding berth before leaving: median etc. per group, day type and slot."""
    t = trips[trips.ok.astype(bool) & trips.service.isin(DWELL_SERVICES[direction])]
    return T.slot_stats(t.assign(ride_min=t.wait_min), by=("group", "day_type", "slot"))


def departures_per_hour(trips, runs, direction="jb-sg"):
    """Average buses seen leaving the berth per hour, per group, day type and slot (a lower bound).

    Averaged only over days that were polled in that slot, so a partial first day or today's
    future hours don't dilute it.
    """
    cols = ["group", "day_type", "slot", "per_hour"]
    d = T.departures_seen(trips)
    if d.empty:
        return pd.DataFrame(columns=cols)
    polled = runs[(runs.ok == 1) & (runs.stop == DIRECTIONS[direction]["board"])]
    t = pd.to_datetime(polled.polled_at)
    cov = pd.DataFrame({"service_day": t.map(service_day), "slot": t.dt.floor(f"{SLOT_MIN}min").dt.strftime("%H:%M")})
    cov = cov.drop_duplicates().assign(day_type=lambda c: c.service_day.map(day_type))
    n_days = cov.groupby(["day_type", "slot"]).size().rename("n_days")
    d = d.assign(day_type=d.service_day.map(day_type)).groupby(["group", "day_type", "slot"]).departures.sum()
    d = d.reset_index().join(n_days, on=["day_type", "slot"])
    d["per_hour"] = d.departures / d.n_days * 60 / SLOT_MIN
    return d[cols]


def tiles(trips, stats, bridge, group, now, gap_today):
    """Values for the Now / Typical / On the bridge now tiles of one group."""
    ok = trips[trips.ok & (trips.group == group)]
    recent = ok[(ok.reached_b > now - timedelta(minutes=60)) & (ok.reached_b <= now)]
    now_v = (f"{recent.ride_min.median():.0f} min", f"{len(recent)} trips, last hour") \
        if len(recent) else ("—", "no trips in the last hour")
    slot = pd.Timestamp(now).floor(f"{SLOT_MIN}min").strftime("%H:%M")
    dtype = day_type(service_day(now))
    s = stats[(stats.group == group) & (stats.day_type == dtype) & (stats.slot == slot)]
    typ_v = (f"{s['median'].iloc[0]:.0f} min", f"{dtype} {slot} · {int(s.n.iloc[0])} trips") \
        if len(s) else ("—", "not enough data yet")
    b = bridge[bridge.group == group]
    if gap_today:
        bridge_v = ("—", "polling gap today")
    elif b.empty:
        bridge_v = ("0", "none on the Causeway")
    else:
        bridge_v = (f"{len(b)}", f"longest {b.elapsed_min.max():.0f} min so far")
    return {"Now": now_v, "Typical": typ_v, "On the bridge now": bridge_v}


def quality(runs, trips, days):
    """One row per service-day: polls OK per stop, trips kept/found per direction."""
    r = runs.assign(day=runs.polled_at.map(service_day))
    rows = []
    for day in days:
        row = {"Day": day_label(day)}
        for stop in COLLECT_STOPS:
            x = r[(r.day == day) & (r.stop == stop)]
            row[f"Polls OK {stop}"] = f"{int(x.ok.sum())}/{len(x)}"
        for direction, d in DIRECTIONS.items():
            t = trips[(trips.service_day == day) & (trips.direction == direction)]
            row[f"{d['name']}: trips kept"] = f"{int(t.ok.sum())}/{len(t)}"
        rows.append(row)
    return pd.DataFrame(rows)


def plan_stats(trips):
    """Ride per direction, weekday, group and slot (holidays left out), and the dates behind each direction x weekday."""
    t = trips[trips.ok & (trips.day_type != "holiday")]
    if "special" in t:
        t = t[~t.special.astype(bool)]  # eves and last days of long weekends aren't typical either
    stats = T.slot_stats(t, by=("direction", "weekday", "group", "slot"))
    t = t.assign(weekday=pd.to_datetime(t.service_day).dt.strftime("%a"))
    dates = {k: sorted(g.unique()) for k, g in t.groupby(["direction", "weekday"]).service_day}
    return stats, dates


def best_text(stats, direction, weekday, group):
    """'Fastest: 15:00 (8 min), ... · Slowest: ...' for one plan chart, or '' with too few hours."""
    s = stats[(stats.direction == direction) & (stats.weekday == weekday)].rename(columns={"weekday": "day_type"})
    g = s[(s.group == group) & (s.n >= MIN_TRIPS_PER_SLOT)]
    g = g[g.slot.str[:2].astype(int).between(BEST_HOURS[0], BEST_HOURS[1] - 1)]  # the hours best_worst ranks
    if len(g) and g["median"].max() - g["median"].min() < MIN_SPREAD_MIN:  # differences within the noise
        return f"About the same all day (~{g['median'].median():.0f} min)"
    bw = best_worst(s, group)
    parts = []
    for kind, label in (("best", "Fastest"), ("worst", "Slowest")):
        rows = bw[bw.kind == kind]
        if len(rows):
            parts.append(f"{label}: " + ", ".join(f"{r.slot} ({r['median']:.0f} min)" for _, r in rows.iterrows()))
    return " · ".join(parts)


# ---------- Figures ----------

def base_layout(fig, height):
    fig.update_layout(height=height, margin=dict(l=44, r=12, t=8, b=36), paper_bgcolor=LIGHT["surface"],
                      plot_bgcolor=LIGHT["surface"], font=dict(color=LIGHT["ink"], size=12,
                      family="system-ui, -apple-system, Segoe UI, sans-serif"),
                      legend=dict(orientation="h", y=1.02, yanchor="bottom", x=0, font=dict(color=LIGHT["ink2"])),
                      hovermode="closest")
    ticks = list(range(5, 26, 2))
    fig.update_xaxes(range=[4.5, 25.5], tickvals=ticks, ticktext=[f"{h % 24:02d}:00" for h in ticks],
                     gridcolor=LIGHT["grid"], linecolor=LIGHT["grid"], zeroline=False,
                     tickfont=dict(color=LIGHT["ink2"]), title_font=dict(color=LIGHT["ink2"]))
    fig.update_yaxes(gridcolor=LIGHT["grid"], linecolor=LIGHT["grid"], zeroline=False,
                     tickfont=dict(color=LIGHT["ink2"]), title_font=dict(color=LIGHT["ink2"]))
    return fig


def profile_fig(ok, stats, group, today, ytitle="ride (min)", height=340, direction="jb-sg"):
    fig = go.Figure()
    for dtype in ("weekday", "weekend"):
        s = stats[(stats.group == group) & (stats.day_type == dtype)].copy()
        if s.empty:
            continue
        s["x"] = hour_x(pd.to_datetime(s.slot, format="%H:%M")) + SLOT_MIN / 120
        # one row per slot on the axis, empty where hidden, so the line breaks instead of bridging the gap
        s = s.set_index("x").reindex([h + SLOT_MIN / 120 for h in HOURS]).rename_axis("x").reset_index()
        run = s["median"].isna().cumsum()  # contiguous stretches of shown slots share a number
        for _, seg in s[s["median"].notna()].groupby(run):
            fig.add_trace(go.Scatter(x=list(seg.x) + list(seg.x[::-1]), y=list(seg.p75) + list(seg.p25[::-1]),
                                     mode="none", fill="toself", fillcolor=LIGHT[f"{dtype}_band"],
                                     hoverinfo="skip", legendgroup=dtype, showlegend=False))
        fig.add_trace(go.Scatter(x=s.x, y=s["median"], mode="lines+markers", name=f"{dtype.title()} median",
                                 legendgroup=dtype, line=dict(width=2, color=LIGHT[dtype]),
                                 marker=dict(size=8, color=LIGHT[dtype]), customdata=s[["slot", "p25", "p75", "n"]],
                                 hovertemplate=f"{dtype} %{{customdata[0]}}: median %{{y:.0f}} min<br>"
                                               "P25-P75 %{customdata[1]:.0f}-%{customdata[2]:.0f}, "
                                               "%{customdata[3]} trips<extra></extra>"))
    t = ok[(ok.group == group) & (ok.service_day == today)]
    if len(t):
        fig.add_trace(go.Scatter(x=hour_x(t.left_a), y=t.ride_min, mode="markers", name="Today's trips",
                                 marker=dict(size=9, color=LIGHT["today"], line=dict(width=2, color=LIGHT["surface"])),
                                 customdata=list(zip(t.service, t.left_a.dt.strftime("%H:%M"))),
                                 hovertemplate="bus %{customdata[0]} left %{customdata[1]}: %{y:.0f} min<extra></extra>"))
    fig.update_yaxes(title_text=ytitle, rangemode="tozero")
    fig.update_xaxes(title_text=f"time the bus left the {BERTH[direction]} checkpoint")
    return base_layout(fig, height)


def plan_fig(stats, direction, weekday, groups=tuple(GROUPS)):
    """Median ride by hour for each of `groups` (shaded: middle half of trips), lines broken at hidden hours."""
    fig = go.Figure()
    for group, key in zip(GROUPS, GROUP_COLOURS):
        if group not in groups:
            continue
        s = stats[(stats.direction == direction) & (stats.weekday == weekday) & (stats.group == group)].copy()
        if s.empty:
            continue
        s["x"] = hour_x(pd.to_datetime(s.slot, format="%H:%M")) + SLOT_MIN / 120
        s = s.set_index("x").reindex([h + SLOT_MIN / 120 for h in HOURS]).rename_axis("x").reset_index()
        run = s["median"].isna().cumsum()
        for _, seg in s[s["median"].notna()].groupby(run):
            fig.add_trace(go.Scatter(x=list(seg.x) + list(seg.x[::-1]), y=list(seg.p75) + list(seg.p25[::-1]),
                                     mode="none", fill="toself", fillcolor=LIGHT[f"{key}_band"],
                                     hoverinfo="skip", legendgroup=group, showlegend=False))
        fig.add_trace(go.Scatter(x=s.x, y=s["median"], mode="lines+markers", name=group_name(group), legendgroup=group,
                                 line=dict(width=2, color=LIGHT[key]), marker=dict(size=8, color=LIGHT[key]),
                                 customdata=s[["slot", "p25", "p75", "n"]],
                                 hovertemplate=f"{group} leaving %{{customdata[0]}}: median %{{y:.0f}} min<br>"
                                               "middle half %{customdata[1]:.0f}-%{customdata[2]:.0f}, "
                                               "%{customdata[3]} trips<extra></extra>"))
    fig.update_yaxes(title_text="ride (min)", rangemode="tozero")
    fig.update_xaxes(title_text=f"time the bus left the {BERTH[direction]} checkpoint")
    fig.update_layout(showlegend=len(groups) > 1)
    return base_layout(fig, 300)


def departures_fig(deps, group, direction="jb-sg"):
    fig = go.Figure()
    for i, dtype in enumerate(("weekday", "weekend")):
        d = deps[(deps.group == group) & (deps.day_type == dtype)]
        if d.empty:
            continue
        x = hour_x(pd.to_datetime(d.slot, format="%H:%M")) + SLOT_MIN / 120
        fig.add_trace(go.Bar(x=x - 0.2 + 0.4 * i, y=d.per_hour, width=0.38, name=dtype.title(),
                             marker=dict(color=LIGHT[dtype]),
                             hovertemplate=f"{dtype} %{{x:.0f}}h: at least %{{y:.1f}} buses<extra></extra>"))
    fig.update_yaxes(title_text="buses per hour", rangemode="tozero")
    fig.update_xaxes(title_text=f"hour the bus left the {BERTH[direction]} checkpoint")
    return base_layout(fig, 240)


def heatmap_fig(ok, group, days, direction="jb-sg"):
    t = ok[ok.group == group].assign(h=lambda d: hour_x(d.left_a).astype(int))
    agg = t.groupby(["service_day", "h"]).ride_min.agg(["median", "size"])
    z = [[agg["median"].get((d, h)) for h in HOURS] for d in days]
    n = [[int(agg["size"].get((d, h), 0)) for h in HOURS] for d in days]
    labels = [heat_label(d) for d in days]
    ramp = LIGHT["ramp"]
    fig = go.Figure(go.Heatmap(x=[h + 0.5 for h in HOURS], y=labels, z=z, customdata=n, xgap=2, ygap=2,
                               colorscale=[[i / (len(ramp) - 1), c] for i, c in enumerate(ramp)], zmin=0,
                               colorbar=dict(title=dict(text="min", font=dict(color=LIGHT["ink2"])), thickness=10,
                                             tickfont=dict(color=LIGHT["ink2"])),
                               hovertemplate="%{y} %{x:.0f}h: median %{z:.0f} min (%{customdata} trips)<extra></extra>"))
    fig.update_yaxes(autorange="reversed", showgrid=False)
    fig.update_xaxes(showgrid=False, title_text=f"hour the bus left the {BERTH[direction]} checkpoint")
    return base_layout(fig, 90 + 24 * len(days))


# ---------- Page ----------

def fig_html(fig):
    return fig.to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False, "responsive": True})


def table_html(df):
    return df.to_html(index=False, border=0, classes="tbl", float_format=lambda x: f"{x:.0f}")


def tile_html(label, value, note):
    return f'<div class="tile"><div class="lbl">{label}</div><div class="val">{value}</div><div class="note">{note}</div></div>'


def page(body, subtitle=""):
    swap = {LIGHT[k]: DARK[k] for k in LIGHT if k != "ramp"} | dict(zip(LIGHT["ramp"], DARK["ramp"]))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Causeway Bus Times</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
:root {{ color-scheme: light; --surface:{LIGHT['surface']}; --ink:{LIGHT['ink']}; --ink2:{LIGHT['ink2']}; --line:{LIGHT['grid']}; --tile:#f3f2ef; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ color-scheme: dark; --surface:{DARK['surface']}; --ink:{DARK['ink']}; --ink2:{DARK['ink2']}; --line:{DARK['grid']}; --tile:#242422; }} }}
:root[data-theme="dark"] {{ color-scheme: dark; --surface:{DARK['surface']}; --ink:{DARK['ink']}; --ink2:{DARK['ink2']}; --line:{DARK['grid']}; --tile:#242422; }}
body {{ margin:0; padding:16px; background:var(--surface); color:var(--ink); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:980px; margin:0 auto; }}
h1 {{ font-size:19px; margin:0; }} h2 {{ font-size:17px; margin:28px 0 8px; }} h3 {{ font-size:14px; margin:18px 0 4px; color:var(--ink2); }}
.sub, .note {{ color:var(--ink2); }} .sub {{ margin:2px 0 12px; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; }}
.tile {{ background:var(--tile); border-radius:8px; padding:10px 12px; }}
.lbl {{ font-size:12px; color:var(--ink2); }} .val {{ font-size:24px; font-weight:600; }} .note {{ font-size:12px; }}
.scroll {{ overflow-x:auto; }} .tbl {{ border-collapse:collapse; font-size:13px; white-space:nowrap; }}
.tbl th, .tbl td {{ border-bottom:1px solid var(--line); padding:4px 10px; text-align:left; }} .tbl th {{ color:var(--ink2); font-weight:600; }}
.bar {{ position:sticky; top:0; z-index:5; background:var(--surface); padding:6px 0 2px; }}
.dirs {{ display:flex; gap:8px; margin:6px 0 6px; }}
.grps {{ display:flex; gap:6px; margin:0 0 4px; }}
.grps button {{ flex:1; min-height:36px; border:1px solid var(--line); border-radius:8px; background:var(--tile);
  color:var(--ink); font:inherit; cursor:pointer; }}
.grps button[aria-pressed="true"] {{ background:var(--ink); color:var(--surface); border-color:var(--ink); font-weight:600; }}
details {{ margin:28px 0 8px; }} summary {{ font-weight:600; cursor:pointer; }}
.cams {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:10px; margin:8px 0 0; }}
.cam {{ margin:0; }} .cam img {{ width:100%; aspect-ratio:16/9; object-fit:cover; border-radius:8px; background:var(--tile); display:block; }}
.cam figcaption {{ font-size:12px; color:var(--ink2); margin-top:4px; }}
.dirs button {{ flex:1; min-height:48px; border:1px solid var(--line); border-radius:10px; background:var(--tile);
  color:var(--ink); font:inherit; font-size:15px; cursor:pointer; }}
.dirs button[aria-pressed="true"] {{ background:var(--ink); color:var(--surface); border-color:var(--ink); font-weight:600; }}
.dirs small {{ display:block; font-size:12px; opacity:.8; font-weight:400; }}
.days {{ display:flex; flex-wrap:wrap; gap:6px; margin:8px 0; }}
.days button {{ min-width:52px; min-height:40px; border:1px solid var(--line); border-radius:8px; background:var(--tile);
  color:var(--ink); font:inherit; cursor:pointer; }}
.days button[aria-pressed="true"] {{ background:var(--ink); color:var(--surface); border-color:var(--ink); font-weight:600; }}
.best {{ font-size:13px; margin:2px 0 10px; }}
</style></head><body><main>
<h1>Causeway bus times</h1>
<div class="sub">{subtitle}</div>
{body}
<p class="note">Bus ride only, checkpoint to checkpoint: immigration queues aren't included. From LTA bus GPS
positions, every minute.</p>
</main><script>
const SWAP = {json.dumps(swap)};
const orig = new Map();
function recolour(x, dark) {{
  if (typeof x === "string") return dark && SWAP[x] ? SWAP[x] : x;
  if (Array.isArray(x)) return x.map(v => recolour(v, dark));
  if (x && typeof x === "object") {{ const o = {{}}; for (const k in x) o[k] = recolour(x[k], dark); return o; }}
  return x;
}}
function paint() {{
  const t = document.documentElement.dataset.theme;
  const dark = t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  document.querySelectorAll(".js-plotly-plot").forEach(el => {{
    if (!orig.has(el)) orig.set(el, {{data: JSON.parse(JSON.stringify(el.data)), layout: JSON.parse(JSON.stringify(el.layout))}});
    const o = orig.get(el);
    Plotly.react(el, recolour(o.data, dark), recolour(o.layout, dark));
  }});
}}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", paint);
window.addEventListener("load", paint);
// pick a page (direction), a weekday and a bus; shared across pages, remembered, and can come from the link,
// e.g. causeway-bus-times#jb-sg,Sat,950
// Only a tap is remembered (not the default), so a later change of default still reaches returning visitors.
const KEYS = {{dir: "dir", day: "day", grp: "bus"}};  // "bus": renamed when the default became 160/170/170X
function pick(sel, attr, value, save) {{
  document.querySelectorAll(sel).forEach(el => {{ el.hidden = el.dataset[attr] !== value; }});
  document.querySelectorAll(`button[data-${{attr}}]`).forEach(b => b.setAttribute("aria-pressed", b.dataset[attr] === value));
  if (save) {{ try {{ localStorage.setItem(KEYS[attr], value); }} catch (e) {{}} }}
  window.dispatchEvent(new Event("resize"));  // charts drawn while hidden need their width
}}
const showDir = (d, save) => pick(".dirpage", "dir", d, save);
const showDay = (d, save) => pick(".day", "day", d, save);
const showGrp = (d, save) => pick(".grp", "grp", d, save);
document.addEventListener("click", e => {{
  const b = e.target.closest("button");
  if (b && b.dataset.dir) showDir(b.dataset.dir, true);
  if (b && b.dataset.day) showDay(b.dataset.day, true);
  if (b && b.dataset.grp) showGrp(b.dataset.grp, true);
}});
function first(attr, fallback) {{
  const ok = v => v && document.querySelector(`[data-${{attr}}="${{v}}"]`);
  const fromLink = location.hash.slice(1).split(",").find(ok);
  let saved = null;
  try {{ saved = localStorage.getItem(KEYS[attr]); }} catch (e) {{}}
  return fromLink || (ok(saved) ? saved : fallback);
}}
const dirBar = document.querySelector(".dirs"), dayBar = document.querySelector(".days"),
  grpBar = document.querySelector(".grps");
if (dirBar) showDir(first("dir", dirBar.dataset.default));
if (dayBar) showDay(first("day", dayBar.dataset.default));
if (grpBar) showGrp(first("grp", grpBar.dataset.default));
// live traffic cameras: the latest image of each, refreshed every minute while the page is open
async function loadCams() {{
  const boxes = document.querySelectorAll(".cam");
  if (!boxes.length) return;
  let cams = [];
  try {{ cams = (await (await fetch("{CAMERA_API}", {{cache: "no-store"}})).json()).items[0].cameras; }} catch (e) {{}}
  boxes.forEach(box => {{
    const c = cams.find(x => x.camera_id === box.dataset.cam), when = box.querySelector(".when");
    if (!c) {{ when.textContent = "not available right now"; return; }}
    const img = box.querySelector("img");
    if (img.getAttribute("src") !== c.image) {{ img.src = c.image; box.querySelector("a").href = c.image; }}
    when.textContent = "taken " + c.timestamp.slice(11, 16);
  }});
}}
loadCams();
setInterval(loadCams, 60000);
</script></body></html>"""


def load(db_path, since):
    con = sqlite3.connect(db_path, timeout=30)
    try:
        runs = pd.read_sql("SELECT polled_at, stop, ok FROM poll_runs WHERE polled_at >= ?", con, params=(since,))
        polls = pd.read_sql("SELECT * FROM polls WHERE polled_at >= ?", con, params=(since,))
    finally:
        con.close()
    return runs, polls


def plan_html(stats, dates, direction, default_day):
    """The "Plan your day" section of one direction's page: Mon-Sun buttons, a chart per weekday and bus."""
    out = [f"<h2>{PLAN_TITLE}</h2>",
           '<p class="note">Ride time by the hour you leave. Line: typical · band: most trips · public holidays '
           "left out.</p>",
           f'<div class="days" data-default="{default_day}">'
           + "".join(f'<button type="button" data-day="{w}" aria-pressed="false">{w}</button>' for w in WEEKDAYS)
           + "</div>"]
    for day in WEEKDAYS:
        used = dates.get((direction, day), [])
        based = (f"{len(used)} {day} · " + ", ".join(day_label(x)[4:] for x in used)) if used else f"No {day} data yet"
        out.append(f'<div class="day" data-day="{day}" hidden><p class="note">{based}</p>')
        for group in BUS_ORDER:
            out.append(f'<div class="grp" data-grp="{group_key(group)}">')
            if stats[(stats.direction == direction) & (stats.weekday == day) & (stats.group == group)].empty:
                out.append('<p class="note">Not enough trips yet.</p>')
            else:
                out.append(fig_html(plan_fig(stats, direction, day, (group,))))
                text = best_text(stats, direction, day, group)
                if text:
                    out.append(f'<p class="best">{text}</p>')
            out.append("</div>")
        out.append("</div>")
    return "\n".join(out)


def details_html(direction, tracks, all_trips, runs, days, now, today):
    """The last DASH_DAYS of one direction, per group: tiles, profile, heatmap, best/worst, waiting at the berth."""
    berth = BERTH[direction]
    gaps = T.poll_gaps(runs, direction)
    trips = all_trips[(all_trips.direction == direction) & all_trips.service_day.isin(days)].reset_index(drop=True)
    ok = trips[trips.ok]
    stats = T.slot_stats(ok[ok.day_type != "holiday"], by=("group", "day_type", "slot"))
    bridge = on_bridge(tracks, pd.Timestamp(now))
    waits = berth_wait(trips, direction)
    deps = departures_per_hour(trips, runs, direction)
    gap_today = bool(len(gaps)) and (gaps.gap_to.map(service_day) == today).any()
    body = [f"<h2>Last {len(days)} days</h2>"]
    for group in BUS_ORDER:
        body.append(f'<div class="grp" data-grp="{group_key(group)}">')
        if ok[ok.group == group].empty:
            body.append('<p class="note">No trips measured yet.</p></div>')
            continue
        tv = tiles(trips, stats, bridge, group, pd.Timestamp(now), gap_today)
        body.append('<div class="tiles">' + "".join(tile_html(k, *v) for k, v in tv.items()) + "</div>")
        body.append(f'<h3>{PROFILE_TITLE}</h3><p class="note">Dots: today.</p>')
        body.append(fig_html(profile_fig(ok, stats, group, today, direction=direction)))
        body.append(f'<h3>{HEATMAP_TITLE}</h3><p class="note">Darker = longer · ★ public holiday</p>')
        body.append(fig_html(heatmap_fig(ok, group, days, direction)))
        bw = best_worst(stats, group)
        if len(bw):
            bw = bw.rename(columns={"day_type": "Day type", "kind": "", "slot": "Leaving", "median": "Median min",
                                    "n": "Trips"})
            body.append(f"<h3>Best and worst times (06:00–23:00)</h3><div class='scroll'>{table_html(bw)}</div>")
        else:
            body.append('<h3>Best and worst times (06:00–23:00)</h3><p class="note">Not enough trips yet.</p>')
        seen = ", ".join(x for x in GROUPS[group] if x in DWELL_SERVICES[direction])
        hidden = ", ".join(x for x in GROUPS[group] if x not in DWELL_SERVICES[direction])
        note = "How long the bus waits before leaving" + (f" (bus {seen} only)" if hidden and seen else "") + "."
        body.append(f'<h3>Waiting at the {berth} berth</h3><p class="note">{note}</p>')
        if seen:
            body.append(fig_html(profile_fig(ok.iloc[0:0], waits, group, today, ytitle="bus waits (min)",
                                             height=280, direction=direction)))
        body.append('<p class="note">Buses seen leaving per hour (at least).</p>')
        body.append(fig_html(departures_fig(deps, group, direction)))
        body.append("</div>")
    return "\n".join(body)


def build(db_path, now):
    """The whole page as HTML. `now` is naive Singapore time."""
    today = service_day(now)
    plan_days = [(pd.Timestamp(today) - pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(PLAN_DAYS)]
    runs, polls = load(db_path, f"{plan_days[-1]} {SERVICE_DAY_START_HOUR:02d}:00:00")
    if runs.empty:
        return page("<p><b>No data yet.</b> The collector hasn't stored any polls in this window.</p>",
                    f"updated {now:%H:%M}")
    first = service_day(runs.polled_at.min())
    days = [d for d in plan_days[:DASH_DAYS] if d >= first]  # newest first

    # both directions over the plan window; the detailed sections use the last DASH_DAYS
    all_tracks = {d: T.link_tracks(T.gps_points(polls, direction=d)) for d in DIRECTIONS}
    found = [T.check_trips(T.find_trips(all_tracks[d]), T.poll_gaps(runs, d), d) for d in DIRECTIONS]
    all_trips = pd.concat([f for f in found if len(f)] or found[:1], ignore_index=True)
    all_trips["day_type"] = all_trips.service_day.map(day_type)
    all_trips["special"] = all_trips.service_day.map(special_day)
    plan, plan_dates = plan_stats(all_trips)

    weekday = pd.Timestamp(today).strftime("%a")
    body = ['<div class="bar"><div class="dirs" data-default="sg-jb">'
            + "".join(f'<button type="button" data-dir="{d}" aria-pressed="false">{title}'
                      f'<small>{DIRECTIONS[d]["name"]}</small></button>' for d, title in PAGES)
            + f'</div><div class="grps" data-default="{group_key(BUS_ORDER[0])}">'
            + "".join(f'<button type="button" data-grp="{group_key(g)}" aria-pressed="false">{group_name(g)}</button>'
                      for g in BUS_ORDER)
            + "</div></div>"]
    body.append("<h2>Live cameras</h2><div class=\"cams\">"
                + "".join(f'<figure class="cam" id="cam-{cid}" data-cam="{cid}"><a target="_blank" rel="noopener">'
                          f'<img alt="{name}"></a><figcaption>{name} · <span class="when">loading…</span>'
                          "</figcaption></figure>" for cid, name in CAMERAS)
                + '</div><p class="note">Cameras: LTA, via data.gov.sg</p>')
    for direction, title in PAGES:
        body.append(f'<section class="dirpage" data-dir="{direction}" hidden>')
        body.append(plan_html(plan, plan_dates, direction, weekday))
        body.append(details_html(direction, all_tracks[direction], all_trips, runs, days, now, today))
        body.append("</section>")
    quality_trips = all_trips[all_trips.service_day.isin(days)]
    body.append(f"<details><summary>Data quality</summary><div class='scroll'>"
                f"{table_html(quality(runs, quality_trips, days))}</div></details>")
    return page("\n".join(body), f"JB ⇄ Singapore checkpoints · updated {now:%H:%M}")


def main():
    start = time.time()
    now = datetime.now(SGT).replace(tzinfo=None, microsecond=0)
    OUT.write_text(build(DB_PATH, now), encoding="utf-8")
    print(f"{now} dashboard ok {time.time() - start:.1f}s", flush=True)


if __name__ == "__main__":
    main()
