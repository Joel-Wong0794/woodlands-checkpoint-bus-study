// Causeway bus times V2: downloads data.json (written every 15 min by dashboard.py on the VM) and draws the page.
// No framework: plain DOM. All maths is in Python; this file only shows numbers. Text always goes in with
// textContent / text nodes (never innerHTML), so nothing in the data can turn into markup.
import * as L from "./logic.js";

const DATA_URL = "data.json";
const SCHEMA = 1;  // dashboard.SCHEMA_VERSION this page understands
const CAMERA_API = "https://api.data.gov.sg/v1/transport/traffic-images";
const DEFAULT_DIR = "sg-jb";  // as V1: the day starts by going to JB
const TITLES = { ride: "Ride across Causeway, by hour you leave", heat: "Last 14 days by hour" };

const $ = sel => document.querySelector(sel);
const store = {  // per-viewer conveniences only; private mode or blocked storage just means no memory
  get: k => { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } },
};

// h("p", {class: "note"}, "text", child): build an element; strings become text nodes
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
    el.append(typeof kid === "string" ? document.createTextNode(kid) : kid);
  return el;
}

let data = null, version = null;
const state = { dir: null, grp: null, day: null, hour: null, tab: "wait" };
let charts = [];  // live uPlot instances, destroyed before each redraw

// ---------- loading ----------

function showMsg(text) {
  const m = $("#msg");
  m.hidden = !text;
  m.textContent = text || "";
}

async function load() {
  if (document.hidden || document.querySelector("dialog[open]")) return;  // don't redraw under the viewer
  let r;
  try { r = await fetch(DATA_URL, { cache: "no-cache" }); } catch (e) {
    if (!data) showMsg("Couldn't load the data. Check your connection; the page retries every minute.");
    return;
  }
  if (!r.ok) { if (!data) showMsg("No data yet."); return; }
  const v = r.headers.get("etag") || r.headers.get("last-modified");
  if (data && v && v === version) {  // unchanged since the last check; a new hour still moves the strip on
    if (nowParts().hour !== renderedHour) render();
    return;
  }
  let next;
  try { next = await r.json(); } catch (e) { return; }
  if (next.schema_version !== SCHEMA) { showMsg("This page is out of date. Reload to update it."); return; }
  version = v;
  const first = !data;
  data = next;
  if (first) initState();
  render();
}

// ---------- state: from the link, else the last tap, else defaults ----------

const dirKeys = () => Object.keys(data.directions);
const grpKeys = () => data.group_order;  // not Object.keys: JS would put "950" before "160-170-170X"
const nowParts = () => { const p = L.sgtParts(); return { ...p, hh: L.defaultHour(p) }; };

function initState() {
  const link = L.parseHash(location.hash, { dirs: dirKeys(), grps: grpKeys() }), now = nowParts();
  const saved = (k, list) => { const v = store.get(k); return list.includes(v) ? v : null; };
  // same localStorage keys as V1 ("dir", "bus"): both pages live on storage.googleapis.com, so a choice carries over
  state.dir = link.dir || saved("dir", dirKeys()) || (dirKeys().includes(DEFAULT_DIR) ? DEFAULT_DIR : dirKeys()[0]);
  state.grp = link.grp || saved("bus", grpKeys()) || grpKeys()[0];
  state.day = link.day || now.day;      // today unless the link names a day (not remembered)
  state.hour = link.hour || now.hh;     // the current hour unless the link names one (not remembered)
  state.tab = link.tab || "wait";
}

function setState(change, save) {
  Object.assign(state, change);
  if (save) for (const [k, key] of [["dir", "dir"], ["grp", "bus"]]) if (k in change) store.set(key, change[k]);
  const now = nowParts();
  history.replaceState(null, "", L.formatHash(state, { day: now.day, hour: now.hh }));
  render();
}

// ---------- rendering ----------

function render() {
  charts.forEach(u => u.destroy());
  charts = [];
  if (!data) return;
  const updated = data.generated_at.slice(11, 16);
  $("#updated").textContent = `JB ⇄ Singapore checkpoints · updated ${updated}`;
  if (!dirKeys().length) { showMsg("No data yet. The collector hasn't stored any polls in this window."); return; }
  showMsg("");
  const page = data.directions[state.dir], g = page.groups[state.grp];
  renderBar();
  renderStrip(g);
  renderCams();
  renderPlanControls(page);
  renderTypical(page, g);
  renderPanel(page, g);
}

function pressed(container, attr, value) {
  container.querySelectorAll(`button[data-${attr}]`).forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset[attr] === value)));
}

function renderBar() {
  const dirs = $("#dirs"), grps = $("#grps");
  if (!dirs.children.length) {
    dirKeys().forEach(d => dirs.append(h("button", { type: "button", "data-dir": d },
      data.directions[d].title, h("small", { text: data.directions[d].name }))));
    grpKeys().forEach(k => grps.append(h("button", { type: "button", "data-grp": k },
      data.directions[dirKeys()[0]].groups[k].name)));
  }
  pressed(dirs, "dir", state.dir);
  pressed(grps, "grp", state.grp);
}

function tile(label, value, note) {
  return h("div", { class: "tile" }, h("div", { class: "lbl", text: label }), h("div", { class: "val", text: value }),
    h("p", { class: "note", text: note }));
}

// Now and next 3 hours: now = live (the hour before the build), later hours = the usual for today's weekday.
// The maths of which figure goes where is in logic.js (stripBlocks, tested); this only draws it.
let renderedHour = null;
function renderStrip(g) {
  const now = nowParts();
  renderedHour = now.hour;
  const warn = L.warningText(data.calendar, state.dir);
  $("#warn").hidden = !warn;
  $("#warn").textContent = warn;
  $("#strip").replaceChildren(...L.stripBlocks(g, { hour: now.hour, day: now.day, rules: data.rules }).map(b => {
    const cls = ["blk", b.status ? b.status.key : "none", b.rough ? "rough" : ""].filter(Boolean).join(" ");
    const src = b.kind === "live" ? "live" : b.kind === "usual" ? "usually" : "";
    const ride = b.ride === null ? "—" : String(Math.round(b.ride));
    const label = [b.label, src, b.ride === null ? b.basis : `${ride} min ride, ${b.status.word}`,
      b.gap === null ? "" : `a bus every ${Math.round(b.gap)} min`, b.ride === null ? "" : `based on ${b.basis}`]
      .filter(Boolean).join(", ");
    return h("button", { type: "button", class: cls, "data-strip": b.hh, disabled: !L.HOURS.includes(b.hh),
      "aria-label": label },
      h("span", { class: "t", text: b.label }), h("span", { class: "src", text: src || " " }),
      h("span", { class: "big" }, ride, b.ride === null ? null : h("small", { text: "min" })),
      h("span", { class: "st", text: b.status ? b.status.word : " " }),
      h("span", { class: "ev", text: b.gap === null ? "" : `every ${Math.round(b.gap)} min` }),
      h("span", { class: "ba", text: b.basis }));
  }));
}

let camsBuilt = false;
function renderCams() {
  if (camsBuilt) return;
  camsBuilt = true;
  $("#cams").replaceChildren(...data.cameras.map(c => h("figure", { class: "cam", "data-cam": c.id },
    h("button", { type: "button", class: "camopen", "aria-label": `Enlarge: ${c.name}` }, h("img", { alt: c.name })),
    h("figcaption", {}, `${c.name} · `, h("span", { class: "when", text: "loading…" })))));
  loadCams();
  setInterval(loadCams, 60000);
}

function renderPlanControls(page) {
  const days = $("#days"), sel = $("#hour");
  if (!days.children.length) {
    L.WEEKDAYS.forEach(w => days.append(h("button", { type: "button", "data-day": w, "aria-label": L.DAY_NAMES[w] }, w)));
    L.HOURS.forEach(hh => sel.append(h("option", { value: hh, text: L.hourLabel(hh) })));
  }
  pressed(days, "day", state.day);
  sel.value = state.hour;
  pressed($("#tabs"), "tab", state.tab);
  $("#tabs").querySelector('[data-tab="wait"]').textContent = `Waiting at the ${page.berth} berth`;
  renderCoverage(page);
}

function renderCoverage(page) {
  const box = $("#cover"), rules = data.rules, ph = new Set(data.ph_dates);
  box.querySelector("summary").textContent = L.coverageSummary(page.coverage, rules);
  const grid = box.querySelector(".covgrid");
  grid.replaceChildren(...L.WEEKDAYS.map(w => h("span", { class: "wd", text: w })),
    ...L.calendarCells(page.coverage, data.service_day).map(c => {
      if (!c) return h("span", { class: "pad" });
      const level = L.coverLevel(c.share, rules), star = ph.has(c.date);
      const label = `${L.dayLabel(c.date)} · ${c.share === null ? "not collected yet"
        : Math.round(c.share * 100) + "% collected"}${star ? " · public holiday" : ""}`;
      return h("span", { class: `cell ${level}${c.date === data.service_day ? " today" : ""}`, title: label,
        "aria-label": label }, `${c.day}${star ? "★" : ""}`);
    }));
}

// Typical <day>, <hour>: from the past 4 weeks of that weekday
function renderTypical(page, g) {
  const slot = (g.typical[state.day] || {})[state.hour] || {};
  const gap = !g.gap_shown || !slot.gap ? ["—", "not enough data yet"]
    : [L.minText(slot.gap.median), L.countText(slot.gap, "buses")];
  const ride = slot.ride ? [L.minText(slot.ride.median), L.countText(slot.ride, "trips")] : ["—", "not enough data yet"];
  $("#typical").replaceChildren(
    h("p", { class: "tilehead", text: `Typical ${L.DAY_NAMES[state.day]}, ${L.hourLabel(state.hour)}` }),
    h("div", { class: "tiles" }, tile("Bus every", ...gap), tile("Ride", ...ride)));
}

function renderPanel(page, g) {
  const box = $("#panel"), day = state.day, byHour = g.typical[day];
  const kids = [];
  if (state.tab === "ride") {
    kids.push(h("h3", { text: TITLES.ride }), h("p", { class: "note", text: L.datesNote(page.dates[day], day) }));
    const med = L.series(byHour, "ride", "median");
    if (med.some(v => v !== null)) {
      const isToday = day === nowParts().day && day === data.weekday;
      kids.push(chartBox(el => lineChart(el, { byHour, kind: "ride", best: g.best[day], worst: g.worst[day],
        dots: isToday ? g.today : [], ytitle: "minutes across Causeway", count: "trips",
        headline: m => `Median ride time across Causeway: ${Math.round(m)} min` }), `Median ride by hour, ${L.DAY_NAMES[day]}`));
      kids.push(legend(day, g, isToday && g.today.length));
      if (g.best_text[day]) kids.push(h("p", { class: "fastslow", text: g.best_text[day] }));
    } else kids.push(h("p", { class: "note", text: "Not enough trips yet." }));
    kids.push(heatmapDetails(page, g));
  } else {
    kids.push(h("h3", { text: `Waiting at the ${page.berth} berth` }));
    if (!g.gap_shown) kids.push(h("p", { class: "note", text: "Not enough data yet." }));
    else if (L.series(byHour, "gap", "median").some(v => v !== null))
      kids.push(chartBox(el => lineChart(el, { byHour, kind: "gap", best: [], worst: [], dots: [],
        ytitle: "minutes between buses", count: "gaps between buses", height: 240,
        headline: m => `A bus every ${Math.round(m)} min` }), `Minutes between buses by hour, ${L.DAY_NAMES[day]}`));
    else kids.push(h("p", { class: "note", text: `Not enough ${day} data yet.` }));
    const deps = g.departures[day];
    if (deps) {
      const det = h("details", {}, h("summary", { text: "More detail" }),
        h("p", { class: "note", text: "Buses seen leaving per hour (at least)." }));
      let drawn = false;
      det.addEventListener("toggle", () => {  // draw when opened: a closed fold has no width
        if (det.open && !drawn) { drawn = true; det.append(chartBox(el => barChart(el, deps), "Buses seen leaving per hour")); }
      });
      kids.push(det);
    }
  }
  box.replaceChildren(...kids);
}

function legend(day, g, hasDots) {
  const item = (cls, text) => h("span", {}, h("i", { class: `sw ${cls}` }), text);
  return h("div", { class: "legend" }, item("line", `Typical ${day}`),
    g.best[day] ? item("best", "Best times") : null, g.worst[day] ? item("worst", "Worst times") : null,
    hasDots ? item("dots", "Today") : null);
}

// ---------- heatmap (plain HTML grid) ----------

function heatmapDetails(page, g) {
  const hm = g.heatmap, ph = new Set(data.ph_dates);
  const max = Math.max(0, ...hm.median.flat().filter(v => v !== null));
  const cap = h("p", { class: "note", "aria-live": "polite", text: "Tap a square for its figure." });
  const grid = h("div", { class: "heat", role: "grid", "aria-label": TITLES.heat });
  grid.append(h("span", {}), ...hm.hours.map((hh, i) => h("span", { class: "hh", text: i % 3 === 0 ? hh : "" })));
  hm.days.forEach((d, r) => {
    grid.append(h("span", { class: "rl", text: L.dayLabel(d) + (ph.has(d) ? " ★" : "") }));
    hm.hours.forEach((hh, c) => {
      const v = hm.median[r][c], lvl = L.heatLevel(v, max);
      const text = v === null ? `${L.dayLabel(d)}, ${L.slotLabel(hh)}: no trips`
        : `${L.dayLabel(d)}, ${L.slotLabel(hh)}: ${L.minText(v)}, from ${hm.n[r][c]} trips`;
      grid.append(h("span", { class: `c${lvl === null ? "" : " l" + lvl}`, title: text, "data-t": text }));
    });
  });
  grid.addEventListener("click", e => {
    const c = e.target.closest(".c");
    if (!c) return;
    grid.querySelectorAll(".sel").forEach(x => x.classList.remove("sel"));
    c.classList.add("sel");
    cap.textContent = c.dataset.t;
  });
  const key = h("div", { class: "heatkey" }, "shorter", ...[0, 1, 2, 3, 4].map(i =>
    h("i", { style: `background:var(--ramp${i})` })), "longer");
  return h("details", {}, h("summary", { text: "More detail" }), h("h3", { text: TITLES.heat }),
    h("p", { class: "note", text: "Darker = longer · ★ public holiday" }),
    hm.days.length ? [grid, key, cap] : h("p", { class: "note", text: "No trips measured yet." }));
}

// ---------- charts (uPlot) ----------

const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const X = L.HOURS.map(hh => L.hourX(hh) + 0.5);  // each hour's point sits in the middle of its hour
// hour labels every 2 hours, every 4 on a phone (narrower than 520 px) so they don't overlap
const ticks = width => (width < 520 ? [5, 9, 13, 17, 21, 25] : [5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25]);

function chartBox(draw, label) {
  const el = h("div", { class: "chart", role: "img", "aria-label": label });
  requestAnimationFrame(() => { if (window.uPlot && el.isConnected) draw(el); });  // after layout: needs a width
  return el;
}

function baseOpts(el, height, ytitle, xtitle) {
  const ink2 = css("--ink2"), line = css("--line"), font = "12px system-ui, -apple-system, sans-serif";
  const axis = { stroke: ink2, font, labelFont: font, grid: { stroke: line, width: 1 }, ticks: { show: false } };
  return {
    width: el.clientWidth, height,
    legend: { show: false },
    cursor: { y: false, drag: { x: false, y: false }, points: { show: false } },
    scales: { x: { time: false, range: [4.5, 25.5] },
      y: { range: (u, min, max) => [0, Math.max(5, Math.ceil(((max || 0) * 1.2) / 5) * 5)] } },
    axes: [{ ...axis, splits: u => ticks(u.width), values: (u, s) => s.map(v => `${String(v % 24).padStart(2, "0")}:00`),
      label: xtitle, labelSize: 22, size: 30 },
      { ...axis, label: ytitle, labelSize: 22, size: 36 }],
  };
}

function rectAt(u, x0, x1, fill) {
  const ctx = u.ctx, l = u.valToPos(x0, "x", true), r = u.valToPos(x1, "x", true);
  ctx.fillStyle = fill;
  ctx.fillRect(l, u.bbox.top, r - l, u.bbox.height);
}

function lineChart(el, o) {
  const med = L.series(o.byHour, o.kind, "median"), p25 = L.series(o.byHour, o.kind, "p25"),
    p75 = L.series(o.byHour, o.kind, "p75"), n = L.series(o.byHour, o.kind, "n"),
    days = L.series(o.byHour, o.kind, "days"), est = L.series(o.byHour, o.kind, "est");
  const accent = css("--accent"), band = css("--band"), ink = css("--ink"), surface = css("--surface");
  const dpr = uPlot.pxRatio, page = data.directions[state.dir];
  const xtitle = `time the bus left the ${page.berth} checkpoint`;
  const tip = h("div", { class: "tip", hidden: true, "aria-hidden": "true" });
  const dotsX = o.dots.map(d => L.hourX(d.left.slice(0, 2)) + +d.left.slice(3) / 60);
  const opts = baseOpts(el, o.height || 280, o.ytitle, xtitle);
  opts.series = [{}, { stroke: accent, width: 2, points: { show: true, size: 7, fill: accent, stroke: accent } },
    { stroke: "transparent", width: 0, points: { show: false } }, { stroke: "transparent", width: 0, points: { show: false } }];
  opts.bands = [{ series: [2, 3], fill: band }];
  opts.hooks = {
    // behind the lines: best (green) and worst (red) hours, then the selected hour (grey)
    drawClear: [u => {
      for (const [list, colour] of [[o.best, css("--best")], [o.worst, css("--worst")]])
        for (const slot of list || []) { const x = L.hourX(slot.slice(0, 2)); rectAt(u, x, x + 1, colour); }
      const hx = L.hourX(state.hour);
      rectAt(u, hx, hx + 1, css("--hourband"));
    }],
    // on top: each hour's median printed above its point, today's rides as dots, and a dotted "now" line
    draw: [u => {
      const ctx = u.ctx;
      ctx.save();
      ctx.font = `${11 * dpr}px system-ui, sans-serif`; ctx.textAlign = "center"; ctx.fillStyle = ink;
      med.forEach((m, i) => { if (m !== null) ctx.fillText(String(Math.round(m)),
        u.valToPos(X[i], "x", true), u.valToPos(m, "y", true) - 8 * dpr); });
      ctx.fillStyle = css("--dots"); ctx.strokeStyle = surface; ctx.lineWidth = 2 * dpr;
      o.dots.forEach((d, i) => { ctx.beginPath();
        ctx.arc(u.valToPos(dotsX[i], "x", true), u.valToPos(d.ride, "y", true), 4.5 * dpr, 0, 2 * Math.PI);
        ctx.fill(); ctx.stroke(); });
      const now = L.sgtParts();
      if (state.day === now.day && now.x >= 4.5 && now.x <= 25.5) {
        const x = u.valToPos(now.x, "x", true);
        ctx.strokeStyle = css("--nowline"); ctx.lineWidth = 1.5 * dpr; ctx.setLineDash([3 * dpr, 3 * dpr]);
        ctx.beginPath(); ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, u.bbox.top + u.bbox.height); ctx.stroke();
        // label on the right of the line, or on its left near the end of the day (so it isn't cut off)
        const right = x < u.bbox.left + u.bbox.width * 0.8;
        ctx.setLineDash([]); ctx.fillStyle = css("--nowline"); ctx.textAlign = right ? "left" : "right";
        ctx.fillText(`now ${now.label}`, x + (right ? 4 : -4) * dpr, u.bbox.top + 12 * dpr);
      }
      ctx.restore();
    }],
    // tap or hover: that hour's details, worded as V1
    setCursor: [u => {
      const i = u.cursor.idx;
      if (i === null || i === undefined || med[i] === null) { tip.hidden = true; return; }
      const hh = L.HOURS[i], today = o.dots.filter(d => d.left.slice(0, 2) === hh);
      const lines = [[`${state.day} ${L.slotLabel(hh)}`, true], [o.headline(med[i])],
        [`From ${n[i]} ${o.count}${est[i] ? `, ${est[i]} estimated` : ""}${days[i] ? ` · ${days[i]} day${days[i] === 1 ? "" : "s"}` : ""}`],
        [`Usually ${Math.round(p25[i])}–${Math.round(p75[i])} min`]];
      if (today.length) lines.push([`Today: ${today.map(d => `${Math.round(d.ride)}`).join(", ")} min`]);
      tip.replaceChildren(...lines.map(([t, bold]) => h("div", {}, bold ? h("b", { text: t }) : t)));
      tip.hidden = false;
      const left = u.cursor.left + u.over.offsetLeft;
      tip.style.left = `${Math.max(0, Math.min(left + 12, el.clientWidth - tip.offsetWidth))}px`;
    }],
  };
  const u = new uPlot(opts, [X, med, p75, p25], el);
  el.append(tip);
  track(u, el);
}

function barChart(el, deps) {
  const y = L.HOURS.map(hh => (deps[hh] === undefined ? null : deps[hh]));
  const page = data.directions[state.dir], accent = css("--accent");
  const tip = h("div", { class: "tip", hidden: true, "aria-hidden": "true" });
  const opts = baseOpts(el, 210, "buses per hour", `hour the bus left the ${page.berth} checkpoint`);
  opts.series = [{}, { stroke: accent, fill: accent, width: 0, paths: uPlot.paths.bars({ size: [0.7, 40] }),
    points: { show: false } }];
  opts.hooks = { setCursor: [u => {
    const i = u.cursor.idx;
    if (i === null || i === undefined || y[i] === null) { tip.hidden = true; return; }
    tip.replaceChildren(h("div", {}, h("b", { text: `${state.day} ${L.slotLabel(L.HOURS[i])}` })),
      h("div", { text: `At least ${y[i].toFixed(1)} buses left` }));
    tip.hidden = false;
    tip.style.left = `${Math.max(0, Math.min(u.cursor.left + u.over.offsetLeft + 12, el.clientWidth - tip.offsetWidth))}px`;
  }] };
  const u = new uPlot(opts, [X, y], el);
  el.append(tip);
  track(u, el);
}

function track(u, el) {
  charts.push(u);
  new ResizeObserver(() => { if (el.clientWidth && el.clientWidth !== u.width) u.setSize({ width: el.clientWidth, height: u.height }); })
    .observe(el);
}

// ---------- cameras ----------

async function loadCams() {
  let cams = [];
  try { cams = (await (await fetch(CAMERA_API, { cache: "no-store" })).json()).items[0].cameras; } catch (e) { /* offline */ }
  document.querySelectorAll(".cam").forEach(box => {
    const c = cams.find(x => x.camera_id === box.dataset.cam), when = box.querySelector(".when");
    if (!c) { when.textContent = "not available right now"; return; }
    const img = box.querySelector("img");
    if (img.getAttribute("src") !== c.image) img.src = c.image;
    when.textContent = "taken " + c.timestamp.slice(11, 16);
  });
}

// camera viewer: tap to open, pinch / wheel / buttons to zoom, drag to pan (as V1)
function lightbox() {
  const lb = $("dialog.lb"), img = lb.querySelector("img"), stage = lb.querySelector(".stage");
  let s = 1, tx = 0, ty = 0, startDist = 0, startS = 1, last = null;
  const pts = new Map();
  const apply = () => { img.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`; };
  const zoomTo = z => { s = Math.min(6, Math.max(1, z)); if (s === 1) { tx = 0; ty = 0; } apply(); };
  $("#cams").addEventListener("click", e => {
    const b = e.target.closest(".camopen");
    if (!b) return;
    const src = b.querySelector("img").getAttribute("src");
    if (!src) return;  // not loaded yet
    img.src = src;
    img.alt = b.querySelector("img").alt;
    lb.querySelector(".cap").textContent = b.closest(".cam").querySelector("figcaption").textContent;
    zoomTo(1);
    lb.showModal();
  });
  lb.querySelector(".close").addEventListener("click", () => lb.close());
  lb.addEventListener("click", e => { if (e.target === lb || (e.target === stage && s === 1)) lb.close(); });
  lb.querySelector(".tools").addEventListener("click", e => {
    const z = e.target.dataset.zoom;
    if (z) zoomTo(z === "in" ? s * 1.5 : z === "out" ? s / 1.5 : 1);
  });
  img.addEventListener("dblclick", () => zoomTo(s > 1 ? 1 : 2.5));
  stage.addEventListener("wheel", e => { e.preventDefault(); zoomTo(s * (e.deltaY < 0 ? 1.2 : 1 / 1.2)); }, { passive: false });
  stage.addEventListener("pointerdown", e => {
    pts.set(e.pointerId, e); stage.setPointerCapture(e.pointerId); last = { x: e.clientX, y: e.clientY };
    if (pts.size === 2) { const [a, b] = [...pts.values()]; startDist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY); startS = s; }
  });
  stage.addEventListener("pointermove", e => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, e);
    if (pts.size === 2) { const [a, b] = [...pts.values()]; zoomTo(startS * Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) / startDist); }
    else if (s > 1 && last) { tx += e.clientX - last.x; ty += e.clientY - last.y; apply(); }
    last = { x: e.clientX, y: e.clientY };
  });
  const up = e => { pts.delete(e.pointerId); last = null; };
  stage.addEventListener("pointerup", up);
  stage.addEventListener("pointercancel", up);
}

// ---------- theme ----------

function themeButton() {
  const btn = $("#theme"), root = document.documentElement;
  const label = () => {
    const dark = root.dataset.theme !== "light";
    btn.textContent = dark ? "Light" : "Dark";
    btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  };
  label();
  btn.addEventListener("click", () => {
    root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
    store.set("theme", root.dataset.theme);
    label();
    render();  // charts read their colours from the theme tokens
  });
}

// ---------- events ----------

document.addEventListener("click", e => {
  const b = e.target.closest("button");
  if (!b || !data) return;
  if (b.dataset.dir) setState({ dir: b.dataset.dir }, true);
  else if (b.dataset.grp) setState({ grp: b.dataset.grp }, true);
  else if (b.dataset.day) setState({ day: b.dataset.day });
  else if (b.dataset.tab) setState({ tab: b.dataset.tab });
  else if (b.dataset.strip) {  // an hour in the strip: show it in Plan your day (today, that hour, the ride chart)
    setState({ day: nowParts().day, hour: b.dataset.strip, tab: "ride" });
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    $("#typical").scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  }
});
$("#hour").addEventListener("change", e => setState({ hour: e.target.value }));
// a share link opened in a tab that already shows the page: apply it (replaceState in setState doesn't fire this)
window.addEventListener("hashchange", () => {
  if (!data) return;
  Object.assign(state, L.parseHash(location.hash, { dirs: dirKeys(), grps: grpKeys() }));
  render();
});

themeButton();
lightbox();
load();
setInterval(load, 60000);  // a new data.json is uploaded every 15 min; checking every minute keeps "now" fresh
document.addEventListener("visibilitychange", load);
