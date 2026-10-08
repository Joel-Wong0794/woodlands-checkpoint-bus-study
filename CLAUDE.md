# Causeway Bus Crossing Time — Build Spec

> Put this file in an empty folder as `CLAUDE.md`, open Claude Code there, and say:
> **"Read CLAUDE.md and do Stage 0. Stop after each stage so I can review."**

## 1. Goal

Measure how long buses **160 / 170 / 170X / 950** take to go from **bus stop 46219 (Johor Bahru Checkpoint)** to **bus stop 46109 (Woodlands Checkpoint)**, by 15-minute slot, and show it on a **rolling 14-day dashboard**.

This is a **learning project**. Keep it small, readable and correct.

## 2. Ground rules for Claude Code

| Rule | Detail |
|---|---|
| Don't over-engineer | Plain functions. No classes, ORMs, frameworks, Docker, async or message queues |
| File budget | Only the files in section 5. Ask before adding any other file |
| Dependencies | `requests`, `pandas`, `plotly`, `holidays`, `pytest`. Everything else from the stdlib |
| Pure logic, thin I/O | Parsing, arrival detection, pairing and stats are **pure functions** (data in → data out) so they can be tested without network or DB |
| Tests first for logic | For `trips.py`, write the tests before the code |
| Never guess facts | Don't invent stop coordinates, holiday dates or API fields. Get them from the API, the `holidays` package, or ask me |
| Stop between stages | Do not start the next stage until I say "next" |
| Teach as you go | After each stage, give a short table: what was built, why, and one thing for me to try |
| Secrets | LTA key comes from env var `LTA_ACCOUNT_KEY`. Never hard-code or commit it. `.env` is in `.gitignore` |

## 3. Decisions already made

| Topic | Decision |
|---|---|
| Data source | LTA DataMall only. `v3/BusArrival` (updated every 20 s by LTA) |
| Polling | Every **1 min**, 24/7. **2 calls per run**: one per stop, without the `ServiceNo` filter. Filter to our 4 services locally |
| Runtime | Google Cloud **e2-micro** VM (Always Free, US region), Debian, `cron` |
| Database | **SQLite** at `data/causeway.db`, WAL mode |
| What we store | Raw parsed snapshots only. Arrivals, trips and stats are **recomputed** from raw data each time, so a logic bug can be fixed and rerun |
| Trip definition | Arrival at 46219 → arrival at 46109, same service. Includes the bus's wait at 46219 |
| Matching method | No bus ID exists in the API. Within one service and one service-day, the n-th bus to reach 46219 is paired with the n-th bus to reach 46109 (first in, first out) |
| Service-day | A day runs 03:00 → 02:59. No buses run at 3am, so every bus that reaches 46219 within a service-day also reaches 46109 within it |
| Dashboard | Static HTML built every 15 min on the VM and uploaded to a public Cloud Storage bucket. No web server |
| Time | All stored times are Singapore time, as text `YYYY-MM-DD HH:MM:SS` (no offset) |
| Out of scope (now) | Rainfall, ML models, alerts, other routes, the opposite direction |

## 4. DataMall facts (from the API User Guide v6.10)

| Item | Detail |
|---|---|
| Headers | `AccountKey: <key>`, `accept: application/json` |
| Bus Arrival | `https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival?BusStopCode=46219` |
| Bus Arrival fields (per `NextBus`, `NextBus2`, `NextBus3`) | `OriginCode, DestinationCode, EstimatedArrival` (ISO with +08:00), `Monitored` (1 = from GPS, 0 = from timetable), `Latitude, Longitude` (strings, `"0.0"` when not monitored), `VisitNumber, Load` (SEA/SDA/LSD), `Feature, Type` (SD/DD/BD) |
| Blank buses | `NextBus2`/`NextBus3` can have every field as `""`. A service can be missing entirely when nothing is running |
| No data at all | When buses aren't running or the API is under maintenance, there may be no response body at all |
| Other datasets | `BusRoutes`, `BusStops`, `BusServices`: 500 records per call, page through with `?$skip=500`, `1000`, … |
| `PV/ODBus?Date=YYYYMM` | Returns a download link (expires in 15 min) to a zipped CSV: `YEAR_MONTH, DAY_TYPE, TIME_PER_HOUR, PT_TYPE, ORIGIN_PT_CODE, DESTINATION_PT_CODE, TOTAL_TRIPS`. Last 3 months only |

## 5. File layout

```
causeway-bus/
├─ CLAUDE.md
├─ README.md
├─ requirements.txt
├─ .gitignore              # .env, data/*.db, logs/, dashboard.html, .venv/
├─ config.py               # stops, services, coordinates, thresholds (section 6)
├─ probe.py                # Stage 0: one-off checks against the API
├─ collector.py            # Stage 1: run by cron every minute
├─ trips.py                # Stage 3: detect arrivals, pair trips, slot stats (pure)
├─ calendar_flags.py       # Stage 5: public/school holiday flags (pure)
├─ dashboard.py            # Stage 4: builds dashboard.html
├─ data/
│  ├─ causeway.db          # gitignored
│  └─ school_holidays.csv  # I fill this in by hand
├─ deploy/
│  └─ crontab.txt
└─ tests/
   ├─ fixtures/            # real API responses saved by probe.py
   ├─ simulate.py          # fake poll generator with known true trips
   ├─ test_collector.py
   ├─ test_trips.py
   ├─ test_calendar.py
   └─ test_dashboard.py
```

## 6. `config.py`

| Name | Value | Note |
|---|---|---|
| `STOP_A` | `"46219"` | JB Checkpoint (start) |
| `STOP_B` | `"46109"` | Woodlands Checkpoint (end) |
| `SERVICES` | `("160", "170", "170X", "950")` | Trim in Stage 0 if a service fails the checks |
| `STOP_COORDS` | Filled in from the `BusStops` output in Stage 0 | Don't guess |
| `GAP_SEC` | `180` | Polls further apart than this are a gap, and no arrival is inferred across them |
| `DUE_WITHIN_SEC` | `120` | A bus only counts as arrived if its ETA was within 2 min of the last poll that showed it |
| `SINGLE_BUS_JUMP_SEC` | `300` | Used when only one bus was listed (see Stage 3) |
| `MIN_TRIP_MIN`, `MAX_TRIP_MIN` | `3`, `240` | A trip outside this range makes the service-day suspect |
| `SERVICE_DAY_START_HOUR` | `3` | |
| `DASH_DAYS` | `14` | |
| `MIN_TRIPS_PER_SLOT` | `3` | Slots with fewer trips are not plotted |

---

## 7. Stages

### Stage 0 — Probe the API (laptop, ~30 min)

Goal: prove the data exists before building anything. Run it **during the day**, when buses are running.

`probe.py` does the following and prints a summary table:

| # | Check | Pass if |
|---|---|---|
| 1 | `BusArrival` for 46219 and 46109. Save the raw JSON to `tests/fixtures/busarrival_<stop>.json` | Each service in `SERVICES` appears at both stops |
| 2 | For each stop × service: count buses with `Monitored = 1` and with real lat/lon | 46219 has `Monitored = 1` for the service. **This is the critical check** |
| 3 | `BusRoutes` (page through with `$skip`), filtered to `SERVICES` | 46219 is immediately followed by 46109 in `StopSequence`, in the same `Direction`. Print the 3 stops either side |
| 4 | `BusStops` (page through) | Print the coordinates for 46219 and 46109, which I copy into `config.py` |
| 5 | `BusServices` for `SERVICES` | Print the AM/PM peak and off-peak frequencies, for reference |
| 6 | `PV/ODBus` for the latest month: download, unzip, filter `ORIGIN_PT_CODE=46219, DESTINATION_PT_CODE=46109` | Print trips per hour for weekday vs weekend. Save to `data/od_46219_46109.csv` |

Finish with a **GO / NO-GO** for each service:
- GO = checks 1–3 pass.
- If **no** service passes check 2 at 46219, **stop and tell me**. The design needs to change.

**Done when:** the table is printed, fixtures are saved, `config.py` has real coordinates, and `SERVICES` lists only GO services.

### Stage 1 — Collector (laptop)

`collector.py`, run once per minute.

| Item | Spec |
|---|---|
| Steps | For each stop: call `BusArrival` (timeout 15 s) → parse → insert. One stop failing must not stop the other |
| `parse_bus_arrival(payload, stop, polled_at, services) -> list[dict]` | **Pure.** One row per service per listed bus (`seq` 1–3). Blank strings → `None`. `"0.0"` lat/lon → `None`. Convert `EstimatedArrival` to SGT text. Skip services not in `services` |
| Exit code | 1 if any call failed, else 0 |
| Output | One log line per run, e.g. `2026-10-09 07:31:02 46219 ok 4svc 11rows \| 46109 ok 4svc 12rows` |
| DB setup | `CREATE TABLE IF NOT EXISTS` on every run. `PRAGMA journal_mode=WAL`. Connection timeout 30 s |

Tables:

```sql
CREATE TABLE IF NOT EXISTS poll_runs (      -- one row per API call, even if it failed or was empty
  polled_at  TEXT NOT NULL,                 -- SGT time of this call
  stop       TEXT NOT NULL,
  ok         INTEGER NOT NULL,              -- 1 = HTTP 200 and parsed
  n_services INTEGER,                       -- how many of OUR services were listed
  error      TEXT,
  PRIMARY KEY (polled_at, stop)
);

CREATE TABLE IF NOT EXISTS polls (          -- one row per listed bus
  polled_at  TEXT NOT NULL,
  stop       TEXT NOT NULL,
  service    TEXT NOT NULL,
  seq        INTEGER NOT NULL,              -- 1 = NextBus, 2 = NextBus2, 3 = NextBus3
  eta        TEXT,
  monitored  INTEGER,
  lat REAL, lon REAL,
  load TEXT, bus_type TEXT,
  origin TEXT, destination TEXT,
  PRIMARY KEY (polled_at, stop, service, seq)
);
```

`poll_runs` is needed to tell *"we polled and no bus was listed"* (normal at night) apart from *"we didn't poll"* (a gap).

Tests (`tests/test_collector.py`):

| Test | Asserts |
|---|---|
| Real fixture from Stage 0 | Row count matches the listed buses for our services. Every `eta` parses as SGT |
| Blank `NextBus3` (all `""`) | Produces no row for seq 3 and doesn't crash |
| `Monitored = 0` with `"0.0"` coordinates | lat/lon are `None` |
| Service not in `SERVICES` | Ignored |
| Empty payload / no `Services` key | Returns `[]` |

**Done when:** tests pass, and running the collector for 30 min on my laptop (via a simple loop or a local cron) gives about 60 `poll_runs` rows and sensible `polls` rows. Show me a sample query.

### Stage 2 — Deploy the collector to Google Cloud (start collecting early)

Data takes 2 weeks to build up, so deploy before writing the analysis. Walk me through each step and explain each GCP concept briefly.

| # | Step | Note |
|---|---|---|
| 1 | Create a project, link billing, and set a **US$1 budget alert** | The free tier still needs a billing account |
| 2 | Create a VM: **e2-micro**, region **us-central1** (or us-west1 / us-east1), Debian 12, **30 GB standard persistent disk** | These choices keep it in the Always Free tier. A *balanced* disk is not free |
| 3 | At creation, set Access scopes → **Storage: Read Write** | Needed for the Stage 4 upload. Changing it later means stopping the VM |
| 4 | SSH in. `sudo timedatectl set-timezone Asia/Singapore`. `sudo apt install -y git python3-venv sqlite3` | Timezone only affects log readability. The code uses zoneinfo |
| 5 | Clone the repo, create a venv, `pip install -r requirements.txt` | |
| 6 | Create `.env` with `LTA_ACCOUNT_KEY=...`, `chmod 600 .env` | |
| 7 | Run `probe.py` check 1 on the VM | Confirms DataMall answers requests from a US server. If it doesn't, stop and tell me |
| 8 | Install the crontab from `deploy/crontab.txt` | See below |

`deploy/crontab.txt` (Stage 2 line only for now):

```cron
* * * * * cd $HOME/causeway-bus && set -a && . ./.env && set +a && flock -n /tmp/collector.lock .venv/bin/python collector.py >> logs/collector.log 2>&1
```

`flock` stops two runs from overlapping.

**Done when:** after 1 hour, `SELECT count(*) FROM poll_runs` ≈ 120 and `ok = 1` for nearly all rows. README has a "check the collector" section with 3 SQL one-liners: rows in the last 10 min, last error, and the biggest gap today.

### Stage 3 — Arrivals and trips (`trips.py`, pure, tests first)

This is the core logic. **Write `tests/simulate.py` and `tests/test_trips.py` first**, then the code.

#### 3a. `detect_arrivals(poll_runs, polls, stop, service) -> DataFrame`

Walk through consecutive **successful** runs for that stop (`prev`, `curr`):

| Case | Rule |
|---|---|
| Gap | `curr.polled_at − prev.polled_at > GAP_SEC` → record a `gap` event and infer nothing |
| No bus listed in `prev` | Nothing to do |
| Which `prev` buses have **left** the list | If `curr` lists no bus for this service, every bus in `prev` has left. Otherwise, take `N`, the front bus in `curr`, and find the `prev` bus with the closest ETA (seq `j`). All `prev` buses with seq < `j` have left. If `prev` had only one bus `F`, treat it as left when `N.eta − F.eta > SINGLE_BUS_JUMP_SEC`. Handling several buses leaving in one poll matters because buses often bunch at the checkpoint |
| A bus that left, with `eta − prev.polled_at ≤ DUE_WITHIN_SEC` | **Arrival.** `arrived_at = min(eta, curr.polled_at)`. Keep `monitored`, `load` and `dist_m` (distance from its last GPS position to the stop, if known) |
| A bus that left while still far away | `dropped` event. Not an arrival |

Output columns: `service, stop, arrived_at, monitored, load, dist_m, event` (`arrival` / `dropped` / `gap`).

#### 3b. `pair_trips(arrivals_a, arrivals_b, gaps) -> (trips, day_quality)`

| Step | Rule |
|---|---|
| Service-day | `service_day = (arrived_at − 3h).date()` |
| For each (service, service_day) | `A` = sorted arrivals at 46219, `B` = sorted arrivals at 46109 |
| `day_ok` | `len(A) == len(B)` **and** no gap at either stop during that service-day |
| If `day_ok` | Pair `A[i]` with `B[i]`. `trip_min = (B[i] − A[i])` in minutes |
| Sanity | If any trip has `trip_min < MIN_TRIP_MIN` or `> MAX_TRIP_MIN`, set `day_ok = False` and `reason = "implausible trip"` |
| If not `day_ok` | Produce no trips for that service-day, and record why |

Outputs:
- `trips`: `service, service_day, left_a, arrived_b, trip_min, both_monitored`
- `day_quality`: `service, service_day, n_a, n_b, n_gaps, day_ok, reason`

#### 3c. `slot_stats(trips, by=["day_type", "slot"]) -> DataFrame`

| Item | Rule |
|---|---|
| `slot` | `left_a` rounded down to 15 min, as `HH:MM` |
| Stats | `n, median, p25, p75` of `trip_min`. Use only `both_monitored` trips |
| Hide | Slots with `n < MIN_TRIPS_PER_SLOT` |

#### Tests (`tests/simulate.py` + `tests/test_trips.py`)

`simulate.py`: given a list of **true** buses `(service, t_at_A, t_at_B)`, generate `poll_runs` and `polls` rows exactly as the collector would, one poll per minute per stop. Each poll lists the next 3 buses that haven't reached that stop yet. Options: ETA noise, a drift factor (ETA under-estimates the remaining time, so it keeps rising in a jam), and dropped polls.

| Test | Asserts |
|---|---|
| Round trip, clean data | 40 buses, headway 3–15 min, true trip 8–90 min → recovered trips equal the true ones (each within 60 s), and every `day_ok` is True |
| Jam drift | The ETA keeps rising for 30 min while the bus is stuck → no false arrival |
| Bus dropped while far | The bus disappears 10 min before its ETA → `dropped`, not `arrival` |
| Last bus of the night | The service vanishes from the list after the last ETA → counted as an arrival |
| Bunching | Two buses of the same service reach the stop within the same minute → **2** arrivals |
| 5-min gap in polling | Gap recorded → that service-day has `day_ok = False` |
| One missed arrival at B | `len(A) ≠ len(B)` → `day_ok = False`, no trips |
| Service-day boundary | A bus at 46219 at 00:40 and 46109 at 01:05 belongs to the previous service-day |
| Blank NextBus2/3 | Handled, no crash |
| `slot_stats` | Known input → known median, P25 and P75, and slot labels |

**Done when:** all tests pass, **and** on ≥ 1 full day of real data from the VM, a small script/notebook command prints:
- `day_quality` for each service
- the % of service-days with `day_ok`. **If below 80%, stop and show me why** before Stage 4
- off-peak (10:00–16:00) median trip time per service. Expect roughly 5–25 min. Flag anything outside that

### Stage 4 — Rolling 14-day dashboard (`dashboard.py`)

One static HTML page, readable on a phone, rebuilt every 15 min.

| Section | Content |
|---|---|
| Header | "JB Checkpoint (46219) → Woodlands Checkpoint (46109), bus 160/170/170X/950", the date range, and "updated HH:MM" |
| KPI tiles | **Now:** median of trips that reached 46109 in the last 60 min (with n) · **Typical:** 14-day median for this day type and slot · **On the bridge now:** buses that have reached 46219 but not yet 46109 today, with how long the longest of them has been travelling · **Data health:** age of the last poll and % of service-days with `day_ok` |
| Chart 1 — Daily profile | x = 15-min slot when the bus left 46219 (05:00–01:00), y = minutes. Lines: weekday median, weekend median, each with a shaded P25–P75 band. Dots: today's trips. Holidays are excluded from the lines |
| Chart 2 — 14-day heatmap | Rows = the last 14 service-days (label e.g. `Mon 12 Oct`, with ★ for a public holiday and ◆ for a school holiday), columns = 15-min slots, colour = median trip minutes, blank where there's no data or the day failed `day_ok` |
| Table — Best and worst times | For weekdays and weekends: the 3 slots with the lowest and highest medians between 06:00 and 23:00 |
| Table — Data quality | For each of the last 14 days: polls OK, arrivals at 46219 / 46109 per service, `day_ok` |

Implementation:

| Item | Spec |
|---|---|
| Charts | Plotly, `fig.to_html(full_html=False, include_plotlyjs="cdn")` for each figure, combined into one simple HTML template string with light CSS |
| Window | Last `DASH_DAYS` service-days. Keep **all** raw data in the DB; only the dashboard is limited to 14 days |
| "On the bridge now" | Today's service-day, per service: `A[len(B):]`. Only show it if today has had no gaps |
| Upload | `gcloud storage cp dashboard.html gs://<BUCKET>/index.html --cache-control="no-cache"` |
| Bucket | US region (Always Free), public read: `allUsers` → `roles/storage.objectViewer`. Walk me through it |

Add to `deploy/crontab.txt`:

```cron
*/15 * * * * cd $HOME/causeway-bus && .venv/bin/python dashboard.py && gcloud storage cp dashboard.html gs://<BUCKET>/index.html --cache-control="no-cache" >> logs/dashboard.log 2>&1
0 3 * * 0 cd $HOME/causeway-bus && sqlite3 data/causeway.db ".backup data/backup.db" && gcloud storage cp data/backup.db gs://<BACKUP_BUCKET>/causeway-$(date +\%F).db >> logs/backup.log 2>&1
```

Use a **separate private bucket** for backups; the dashboard bucket is public.

Tests (`tests/test_dashboard.py`):

| Test | Asserts |
|---|---|
| Smoke | Build the dashboard from a temporary DB filled by `simulate.py` → no error, and the HTML contains both chart titles and the KPI labels |
| Empty DB | Shows "No data yet" and doesn't crash |
| "On the bridge now" | 5 arrivals at A and 3 at B today → 2 buses on the bridge, with the right elapsed times |

**Done when:** the public URL loads on my phone and updates every 15 min.

### Stage 5 — Calendar flags (`calendar_flags.py`)

| Item | Spec |
|---|---|
| `day_flags(date) -> dict` | `sg_ph` (`holidays.country_holidays("SG")`), `my_ph` (`holidays.country_holidays("MY", subdiv=<Johor code>)`; check the correct subdivision code in the installed version), `school_hol` (from the CSV) |
| `day_type(date)` | `"holiday"` if `sg_ph` or `my_ph`, else `"weekend"` for Sat/Sun, else `"weekday"` |
| `data/school_holidays.csv` | Columns `country,start,end,name`. **Create it with only the header row, then ask me to fill it.** Don't guess dates |
| Use | The dashboard uses `day_type` for the profile lines (holidays excluded), and the ★ / ◆ heatmap labels |

Tests (`tests/test_calendar.py`): 25 Dec 2026 → `sg_ph` and `my_ph` both True. An ordinary Tuesday → `weekday`. A Saturday → `weekend`. A date inside a CSV range → `school_hol` True.

**Done when:** tests pass and the dashboard shows holiday markers.

### Stage 6 — README

Short and table-based:
- architecture diagram
- one-off setup checklist (LTA key, GCP, cron)
- how to check the collector is healthy (the SQL one-liners)
- how to run the tests
- the known limitations (section 8)

---

## 8. Known limitations (explain these to me when relevant)

| Limitation | Effect |
|---|---|
| No bus ID, so trips are paired in arrival order | If one bus overtakes another of the same service, their two times are swapped. Medians are barely affected |
| One missed arrival invalidates that service-day for that service | Deliberate. Correct trips matter more than more trips. The `day_ok` % on the dashboard shows the cost |
| A bus that stops partway (breakdown) | That service-day is unbalanced → excluded |
| Only the bus ride is measured | It excludes immigration queues before boarding and after getting off |
| 14 days = only 2 of each weekday | So the profile groups weekdays vs weekends. Per-weekday profiles need more weeks of data (the DB keeps everything) |
| Arrival times come from LTA's ETA | Accurate to roughly ±1 min |

## 9. Later (do NOT build now)

| Idea | Learning goal |
|---|---|
| Rainfall from data.gov.sg (it can fetch by date, so it can be added later) | Joining extra data onto trips |
| Hourly passenger trips from `PV/ODBus` overlaid on the profile | Demand vs travel time |
| A model to forecast the next 2 hours (start from the median, then try LightGBM) | Forecasting |
| Push alert when "Now" is more than 1.5× "Typical" | Notifications |
| The SG → JB direction | Reuse the same logic |
