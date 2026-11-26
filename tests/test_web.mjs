// Tests for web/logic.js (the V2 page's pure functions). Run from the project root: node --test tests/test_web.mjs
// (or node --test "tests/*.mjs"). Built-in Node test runner: no npm packages.
import { test } from "node:test";
import assert from "node:assert/strict";
import * as L from "../web/logic.js";

test("Singapore time and the 03:00 service day, whatever the viewer's timezone", () => {
  // 2026-10-09 is a Friday. 18:20 SGT = 10:20 UTC
  assert.deepEqual(L.sgtParts(new Date("2026-10-09T10:20:00Z")),
    { day: "Fri", hour: 18, minute: 20, x: 18 + 20 / 60, label: "18:20" });
  // Sat 00:30 SGT still belongs to Friday's service day and plots after 23:00
  const p = L.sgtParts(new Date("2026-10-09T16:30:00Z"));
  assert.equal(p.day, "Fri"); assert.equal(p.x, 24.5);
  // Sat 03:10 SGT starts Saturday
  assert.equal(L.sgtParts(new Date("2026-10-09T19:10:00Z")).day, "Sat");
});

test("default hour is the current hour, 05 when no buses run", () => {
  assert.equal(L.defaultHour({ hour: 18 }), "18");
  assert.equal(L.defaultHour({ hour: 0 }), "00");
  assert.equal(L.defaultHour({ hour: 3 }), "05");
});

test("share link: read any order, ignore junk", () => {
  const opts = { dirs: ["sg-jb", "jb-sg"], grps: ["160-170-170X", "950"] };
  assert.deepEqual(L.parseHash("#jb-sg,Sat,950,18,ride", opts),
    { dir: "jb-sg", day: "Sat", grp: "950", hour: "18", tab: "ride" });
  assert.deepEqual(L.parseHash("#18,xx,<b>,Sun", opts), { day: "Sun", hour: "18" });
  assert.deepEqual(L.parseHash("#04", opts), {});  // 04:00 is not on the hour list
  assert.deepEqual(L.parseHash("", opts), {});
});

test("share link: write only what differs from the defaults", () => {
  const now = { day: "Fri", hour: "18" };
  assert.equal(L.formatHash({ dir: "jb-sg", grp: "950", day: "Fri", hour: "18", tab: "wait" }, now), "#jb-sg,950");
  assert.equal(L.formatHash({ dir: "jb-sg", grp: "950", day: "Sat", hour: "07", tab: "ride" }, now),
    "#jb-sg,Sat,950,ride,07");
});

test("hour labels", () => {
  assert.equal(L.hourLabel("18"), "18:00–19:00");
  assert.equal(L.hourLabel("23"), "23:00–00:00");
  assert.equal(L.slotLabel("13"), "1300–1400hrs");
  assert.equal(L.slotLabel("23"), "2300–0000hrs");
  assert.deepEqual([L.hourX("05"), L.hourX("23"), L.hourX("00"), L.hourX("01")], [5, 23, 24, 25]);
  assert.equal(L.HOURS.length, 21);
  assert.deepEqual([L.HOURS[0], L.HOURS.at(-1)], ["05", "01"]);
});

test("series over the hour axis, null where hidden", () => {
  const byHour = { "05": { ride: { median: 8, p25: 6, p75: 9, n: 4, days: 2 } }, "00": { gap: { median: 3 } } };
  const s = L.series(byHour, "ride", "median");
  assert.equal(s.length, 21); assert.equal(s[0], 8); assert.equal(s[1], null); assert.equal(s[19], null);
  assert.equal(L.series(byHour, "gap", "median")[19], 3);
  assert.equal(L.series(undefined, "ride", "median").every(v => v === null), true);
});

test("minutes and counts as text", () => {
  assert.equal(L.minText(7.5), "8 min");
  assert.equal(L.minText(null), "—");
  assert.equal(L.countText({ n: 14, days: 3 }, "trips"), "from 14 trips, 3 days");
  assert.equal(L.countText({ n: 1, days: 1 }, "trips"), "from 1 trip, 1 day");
  assert.equal(L.countText({ n: 12, est: 5 }, "trips"), "from 12 trips, 5 estimated");
});

test("dates behind a weekday", () => {
  assert.equal(L.dayLabel("2026-10-09"), "Fri 9 Oct");
  assert.equal(L.datesNote(["2026-10-09", "2026-10-16"], "Fri"), "2 Fri · 9 Oct, 16 Oct");
  assert.equal(L.datesNote([], "Sat"), "No Sat data yet");
});

test("coverage levels and summary", () => {
  const rules = { cover_full: 0.95, cover_part: 0.5 };
  assert.deepEqual([0.95, 0.94, 0.5, 0.49, null].map(s => L.coverLevel(s, rules)), ["full", "part", "part", "miss", "none"]);
  assert.equal(L.coverageSummary({ a: 1, b: 0.6, c: null }, rules), "Data collected: 1 of 2 days complete");
  assert.equal(L.coverageSummary({ c: null }, rules), "Data collected: none yet");
});

test("coverage calendar: whole weeks Monday to Sunday", () => {
  // window Wed 7 Oct .. Fri 9 Oct; today Fri 9 Oct -> one week Mon 5 .. Sun 11
  const cells = L.calendarCells({ "2026-10-07": 1, "2026-10-08": 1, "2026-10-09": 0.5 }, "2026-10-09");
  assert.equal(cells.length, 7);
  assert.deepEqual(cells.map(c => c && c.date), [null, null, "2026-10-07", "2026-10-08", "2026-10-09", null, null]);
  assert.equal(cells[4].day, 9);
});

test("heatmap colour step", () => {
  assert.equal(L.heatLevel(null, 30), null);
  assert.equal(L.heatLevel(0, 30), 0);
  assert.equal(L.heatLevel(30, 30), 4);
  assert.equal(L.heatLevel(15, 30), 2);
});

// ---------- step 3: the "now + 3 hours" strip ----------

const RULES = { quick_max: 10, slow_min: 25, min_days: 3 };

test("status from the minutes shown (rounded), the same bands every hour", () => {
  const key = m => (L.statusOf(m, RULES) || {}).key;
  // 9.4 shows "9 min" -> Quick; 9.5 shows "10 min" -> Moderate; 25 Moderate; 25.5 shows "26" -> Slow
  assert.deepEqual([9.4, 9.5, 10, 25, 25.4, 25.5, 40].map(key),
    ["quick", "moderate", "moderate", "moderate", "moderate", "slow", "slow"]);
  assert.equal(L.statusOf(9, RULES).word, "Quick");
  assert.equal(L.statusOf(null, RULES), null);
});

const slot = (median, days, gap) => ({ ride: { median, n: 10, days }, ...(gap ? { gap: { median: gap, n: 8, days } } : {}) });
const G = {
  gap_shown: true,
  live: { window: ["17:20", "18:20"], ride: { median: 22, n: 7 }, gap: { median: 6, n: 9 } },
  typical: { Fri: { "18": slot(21, 4, 6), "19": slot(17, 4, 7), "20": slot(12, 2, 9), "00": slot(5, 3), "01": slot(4, 3) } },
};

test("strip at 18:20 on a Friday: live now, then the usual for 19, 20, 21", () => {
  const b = L.stripBlocks(G, { hour: 18, day: "Fri", rules: RULES });
  assert.deepEqual(b.map(x => [x.label, x.hh, x.kind]),
    [["Now", "18", "live"], ["19:00", "19", "usual"], ["20:00", "20", "usual"], ["21:00", "21", "none"]]);
  assert.deepEqual([b[0].ride, b[0].gap, b[0].status.key, b[0].basis], [22, 6, "moderate", "7 trips"]);
  assert.deepEqual([b[1].ride, b[1].gap, b[1].status.key, b[1].basis, b[1].rough], [17, 7, "moderate", "4 days", false]);
  assert.deepEqual([b[2].basis, b[2].rough, b[2].status.key], ["2 days · rough", true, "moderate"]);  // < 3 days
  assert.deepEqual([b[3].ride, b[3].basis], [null, "Not enough data yet"]);
});

test("strip after midnight stays on the service day, and ends when buses stop", () => {
  const b = L.stripBlocks({ ...G, live: { ...G.live, ride: null, gap: null } }, { hour: 0, day: "Fri", rules: RULES });
  assert.deepEqual(b.map(x => [x.label, x.kind, x.ride]),
    [["Now", "usual", 5], ["01:00", "usual", 4], ["02:00", "none", null], ["03:00", "none", null]]);
  assert.equal(b[0].basis, "3 days");                   // no live trips: the usual figure stands in for now
  assert.equal(b[2].basis, "No buses");
  assert.equal(b[0].status.key, "quick");
});

test("no gap where V1 hides it", () => {
  const b = L.stripBlocks({ ...G, gap_shown: false }, { hour: 18, day: "Fri", rules: RULES });
  assert.deepEqual(b.map(x => x.gap), [null, null, null, null]);
});

test("holiday warning line", () => {
  const cal = (o = {}) => ({ ph: false, ph_eve: false, ph_last_day: false, ...o });
  assert.equal(L.warningText(cal(), "jb-sg"), "");
  assert.equal(L.warningText(cal({ ph: true }), "sg-jb"), "Holiday today: may be busier");
  assert.equal(L.warningText(cal({ ph_eve: true }), "sg-jb"), "Holiday tomorrow: may be busier");
  assert.equal(L.warningText(cal({ ph_last_day: true }), "jb-sg"), "Last day of a long weekend: the return may be busier");
  assert.equal(L.warningText(cal({ ph_last_day: true }), "sg-jb"), "");  // the return crush is JB -> SG only
});
