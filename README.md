# Causeway Bus Crossing Time

How long buses take from JB Checkpoint (46219) to Woodlands Checkpoint (46109). See `CLAUDE.md` for the full spec.

## Check the collector

Run on the VM from `~/causeway-bus`. Times are stored in Singapore time, so `now` is shifted by +8 hours.

| Question | Command |
|---|---|
| Rows in the last 10 min (expect about 10 per stop, all ok) | `sqlite3 data/causeway.db "SELECT stop, count(*) AS runs, sum(ok) AS ok FROM poll_runs WHERE polled_at >= datetime('now', '+8 hours', '-10 minutes') GROUP BY stop"` |
| Last error | `sqlite3 data/causeway.db "SELECT polled_at, stop, error FROM poll_runs WHERE ok = 0 ORDER BY polled_at DESC LIMIT 1"` |
| Biggest gap today, in seconds (over 180 = a gap) | `sqlite3 data/causeway.db "SELECT stop, max(gap) FROM (SELECT stop, strftime('%s', polled_at) - strftime('%s', lag(polled_at) OVER (PARTITION BY stop ORDER BY polled_at)) AS gap FROM poll_runs WHERE polled_at >= date('now', '+8 hours')) GROUP BY stop"` |

Also: `tail logs/collector.log` shows one line per minute.
