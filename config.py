"""Project settings. See CLAUDE.md section 6."""

STOP_A = "46219"  # JB Checkpoint (start)
STOP_B = "46109"  # Woodlands Checkpoint (end)

# The collector stores all 4, so a later design can still use 160 and 170X.
COLLECT_SERVICES = ("160", "170", "170X", "950")

# Analysis only uses the services that pass Stage 0 checks 1-3.
# 160 and 170X start at 46219 and have no GPS there in the daytime (9 Oct data), so they fail check 2.
SERVICES = ("170", "950")

# Copied from probe.py check 4 (LTA BusStops). Don't guess: {stop: (lat, lon)}
STOP_COORDS = {
    "46219": (1.465427, 103.768267),  # Johor Bahru Checkpt
    "46109": (1.446941, 103.769253),  # W'lands Checkpt
}

GAP_SEC = 180              # polls further apart than this are a gap
DUE_WITHIN_SEC = 120       # a bus only counts as arrived if its ETA was this close
SINGLE_BUS_JUMP_SEC = 300  # when only one bus was listed (Stage 3)
MIN_TRIP_MIN = 3
MAX_TRIP_MIN = 240
SERVICE_DAY_START_HOUR = 3
DASH_DAYS = 14
MIN_TRIPS_PER_SLOT = 3
