// Pure helpers for the V2 page: no DOM, no network, so Node can test them (tests/test_web.mjs).
// app.js does the drawing; everything here only turns numbers and strings into other numbers and strings.

export const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
export const DAY_NAMES = { Mon: "Monday", Tue: "Tuesday", Wed: "Wednesday", Thu: "Thursday", Fri: "Friday",
  Sat: "Saturday", Sun: "Sunday" };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const SERVICE_DAY_START_HOUR = 3;  // a service day runs 03:00 -> 02:59 (config.py)
// the hour axis, as on V1's charts: 05:00 -> 01:00 the next morning (dashboard.HOURS = range(5, 26))
export const HOURS = Array.from({ length: 21 }, (_, i) => String((5 + i) % 24).padStart(2, "0"));
export const TABS = ["wait", "ride"];

const pad = n => String(n).padStart(2, "0");

// Singapore time from the viewer's clock, whatever their timezone. Before 03:00 counts as the day before
// (the service day), and plots after 23:00 on the hour axis (x = 24.5 for 00:30).
export function sgtParts(date = new Date()) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Singapore", weekday: "short",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(date).map(x => [x.type, x.value]));
  const hour = +p.hour, minute = +p.minute, i = WEEKDAYS.indexOf(p.weekday), early = hour < SERVICE_DAY_START_HOUR;
  return { day: early ? WEEKDAYS[(i + 6) % 7] : WEEKDAYS[i], hour, minute,
    x: hour + minute / 60 + (early ? 24 : 0), label: `${pad(hour)}:${pad(minute)}` };
}

// the hour picker's default: the current hour, or 05 while no buses run (02:00-04:59)
export function defaultHour(parts) {
  const h = pad(parts.hour);
  return HOURS.includes(h) ? h : "05";
}

// Share link, as V1: values in any order, e.g. #jb-sg,Sat,950,18,ride. Anything unknown is ignored, so a
// hand-edited or old link can't inject anything (values are only ever compared against these lists).
export function parseHash(hash, { dirs, grps }) {
  const out = {};
  for (const v of (hash || "").replace(/^#/, "").split(",")) {
    if (dirs.includes(v)) out.dir = v;
    else if (WEEKDAYS.includes(v)) out.day = v;
    else if (grps.includes(v)) out.grp = v;
    else if (TABS.includes(v)) out.tab = v;
    else if (HOURS.includes(v)) out.hour = v;
  }
  return out;
}

// The link for the current view: direction and bus always; day, tab and hour only when not the defaults
// (today, Waiting, the current hour), so a shared link opened later still starts on "now".
export function formatHash(state, now) {
  const parts = [state.dir, state.day !== now.day ? state.day : "", state.grp,
    state.tab !== "wait" ? state.tab : "", state.hour !== now.hour ? state.hour : ""];
  return "#" + parts.filter(Boolean).join(",");
}

const nextHour = hh => pad((+hh + 1) % 24);
export const hourLabel = hh => `${hh}:00–${nextHour(hh)}:00`;     // "18:00–19:00" (the hour picker)
export const slotLabel = hh => `${hh}00–${nextHour(hh)}00hrs`;    // "1800–1900hrs" (hover text, as V1)
export const hourX = hh => (+hh < SERVICE_DAY_START_HOUR ? +hh + 24 : +hh);  // position on the hour axis

// One figure per hour on the axis (null where there's not enough data), e.g. the median ride for each hour.
// byHour = data.json typical[<weekday>], kind = "ride" | "gap", field = "median" | "p25" | "p75" | "n".
export function series(byHour, kind, field) {
  return HOURS.map(h => {
    const s = byHour && byHour[h] && byHour[h][kind];
    return s && s[field] !== undefined && s[field] !== null ? s[field] : null;
  });
}

export const minText = m => (m === null || m === undefined ? "—" : `${Math.round(m)} min`);

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
// "from 14 trips, 3 days" (how much the figure rests on); "trips" -> "trip" for 1
export function countText(stat, what) {
  const unit = what.endsWith("s") ? what.slice(0, -1) : what;
  let text = `from ${plural(stat.n, unit)}`;
  if (stat.est) text += `, ${stat.est} estimated`;
  else if (stat.days) text += `, ${plural(stat.days, "day")}`;
  return text;
}

// "2026-10-09" -> "Fri 9 Oct" (dates are calendar dates; read as UTC so the viewer's timezone can't shift them)
function parseDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}
export function dayLabel(iso) {
  const d = parseDate(iso);
  return `${WEEKDAYS[(d.getUTCDay() + 6) % 7]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}
// the line under a chart: which days the typical comes from, e.g. "2 Fri · 9 Oct, 16 Oct"
export function datesNote(dates, day) {
  if (!dates || !dates.length) return `No ${day} data yet`;
  return `${dates.length} ${day} · ` + dates.map(d => dayLabel(d).slice(4)).join(", ");
}

// Data collected calendar (as V1's coverage strip): complete / partial / missing per day
export function coverLevel(share, rules) {
  if (share === null || share === undefined) return "none";
  return share >= rules.cover_full ? "full" : share >= rules.cover_part ? "part" : "miss";
}
export function coverageSummary(coverage, rules) {
  const shown = Object.values(coverage).filter(s => s !== null);
  if (!shown.length) return "Data collected: none yet";
  return `Data collected: ${shown.filter(s => coverLevel(s, rules) === "full").length} of ${shown.length} days complete`;
}
// whole weeks Monday -> Sunday from the first day of the window to the week of today; null = outside the window
export function calendarCells(coverage, today) {
  const days = Object.keys(coverage).sort();
  if (!days.length) return [];
  const first = parseDate(days[0]), last = parseDate(today), dayMs = 86400000;
  const start = first.getTime() - ((first.getUTCDay() + 6) % 7) * dayMs;
  const end = last.getTime() + (6 - (last.getUTCDay() + 6) % 7) * dayMs;
  const cells = [];
  for (let t = start; t <= end; t += dayMs) {
    const d = new Date(t), iso = d.toISOString().slice(0, 10);
    cells.push(iso in coverage ? { date: iso, day: d.getUTCDate(), share: coverage[iso] } : null);
  }
  return cells;
}

// heatmap: which of 5 colour steps (0 = shortest), scaled from 0 to the longest median on the grid
export function heatLevel(v, max, steps = 5) {
  if (v === null || v === undefined) return null;
  return max > 0 ? Math.min(steps - 1, Math.floor((v / max) * steps * 0.9999)) : 0;
}

// ---------- the "now + 3 hours" strip (step 3) ----------

// Quick / Moderate / Slow from the ride minutes as shown (rounded), the same bands for every hour and direction:
// < quick_max Quick, up to slow_min Moderate, above it Slow (data.json rules: 10 and 25)
export function statusOf(min, rules) {
  if (min === null || min === undefined) return null;
  const m = Math.round(min);
  if (m < rules.quick_max) return { key: "quick", word: "Quick" };
  if (m <= rules.slow_min) return { key: "moderate", word: "Moderate" };
  return { key: "slow", word: "Slow" };
}

// Four blocks: now (live: the last hour's trips; if none, the usual figure for this hour), then the next 3 hours
// (the usual figure for today's weekday at that hour, past 4 weeks). g = one bus group from data.json.
// Hours belong to the service day, so at 00:30 "now" is still the evening's weekday; past 01:00 there are no buses.
export function stripBlocks(g, { hour, day, rules }) {
  const byHour = (g.typical && g.typical[day]) || {};
  const blocks = [];
  for (let i = 0; i < 4; i++) {
    const hh = pad((hour + i) % 24);
    const b = { hh, label: i === 0 ? "Now" : `${hh}:00`, kind: "none", ride: null, gap: null, status: null,
      basis: "", rough: false };
    if (i === 0 && g.live && g.live.ride) {
      Object.assign(b, { kind: "live", ride: g.live.ride.median, basis: `${g.live.ride.n} trips`,
        gap: g.gap_shown && g.live.gap ? g.live.gap.median : null });
    } else if (!HOURS.includes(hh)) {
      b.basis = "No buses";
    } else {
      const s = byHour[hh];
      if (s && s.ride) {
        const rough = s.ride.days < rules.min_days;
        Object.assign(b, { kind: "usual", ride: s.ride.median, rough,
          basis: plural(s.ride.days, "day") + (rough ? " · rough" : "") });
      } else b.basis = "Not enough data yet";
      if (g.gap_shown && s && s.gap) b.gap = s.gap.median;
    }
    b.status = statusOf(b.ride, rules);
    blocks.push(b);
  }
  return blocks;
}

// one short line above the strip on holidays; the strip itself still shows the normal weekday figures
export function warningText(calendar, dir) {
  if (calendar.ph) return "Holiday today: may be busier";
  if (calendar.ph_eve) return "Holiday tomorrow: may be busier";
  if (calendar.ph_last_day && dir === "jb-sg") return "Last day of a long weekend: the return may be busier";
  return "";
}
