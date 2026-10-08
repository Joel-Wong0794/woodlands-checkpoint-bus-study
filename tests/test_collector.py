"""Tests for collector.parse_bus_arrival. Run from the project root: python -m pytest"""
import json
from datetime import datetime
from pathlib import Path

from collector import count_services, parse_bus_arrival

FIXTURES = Path(__file__).parent / "fixtures"
SERVICES = ("160", "170", "170X", "950")
POLLED_AT = "2026-10-08 22:40:13"
BLANK_BUS = {k: "" for k in ("OriginCode", "DestinationCode", "EstimatedArrival", "Monitored", "Latitude",
                             "Longitude", "VisitNumber", "Load", "Feature", "Type")}


def bus(eta="2026-10-08T22:45:00+08:00", monitored=1, lat="1.4600", lon="103.7700"):
    return {"OriginCode": "46219", "DestinationCode": "29009", "EstimatedArrival": eta, "Monitored": monitored,
            "Latitude": lat, "Longitude": lon, "VisitNumber": "1", "Load": "SEA", "Feature": "WAB", "Type": "SD"}


def service(no, *buses):
    buses = list(buses) + [BLANK_BUS] * (3 - len(buses))
    return {"ServiceNo": no, "Operator": "SBST", "NextBus": buses[0], "NextBus2": buses[1], "NextBus3": buses[2]}


def test_real_fixture():
    for stop in ("46219", "46109"):
        payload = json.loads((FIXTURES / f"busarrival_{stop}.json").read_text())
        rows = parse_bus_arrival(payload, stop, POLLED_AT, SERVICES)
        expected = sum(bool(s[k].get("EstimatedArrival"))
                       for s in payload["Services"] if s["ServiceNo"] in SERVICES
                       for k in ("NextBus", "NextBus2", "NextBus3"))
        assert len(rows) == expected
        for row in rows:
            datetime.strptime(row["eta"], "%Y-%m-%d %H:%M:%S")  # raises if not plain SGT text
            assert row["stop"] == stop and row["service"] in SERVICES


def test_eta_converted_to_sgt():
    payload = {"Services": [service("170", bus(eta="2026-10-08T14:45:00+00:00"))]}
    assert parse_bus_arrival(payload, "46219", POLLED_AT, SERVICES)[0]["eta"] == "2026-10-08 22:45:00"


def test_blank_nextbus3_gives_no_row():
    payload = {"Services": [service("170", bus(), bus())]}
    rows = parse_bus_arrival(payload, "46219", POLLED_AT, SERVICES)
    assert [r["seq"] for r in rows] == [1, 2]


def test_not_monitored_has_no_coordinates():
    payload = {"Services": [service("160", bus(monitored=0, lat="0.0", lon="0.0"))]}
    row = parse_bus_arrival(payload, "46219", POLLED_AT, SERVICES)[0]
    assert row["monitored"] == 0 and row["lat"] is None and row["lon"] is None


def test_blank_strings_become_none():
    b = bus()
    b["Load"] = b["Type"] = ""
    row = parse_bus_arrival({"Services": [service("170", b)]}, "46219", POLLED_AT, SERVICES)[0]
    assert row["load"] is None and row["bus_type"] is None


def test_other_services_ignored():
    payload = {"Services": [service("911", bus()), service("170", bus())]}
    rows = parse_bus_arrival(payload, "46219", POLLED_AT, SERVICES)
    assert [r["service"] for r in rows] == ["170"]
    assert count_services(payload, SERVICES) == 1


def test_empty_payloads():
    for payload in (None, {}, {"Services": []}, {"odata.metadata": "x"}):
        assert parse_bus_arrival(payload, "46219", POLLED_AT, SERVICES) == []
        assert count_services(payload, SERVICES) == 0
