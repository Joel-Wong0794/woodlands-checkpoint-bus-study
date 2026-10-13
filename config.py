"""Project settings. See CLAUDE.md section 6."""

STOP_A = "46219"  # JB Checkpoint (start)
STOP_B = "46109"  # Woodlands Checkpoint (end)

# SG -> JB direction (collected from 11 Oct 2026 23:31): all 4 services go 46101 -> 46211 (LTA BusRoutes).
STOP_SG = "46101"  # W'lands Checkpt, departure side (board after Singapore exit immigration)
STOP_JB = "46211"  # Johor Bahru Checkpt, arrival side (alight for Malaysian immigration)
COLLECT_STOPS = (STOP_A, STOP_B, STOP_SG, STOP_JB)

# The collector stores all 4, so a later design can still use 160 and 170X.
COLLECT_SERVICES = ("160", "170", "170X", "950")

# Analysis uses all 4. 160 and 170X failed Stage 0 check 2 (no GPS while waiting at 46219), but they switch GPS on
# when they leave, which is all the GPS-track method needs (Stage 3, 11 Oct data).
SERVICES = ("160", "170", "170X", "950")

# A commuter at the JB checkpoint boards any of 160/170/170X from the same berth; 950 has its own. Stats are per group.
GROUPS = {"160/170/170X": ("160", "170", "170X"), "950": ("950",)}

# Copied from probe.py check 4 (LTA BusStops). Don't guess: {stop: (lat, lon)}
# 46219/46109 from 8 Oct. LTA's BusStops showed them ~150-190 m away on 11 Oct; kept as is, because the JB -> SG
# zones in DIRECTIONS were tuned to real bus GPS fixes around these points.
STOP_COORDS = {
    "46219": (1.465427, 103.768267),  # Johor Bahru Checkpt
    "46109": (1.446941, 103.769253),  # W'lands Checkpt
    "46101": (1.446160, 103.768541),  # W'lands Checkpt (11 Oct)
    "46211": (1.465696, 103.767509),  # Johor Bahru Checkpt (11 Oct)
}

GAP_SEC = 180              # polls further apart than this are a gap

# Stage 3 measures trips from GPS tracks (LTA's ETAs proved unreliable across the border).
# The Causeway as a latitude band between the two checkpoint areas. Buses only turn round outside it.
CAUSEWAY_LAT = (1.4475, 1.4645)

# Each direction: where you board and alight, and how a GPS fix counts as "at" each stop:
# within <stop>_m metres of the stop AND inside the latitude bounds.
DIRECTIONS = {
    "jb-sg": {
        "name": "JB → Singapore", "board": STOP_A, "alight": STOP_B,
        # buses wait in the JB checkpoint bus area, lat 1.4645-1.4665, up to ~450 m west of the stop (9 Oct fixes)
        "board_m": 500, "board_lat": (1.4645, 90.0),
        # real arrivals stop 30-65 m from 46109
        "alight_m": 100, "alight_lat": (-90.0, 90.0),
    },
    "sg-jb": {
        "name": "Singapore → JB", "board": STOP_SG, "alight": STOP_JB,
        # last fix before heading north: median 41 m, 90% within 155 m of 46101 (12-13 Oct fixes)
        "board_m": 250, "board_lat": (-90.0, 1.4475),
        # tracks end 220-310 m from the 46211 pin, inside the JB checkpoint area (12-13 Oct fixes)
        "alight_m": 400, "alight_lat": (1.4635, 90.0),
    },
}

MAX_SPEED_MS = 25          # a bus can't move faster than this (~90 km/h) between fixes...
LINK_SLACK_M = 200         # ...plus this much GPS noise
MAX_LINK_M = 2500          # never link fixes further apart than this (the crossing is ~2 km)
BACK_M = 150              # on the Causeway a track can't move this far against its direction (buses don't U-turn there)
MAX_LOST_MIN = 15          # a track survives this long without a fix (buses drop off the list and come back)
BLIND_FLAG_MIN = 5         # a ride with a longer stretch without fixes is flagged (time less precise)
SLOT_MIN = 60              # slot size for stats; hourly until there are enough weeks of data

MIN_TRIP_MIN = 3
MAX_TRIP_MIN = 240
SERVICE_DAY_START_HOUR = 3
DASH_DAYS = 14
PLAN_DAYS = 28             # "Plan your day" window: 4 of each weekday
MIN_TRIPS_PER_SLOT = 3
