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
from config import (DASH_DAYS, DIRECTIONS, GROUPS, MAX_TRIP_MIN, MIN_TRIPS_PER_SLOT, PLAN_DAYS,
                    SERVICE_DAY_START_HOUR, SLOT_MIN)

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "causeway.db"
OUT = ROOT / "dashboard.html"
SGT = ZoneInfo("Asia/Singapore")

PROFILE_TITLE = "Ride time by the hour you leave"
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

# Pop-up viewer for a camera image: zoom (buttons, pinch, wheel, double-tap) and drag to pan, without leaving the page.
# Plain strings (not f-strings), so the JavaScript needs no doubled braces.
LIGHTBOX_HTML = """<dialog class="lb" aria-label="Camera image">
<div class="cap"></div><button type="button" class="close" aria-label="Close">✕</button>
<div class="stage"><img alt=""></div>
<div class="tools"><button type="button" data-zoom="out" aria-label="Zoom out">－</button>
<button type="button" data-zoom="reset">Reset</button>
<button type="button" data-zoom="in" aria-label="Zoom in">＋</button></div>
</dialog>"""
LIGHTBOX_JS = """
const lb = document.querySelector("dialog.lb");
if (lb) {
  const img = lb.querySelector("img"), stage = lb.querySelector(".stage");
  let s = 1, tx = 0, ty = 0, startDist = 0, startS = 1, last = null;
  const pts = new Map();
  const apply = () => { img.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`; };
  const zoomTo = z => { s = Math.min(6, Math.max(1, z)); if (s === 1) { tx = 0; ty = 0; } apply(); };
  document.querySelectorAll(".camopen").forEach(b => b.addEventListener("click", () => {
    const src = b.querySelector("img").getAttribute("src");
    if (!src) return;  // not loaded yet
    img.src = src;
    img.alt = b.querySelector("img").alt;
    lb.querySelector(".cap").textContent = b.closest(".cam").querySelector("figcaption").textContent;
    zoomTo(1);
    lb.showModal();
  }));
  lb.querySelector(".close").addEventListener("click", () => lb.close());
  lb.addEventListener("click", e => { if (e.target === lb || (e.target === stage && s === 1)) lb.close(); });
  lb.querySelector(".tools").addEventListener("click", e => {
    const z = e.target.dataset.zoom;
    if (z) zoomTo(z === "in" ? s * 1.5 : z === "out" ? s / 1.5 : 1);
  });
  img.addEventListener("dblclick", () => zoomTo(s > 1 ? 1 : 2.5));
  stage.addEventListener("wheel", e => { e.preventDefault(); zoomTo(s * (e.deltaY < 0 ? 1.2 : 1 / 1.2)); },
                         {passive: false});
  // one finger / mouse drags; two fingers pinch
  stage.addEventListener("pointerdown", e => { pts.set(e.pointerId, e); stage.setPointerCapture(e.pointerId);
    last = {x: e.clientX, y: e.clientY};
    if (pts.size === 2) { const [a, b] = [...pts.values()];
      startDist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY); startS = s; } });
  stage.addEventListener("pointermove", e => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, e);
    if (pts.size === 2) { const [a, b] = [...pts.values()];
      zoomTo(startS * Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) / startDist); }
    else if (s > 1 && last) { tx += e.clientX - last.x; ty += e.clientY - last.y; apply(); }
    last = {x: e.clientX, y: e.clientY};
  });
  const up = e => { pts.delete(e.pointerId); last = null; };
  stage.addEventListener("pointerup", up);
  stage.addEventListener("pointercancel", up);
}
"""
# Services with GPS while they wait at the boarding berth. At JB (9-11 Oct data) 160 and 170X only switch it on
# when they leave, so their measured "wait" is just the last minute or two and is left out. At Woodlands all four
# report while waiting (12-13 Oct: median waits 8-13 min for every service).
DWELL_SERVICES = {"jb-sg": ("170", "950"), "sg-jb": ("160", "170", "170X", "950")}
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


def typical_trips(trips):
    """Kept trips on ordinary days: public holidays, their eves and last days of long weekends left out."""
    t = trips[trips.ok.astype(bool)]
    if "day_type" in t:
        t = t[t.day_type != "holiday"]
    if "special" in t:
        t = t[~t.special.astype(bool)]
    return t


def berth_wait(trips, direction="jb-sg"):
    """How long buses sit at the boarding berth before leaving: median etc. per weekday, group and slot."""
    t = typical_trips(trips)
    t = t[t.service.isin(DWELL_SERVICES[direction])]
    return T.slot_stats(t.assign(ride_min=t.wait_min, direction=direction), by=("direction", "weekday", "group", "slot"))


def departures_per_hour(trips, runs, direction="jb-sg"):
    """Average buses seen leaving the berth per hour, per group, weekday and slot (a lower bound).

    Averaged only over days that were polled in that slot, so a partial first day or today's future hours don't
    dilute it. Special days (holidays, eves, last days of long weekends) are left out of both counts.
    """
    cols = ["group", "weekday", "slot", "per_hour"]
    d = T.departures_seen(trips)
    if d.empty:
        return pd.DataFrame(columns=cols)
    d = d[~d.service_day.map(special_day).astype(bool)]
    polled = runs[(runs.ok == 1) & (runs.stop == DIRECTIONS[direction]["board"])]
    t = pd.to_datetime(polled.polled_at)
    cov = pd.DataFrame({"service_day": t.map(service_day), "slot": t.dt.floor(f"{SLOT_MIN}min").dt.strftime("%H:%M")})
    cov = cov.drop_duplicates()
    cov = cov[~cov.service_day.map(special_day).astype(bool)]
    cov = cov.assign(weekday=pd.to_datetime(cov.service_day).dt.strftime("%a"))
    n_days = cov.groupby(["weekday", "slot"]).size().rename("n_days")
    d = d.assign(weekday=pd.to_datetime(d.service_day).dt.strftime("%a"))
    d = d.groupby(["group", "weekday", "slot"]).departures.sum().reset_index().join(n_days, on=["weekday", "slot"])
    d["per_hour"] = d.departures / d.n_days * 60 / SLOT_MIN
    return d[cols]


def now_tile(trips, group, now):
    """'Now': median ride of this group's trips that arrived in the last hour."""
    ok = trips[trips.ok & (trips.group == group)]
    recent = ok[(ok.reached_b > now - timedelta(minutes=60)) & (ok.reached_b <= now)]
    window = f"{now - timedelta(minutes=60):%H:%M}–{now:%H:%M}"  # the hour before the page was built
    if len(recent):
        return f"{recent.ride_min.median():.0f} min", f"{len(recent)} trips, {window}"
    return "—", f"no trips {window}"


def typical_tile(stats, direction, group, weekday, now):
    """'Typical': median ride for the selected weekday at the current hour."""
    slot = pd.Timestamp(now).floor(f"{SLOT_MIN}min").strftime("%H:%M")
    s = stats[(stats.direction == direction) & (stats.weekday == weekday) & (stats.group == group)
              & (stats.slot == slot)]
    if len(s):
        return f"{s['median'].iloc[0]:.0f} min", f"{weekday} {slot} · {int(s.n.iloc[0])} trips"
    return "—", f"not enough {weekday} data at {slot} yet"


def plan_stats(trips):
    """Ride per direction, weekday, group and slot (special days left out), and the dates behind each direction x weekday."""
    t = typical_trips(trips)
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
                      hovermode="closest", dragmode=False,  # no zoom or pan; tap/hover still shows details
                      template="none")  # all colours are set here; Plotly's default theme added ~7 KB per chart
    ticks = list(range(5, 26, 2))
    fig.update_xaxes(fixedrange=True, automargin=True, title_standoff=8)  # automargin: room for labels and titles
    fig.update_yaxes(fixedrange=True, automargin=True, title_standoff=8)
    fig.update_xaxes(range=[4.5, 25.5], tickvals=ticks, ticktext=[f"{h % 24:02d}:00" for h in ticks],
                     gridcolor=LIGHT["grid"], linecolor=LIGHT["grid"], zeroline=False,
                     tickfont=dict(color=LIGHT["ink2"]), title_font=dict(color=LIGHT["ink2"]))
    fig.update_yaxes(gridcolor=LIGHT["grid"], linecolor=LIGHT["grid"], zeroline=False,
                     tickfont=dict(color=LIGHT["ink2"]), title_font=dict(color=LIGHT["ink2"]))
    return fig


def day_fig(stats, direction, weekday, group, ytitle="ride (min)", dots=None, height=300):
    """Median by hour for one weekday and bus (shaded: middle half), lines broken at hidden hours; dots = today."""
    key = GROUP_COLOURS[list(GROUPS).index(group)]
    fig = go.Figure()
    s = stats[(stats.direction == direction) & (stats.weekday == weekday) & (stats.group == group)].copy()
    if len(s):
        s["x"] = hour_x(pd.to_datetime(s.slot, format="%H:%M")) + SLOT_MIN / 120
        s = s.set_index("x").reindex([h + SLOT_MIN / 120 for h in HOURS]).rename_axis("x").reset_index()
        run = s["median"].isna().cumsum()  # contiguous stretches of shown slots share a number
        for _, seg in s[s["median"].notna()].groupby(run):
            fig.add_trace(go.Scatter(x=list(seg.x) + list(seg.x[::-1]), y=list(seg.p75) + list(seg.p25[::-1]),
                                     mode="none", fill="toself", fillcolor=LIGHT[f"{key}_band"], hoverinfo="skip",
                                     showlegend=False))
        fig.add_trace(go.Scatter(x=s.x, y=s["median"], mode="lines+markers", name=f"Typical {weekday}",
                                 line=dict(width=2, color=LIGHT[key]), marker=dict(size=8, color=LIGHT[key]),
                                 customdata=s[["slot", "p25", "p75", "n"]],
                                 hovertemplate=f"{weekday} %{{customdata[0]}}: median %{{y:.0f}} min<br>"
                                               "middle half %{customdata[1]:.0f}-%{customdata[2]:.0f}, "
                                               "%{customdata[3]} trips<extra></extra>"))
    has_dots = dots is not None and len(dots) > 0
    if has_dots:
        fig.add_trace(go.Scatter(x=hour_x(dots.left_a), y=dots.ride_min, mode="markers", name="Today",
                                 marker=dict(size=9, color=LIGHT["weekend"], line=dict(width=2, color=LIGHT["surface"])),
                                 customdata=list(zip(dots.service, dots.left_a.dt.strftime("%H:%M"))),
                                 hovertemplate="bus %{customdata[0]} left %{customdata[1]}: %{y:.0f} min<extra></extra>"))
    fig.update_yaxes(title_text=ytitle, rangemode="tozero")
    fig.update_xaxes(title_text=f"time the bus left the {BERTH[direction]} checkpoint")
    fig.update_layout(showlegend=has_dots)
    return base_layout(fig, height)


def departures_fig(deps, group, weekday, direction="jb-sg"):
    d = deps[(deps.group == group) & (deps.weekday == weekday)]
    colour = LIGHT[GROUP_COLOURS[list(GROUPS).index(group)]]
    fig = go.Figure(go.Bar(x=hour_x(pd.to_datetime(d.slot, format="%H:%M")) + SLOT_MIN / 120, y=d.per_hour, width=0.7,
                           marker=dict(color=colour), showlegend=False,
                           hovertemplate=f"{weekday} %{{x:.0f}}h: at least %{{y:.1f}} buses<extra></extra>"))
    fig.update_yaxes(title_text="buses per hour", rangemode="tozero")
    fig.update_xaxes(title_text=f"hour the bus left the {BERTH[direction]} checkpoint")
    return base_layout(fig, 220)


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
    return fig.to_html(full_html=False, include_plotlyjs=False,
                       config={"displayModeBar": False, "responsive": True, "doubleClick": False, "scrollZoom": False})


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
.tiles .day:not([hidden]) {{ display:contents; }}
.lbl {{ font-size:12px; color:var(--ink2); }} .val {{ font-size:24px; font-weight:600; }} .note {{ font-size:12px; }}
.bar {{ position:sticky; top:0; z-index:5; background:var(--surface); padding:6px 0 2px; }}
.dirs {{ display:flex; gap:8px; margin:6px 0 6px; }}
.grps {{ display:flex; gap:6px; margin:0 0 4px; }}
.grps button {{ flex:1; min-height:36px; border:1px solid var(--line); border-radius:8px; background:var(--tile);
  color:var(--ink); font:inherit; cursor:pointer; }}
.grps button[aria-pressed="true"] {{ background:var(--ink); color:var(--surface); border-color:var(--ink); font-weight:600; }}
.tabs {{ display:flex; gap:0; margin:14px 0 4px; border-bottom:1px solid var(--line); }}
.tabs button {{ flex:1; min-height:40px; border:0; border-bottom:3px solid transparent; background:none; color:var(--ink2);
  font:inherit; cursor:pointer; }}
.tabs button[aria-pressed="true"] {{ color:var(--ink); border-bottom-color:var(--ink); font-weight:600; }}
.cams {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:10px; margin:8px 0 0; }}
.cam {{ margin:0; }} .cam img {{ width:100%; aspect-ratio:16/9; object-fit:cover; border-radius:8px; background:var(--tile); display:block; }}
.cam figcaption {{ font-size:12px; color:var(--ink2); margin-top:4px; }}
.camopen {{ display:block; width:100%; padding:0; border:0; background:none; cursor:zoom-in; }}
dialog.lb {{ border:0; padding:0; margin:0; width:100vw; height:100vh; max-width:100vw; max-height:100vh;
  background:rgba(0,0,0,.5); color:#fff; overflow:hidden; }}
dialog.lb::backdrop {{ background:transparent; }}
.lb .stage {{ position:absolute; inset:44px 0 60px 0; display:flex; align-items:center; justify-content:center;
  overflow:hidden; touch-action:none; }}
.lb .stage img {{ max-width:100%; max-height:100%; user-select:none; -webkit-user-drag:none; cursor:grab; }}
.lb .cap {{ position:absolute; top:8px; left:8px; max-width:calc(100% - 76px); font-size:13px;
  background:rgba(0,0,0,.7); padding:5px 10px; border-radius:6px; }}
.lb .close {{ position:absolute; top:4px; right:8px; }}
.lb .tools {{ position:absolute; left:0; right:0; bottom:8px; display:flex; gap:8px; justify-content:center; }}
.lb button {{ min-width:48px; min-height:44px; border-radius:8px; border:1px solid #555; background:#222; color:#fff;
  font:inherit; font-size:16px; cursor:pointer; }}
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
<p class="note"><b>How it's measured:</b> every minute, LTA's live GPS positions of buses 160, 170, 170X and 950
are recorded, and each bus is timed from its last position at the boarding checkpoint to the next.</p>
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
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {{ paint(); markNow(); }});
window.addEventListener("load", () => {{ paint(); markNow(); setInterval(markNow, 60000); }});
// pick a page (direction), a weekday and a bus; shared across pages, remembered, and can come from the link,
// e.g. causeway-bus-times#jb-sg,Sat,950
// Only a tap is remembered (not the default), so a later change of default still reaches returning visitors.
const KEYS = {{dir: "dir", day: "day", grp: "bus"}};  // "bus": renamed when the default became 160/170/170X
// Singapore time from the viewer's clock (whatever their timezone): weekday of the service day (before 03:00 counts
// as the day before) and the hour as a number on the charts' axis (00:00-02:59 plots after 23:00).
function sgtNow() {{
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {{timeZone: "Asia/Singapore", weekday: "short",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23"}}).formatToParts(new Date()).map(x => [x.type, x.value]));
  const h = +p.hour, m = +p.minute, days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const i = days.indexOf(p.weekday);
  return {{day: h < {SERVICE_DAY_START_HOUR} ? days[(i + 6) % 7] : days[i],
          x: h + m / 60 + (h < {SERVICE_DAY_START_HOUR} ? 24 : 0), label: p.hour + ":" + p.minute}};
}}
const todayName = () => sgtNow().day;
// a dotted "now" line on every chart with hours along the bottom
function markNow() {{
  const n = sgtNow();
  document.querySelectorAll(".js-plotly-plot").forEach(el => {{
    const r = el.layout && el.layout.xaxis && el.layout.xaxis.range;
    if (!r || r[0] !== 4.5 || n.x < r[0] || n.x > r[1]) return;
    Plotly.relayout(el, {{shapes: [{{type: "line", xref: "x", yref: "paper", x0: n.x, x1: n.x, y0: 0, y1: 1,
      line: {{color: "#8a8984", width: 1.5, dash: "dot"}}}}],
      annotations: [{{x: n.x, xref: "x", y: 1, yref: "paper", text: "now " + n.label, showarrow: false,
      yanchor: "top", xanchor: "left", xshift: 4, font: {{size: 11, color: "#8a8984"}}}}]}});  // inside, clear of the legend
  }});
}}
function pick(sel, attr, value, save) {{
  document.querySelectorAll(sel).forEach(el => {{ el.hidden = el.dataset[attr] !== value; }});
  document.querySelectorAll(`button[data-${{attr}}]`).forEach(b => b.setAttribute("aria-pressed", b.dataset[attr] === value));
  if (save) {{ try {{ localStorage.setItem(KEYS[attr], value); }} catch (e) {{}} }}
  window.dispatchEvent(new Event("resize"));  // charts drawn while hidden need their width
}}
const showDir = (d, save) => pick(".dirpage", "dir", d, save);
const showDay = (d, save) => pick(".day", "day", d, save);
const showGrp = (d, save) => pick(".grp", "grp", d, save);
const showTab = d => pick(".panel", "tab", d, false);  // tabs always start on Waiting at the berth
document.addEventListener("click", e => {{
  const b = e.target.closest("button");
  if (b && b.dataset.dir) showDir(b.dataset.dir, true);
  if (b && b.dataset.day) showDay(b.dataset.day, true);
  if (b && b.dataset.grp) showGrp(b.dataset.grp, true);
  if (b && b.dataset.tab) showTab(b.dataset.tab);
}});
function first(attr, fallback, useSaved = true) {{
  const ok = v => v && document.querySelector(`[data-${{attr}}="${{v}}"]`);
  const fromLink = location.hash.slice(1).split(",").find(ok);
  let saved = null;
  if (useSaved) {{ try {{ saved = localStorage.getItem(KEYS[attr]); }} catch (e) {{}} }}
  return fromLink || (ok(saved) ? saved : fallback);
}}
const dirBar = document.querySelector(".dirs"), dayBar = document.querySelector(".days"),
  grpBar = document.querySelector(".grps");
if (dirBar) showDir(first("dir", dirBar.dataset.default));
if (dayBar) showDay(first("day", todayName(), false));  // today unless the link names a day
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
    if (img.getAttribute("src") !== c.image) img.src = c.image;
    when.textContent = "taken " + c.timestamp.slice(11, 16);
  }});
}}
loadCams();
setInterval(loadCams, 60000);
{LIGHTBOX_JS}
</script></body></html>"""


def load(db_path, since):
    con = sqlite3.connect(db_path, timeout=30)
    try:
        runs = pd.read_sql("SELECT polled_at, stop, ok FROM poll_runs WHERE polled_at >= ?", con, params=(since,))
        polls = pd.read_sql("SELECT * FROM polls WHERE polled_at >= ?", con, params=(since,))
    finally:
        con.close()
    return runs, polls


def page_html(direction, stats, dates, waits, deps, trips14, days, now, today):
    """One direction's page: a day selector, then per bus the tiles and the Ride time / Waiting tabs for that day."""
    berth, weekday_today = BERTH[direction], pd.Timestamp(today).strftime("%a")
    out = [f"<h2>{PLAN_TITLE}</h2>",
           '<p class="note">Select a day to see typical wait (past 4 weeks)</p>',
           f'<div class="days" data-default="{weekday_today}">'
           + "".join(f'<button type="button" data-day="{w}" aria-pressed="false">{w}</button>' for w in WEEKDAYS)
           + "</div>"]
    ok14 = trips14[trips14.ok]
    for group in BUS_ORDER:
        out.append(f'<div class="grp" data-grp="{group_key(group)}">')
        out.append('<div class="tiles">' + tile_html("Ride now", *now_tile(trips14, group, pd.Timestamp(now)))
                   + "".join(f'<div class="day" data-day="{w}" hidden>'
                             + tile_html("Typical ride", *typical_tile(stats, direction, group, w, now)) + "</div>"
                             for w in WEEKDAYS) + "</div>")
        out.append(f'<div class="tabs"><button type="button" data-tab="wait" aria-pressed="true">Waiting at the '
                   f'{berth} berth</button><button type="button" data-tab="ride" aria-pressed="false">Ride time'
                   "</button></div>")
        # Ride time: the selected weekday, then the last 14 days
        out.append(f'<div class="panel" data-tab="ride" hidden><h3>{PROFILE_TITLE}</h3>')
        for w in WEEKDAYS:
            used = dates.get((direction, w), [])
            based = (f"{len(used)} {w} · " + ", ".join(day_label(x)[4:] for x in used)) if used else f"No {w} data yet"
            out.append(f'<div class="day" data-day="{w}" hidden><p class="note">{based}</p>')
            if stats[(stats.direction == direction) & (stats.weekday == w) & (stats.group == group)].empty:
                out.append('<p class="note">Not enough trips yet.</p>')
            else:
                dots = ok14[(ok14.group == group) & (ok14.service_day == today)] if w == weekday_today else None
                out.append(fig_html(day_fig(stats, direction, w, group, dots=dots)))
                text = best_text(stats, direction, w, group)
                if text:
                    out.append(f'<p class="best">{text}</p>')
            out.append("</div>")
        out.append(f'<h3>{HEATMAP_TITLE}</h3><p class="note">Darker = longer · ★ public holiday</p>')
        out.append(fig_html(heatmap_fig(ok14, group, days, direction))
                   if len(ok14[ok14.group == group]) else '<p class="note">No trips measured yet.</p>')
        out.append("</div>")
        # Waiting at the berth: the selected weekday
        seen = ", ".join(x for x in GROUPS[group] if x in DWELL_SERVICES[direction])
        hidden = ", ".join(x for x in GROUPS[group] if x not in DWELL_SERVICES[direction])
        note = "How long the bus waits before leaving" + (f" (bus {seen} only)" if hidden and seen else "") + "."
        out.append(f'<div class="panel" data-tab="wait"><h3>Waiting at the {berth} berth</h3>'
                   f'<p class="note">{note}</p>')
        for w in WEEKDAYS:
            out.append(f'<div class="day" data-day="{w}" hidden>')
            if seen and len(waits[(waits.weekday == w) & (waits.group == group)]):
                out.append(fig_html(day_fig(waits, direction, w, group, ytitle="bus waits (min)", height=260)))
            else:
                out.append(f'<p class="note">Not enough {w} data yet.</p>')
            if len(deps[(deps.weekday == w) & (deps.group == group)]):
                out.append('<p class="note">Buses seen leaving per hour (at least).</p>')
                out.append(fig_html(departures_fig(deps, group, w, direction)))
            out.append("</div>")
        out.append("</div></div>")  # panel, group
    return "\n".join(out)


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

    # both directions over the plan window (4 weeks); the heatmap and "Now" use the last DASH_DAYS
    all_tracks = {d: T.link_tracks(T.gps_points(polls, direction=d)) for d in DIRECTIONS}
    found = [T.check_trips(T.find_trips(all_tracks[d]), T.poll_gaps(runs, d), d) for d in DIRECTIONS]
    all_trips = pd.concat([f for f in found if len(f)] or found[:1], ignore_index=True)
    all_trips["day_type"] = all_trips.service_day.map(day_type)
    all_trips["special"] = all_trips.service_day.map(special_day)
    plan, plan_dates = plan_stats(all_trips)

    body = ['<div class="bar"><div class="dirs" data-default="sg-jb">'
            + "".join(f'<button type="button" data-dir="{d}" aria-pressed="false">{title}'
                      f'<small>{DIRECTIONS[d]["name"]}</small></button>' for d, title in PAGES)
            + f'</div><div class="grps" data-default="{group_key(BUS_ORDER[0])}">'
            + "".join(f'<button type="button" data-grp="{group_key(g)}" aria-pressed="false">{group_name(g)}</button>'
                      for g in BUS_ORDER)
            + "</div></div>"]
    body.append("<h2>Live cameras</h2><div class=\"cams\">"
                + "".join(f'<figure class="cam" id="cam-{cid}" data-cam="{cid}"><button type="button" class="camopen" '
                          f'aria-label="Enlarge: {name}"><img alt="{name}"></button><figcaption>{name} · '
                          '<span class="when">loading…</span></figcaption></figure>' for cid, name in CAMERAS)
                + '</div><p class="note">Cameras: LTA, via data.gov.sg · tap to enlarge</p>' + LIGHTBOX_HTML)
    for direction, title in PAGES:
        mine = all_trips[all_trips.direction == direction]
        body.append(f'<section class="dirpage" data-dir="{direction}" hidden>')
        body.append(page_html(direction, plan, plan_dates, berth_wait(mine, direction),
                              departures_per_hour(mine, runs, direction),
                              mine[mine.service_day.isin(days)].reset_index(drop=True), days, now, today))
        body.append("</section>")
    return page("\n".join(body), f"JB ⇄ Singapore checkpoints · updated {now:%H:%M}")


def main():
    start = time.time()
    now = datetime.now(SGT).replace(tzinfo=None, microsecond=0)
    OUT.write_text(build(DB_PATH, now), encoding="utf-8")
    print(f"{now} dashboard ok {time.time() - start:.1f}s", flush=True)


if __name__ == "__main__":
    main()
