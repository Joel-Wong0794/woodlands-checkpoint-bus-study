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

# Stage 3 measures trips from GPS tracks (LTA's ETAs proved unreliable across the border).
# "At 46219" = within ZONE_A_M of the stop AND north of GATE_A_LAT. From the 9 Oct GPS fixes: buses wait
# in the JB checkpoint bus area (lat 1.4645-1.4665, up to ~450 m west of the stop); south of it is the Causeway.
ZONE_A_M = 500
GATE_A_LAT = 1.4645
ZONE_B_M = 100             # "at 46109" = within this distance (real arrivals stop 30-65 m from it)
MAX_SPEED_MS = 25          # a bus can't move faster than this (~90 km/h) between fixes...
LINK_SLACK_M = 200         # ...plus this much GPS noise
MAX_LINK_M = 2500          # never link fixes further apart than this (the crossing is ~2 km)
MAX_LOST_MIN = 15          # a track survives this long without a fix (buses drop off the list and come back)
BLIND_FLAG_MIN = 5         # a ride with a longer stretch without fixes is flagged (time less precise)
SLOT_MIN = 60              # slot size for stats; hourly until there are enough weeks of data

MIN_TRIP_MIN = 3
MAX_TRIP_MIN = 240
SERVICE_DAY_START_HOUR = 3
DASH_DAYS = 14
MIN_TRIPS_PER_SLOT = 3
