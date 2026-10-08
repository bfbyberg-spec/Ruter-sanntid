import os
from datetime import datetime
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

CLIENT_NAME = os.getenv("ENTUR_CLIENT_NAME", "bjornar-ruter-sanntid")
GEOCODER_URL = "https://api.entur.io/geocoder/v3/autocomplete"
JOURNEY_URL = "https://api.entur.io/journey-planner/v3/graphql"

mcp = MCPServer("Bjørnars Entur Live")

HEADERS = {
    "ET-Client-Name": CLIENT_NAME,
    "User-Agent": CLIENT_NAME,
}

_STOP_CACHE: dict[str, dict[str, Any]] = {}


def _client() -> httpx.Client:
    return httpx.Client(timeout=15.0, headers=HEADERS)


def _graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    with _client() as client:
        r = client.post(
            JOURNEY_URL,
            headers={"Content-Type": "application/json"},
            json={"query": query, "variables": variables or {}},
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            raise RuntimeError(str(payload["errors"]))
        return payload["data"]


def _search_stop(name: str) -> dict[str, Any]:
    key = name.strip().casefold()
    if key in _STOP_CACHE:
        return _STOP_CACHE[key]

    params = {"q": name, "lang": "no", "limit": 5, "layers": "stopPlace"}
    with _client() as client:
        r = client.get(GEOCODER_URL, params=params)
        r.raise_for_status()
        data = r.json()

    features = data.get("features", [])
    if not features:
        raise ValueError(f"Fant ikke stoppested: {name}")

    def score(feature: dict[str, Any]) -> tuple[int, int]:
        p = feature.get("properties", {})
        modes_raw = p.get("transportModes") or []
        modes = []
        for x in modes_raw:
            if isinstance(x, dict):
                modes.append(str(x.get("mode", "")).lower())
            else:
                modes.append(str(x).lower())
        return (0 if modes else 1, 0 if "metro" in modes else 1)

    feature = sorted(features, key=score)[0]
    p = feature.get("properties", {})
    names = p.get("names") or {}
    result = {
        "id": p.get("id"),
        "name": names.get("default") or p.get("name") or name,
        "display_name": names.get("display") or p.get("label"),
        "transport_modes": p.get("transportModes") or [],
        "stop_place_types": p.get("stopPlaceTypes") or [],
    }
    if not result["id"]:
        raise ValueError(f"Fant stoppested uten gyldig Entur-ID: {name}")
    _STOP_CACHE[key] = result
    return result


def _departure_board(stop_place_id: str, number_of_departures: int = 40, time_range_seconds: int = 10800) -> dict[str, Any]:
    query = """
    query Departures($id: String!, $n: Int!, $range: Int!) {
      stopPlace(id: $id) {
        id
        name
        estimatedCalls(numberOfDepartures: $n, timeRange: $range, includeCancelledTrips: true) {
          realtime
          cancellation
          aimedArrivalTime
          aimedDepartureTime
          expectedArrivalTime
          expectedDepartureTime
          destinationDisplay { frontText }
          quay { id publicCode name }
          serviceJourney {
            id
            line {
              id
              publicCode
              name
              transportMode
            }
          }
        }
      }
    }
    """
    data = _graphql(
        query,
        {
            "id": stop_place_id,
            "n": max(1, min(number_of_departures, 100)),
            "range": max(60, min(time_range_seconds, 86400)),
        },
    )
    stop = data.get("stopPlace")
    if not stop:
        return {"stop_place_id": stop_place_id, "error": "Stop place not found", "departures": []}

    calls = []
    for c in stop.get("estimatedCalls") or []:
        line = ((c.get("serviceJourney") or {}).get("line") or {})
        calls.append(
            {
                "line": line.get("publicCode"),
                "line_name": line.get("name"),
                "transport_mode": line.get("transportMode"),
                "destination": ((c.get("destinationDisplay") or {}).get("frontText")),
                "aimed_departure_time": c.get("aimedDepartureTime"),
                "expected_departure_time": c.get("expectedDepartureTime"),
                "aimed_arrival_time": c.get("aimedArrivalTime"),
                "expected_arrival_time": c.get("expectedArrivalTime"),
                "realtime": c.get("realtime"),
                "cancelled": c.get("cancellation"),
                "quay": c.get("quay"),
                "service_journey_id": ((c.get("serviceJourney") or {}).get("id")),
            }
        )

    return {
        "stop_place_id": stop.get("id"),
        "stop_name": stop.get("name"),
        "retrieved_at": datetime.now().astimezone().isoformat(),
        "departures": calls,
    }


def _trip_patterns(
    from_stop_id: str,
    to_stop_id: str,
    num_patterns: int = 6,
    transport_mode: str | None = None,
) -> list[dict[str, Any]]:
    mode_clause = ""
    if transport_mode:
        mode_clause = (
            "modes: { accessMode: foot, egressMode: foot, directMode: foot, "
            f"transportModes: [{{ transportMode: {transport_mode} }}] }}"
        )

    query = f"""
    {{
      trip(
        from: {{ place: \"{from_stop_id}\" }}
        to: {{ place: \"{to_stop_id}\" }}
        {mode_clause}
        numTripPatterns: {max(1, min(num_patterns, 10))}
      ) {{
        tripPatterns {{
          duration
          expectedStartTime
          expectedEndTime
          legs {{
            mode
            duration
            fromPlace {{ name }}
            toPlace {{ name }}
            aimedStartTime
            expectedStartTime
            aimedEndTime
            expectedEndTime
            line {{
              publicCode
              name
              transportMode
            }}
            fromEstimatedCall {{
              destinationDisplay {{ frontText }}
            }}
          }}
        }}
      }}
    }}
    """
    data = _graphql(query)
    return ((data.get("trip") or {}).get("tripPatterns") or [])


def _normalise_trip(pattern: dict[str, Any]) -> dict[str, Any]:
    raw_legs = pattern.get("legs") or []
    legs = []
    transit_legs = []
    for leg in raw_legs:
        line = leg.get("line") or {}
        dest_display = ((leg.get("fromEstimatedCall") or {}).get("destinationDisplay") or {}).get("frontText")
        item = {
            "mode": leg.get("mode"),
            "line": line.get("publicCode"),
            "line_name": line.get("name"),
            "transport_mode": line.get("transportMode"),
            "destination_display": dest_display,
            "from": (leg.get("fromPlace") or {}).get("name"),
            "to": (leg.get("toPlace") or {}).get("name"),
            "aimed_departure_time": leg.get("aimedStartTime"),
            "expected_departure_time": leg.get("expectedStartTime"),
            "aimed_arrival_time": leg.get("aimedEndTime"),
            "expected_arrival_time": leg.get("expectedEndTime"),
            "duration_seconds": leg.get("duration"),
        }
        legs.append(item)
        mode = str(leg.get("mode") or "").lower()
        if line.get("publicCode") or mode not in ("foot", "walk", "walking"):
            transit_legs.append(item)

    first = transit_legs[0] if transit_legs else (legs[0] if legs else {})
    aimed = first.get("aimed_departure_time")
    expected = first.get("expected_departure_time") or aimed
    delay_seconds = None
    if aimed and expected:
        delay_seconds = round((datetime.fromisoformat(expected) - datetime.fromisoformat(aimed)).total_seconds())

    return {
        "expected_start_time": pattern.get("expectedStartTime"),
        "expected_end_time": pattern.get("expectedEndTime"),
        "duration_seconds": pattern.get("duration"),
        "number_of_transit_legs": len(transit_legs),
        "direct": len(transit_legs) == 1,
        "first_line": first.get("line"),
        "first_mode": first.get("transport_mode") or first.get("mode"),
        "first_destination_display": first.get("destination_display"),
        "aimed_departure_time": aimed,
        "expected_departure_time": expected,
        "delay_seconds": delay_seconds,
        "legs": legs,
    }


def _mode_from_filter(value: str | None) -> str | None:
    if not value:
        return None
    v = value.casefold().strip().replace("-", " ")
    aliases = {
        "metro": "metro",
        "bane": "metro",
        "t bane": "metro",
        "tbane": "metro",
        "subway": "metro",
        "bus": "bus",
        "buss": "bus",
        "tram": "tram",
        "trikk": "tram",
        "rail": "rail",
        "tog": "rail",
        "train": "rail",
    }
    return aliases.get(v)


@mcp.tool()
def next_departure(from_stop: str, destination: str, line: str | None = None) -> dict[str, Any]:
    """FAST PATH. Find the next real journey from a named stop to another named stop. Optional line can be a line number, or a mode word such as metro/bane/T-bane, bus/buss, tram/trikk or rail/tog to restrict the journey search."""
    origin = _search_stop(from_stop)
    dest = _search_stop(destination)

    mode_filter = _mode_from_filter(line)
    patterns = [
        _normalise_trip(p)
        for p in _trip_patterns(origin["id"], dest["id"], 8, transport_mode=mode_filter)
    ]

    if line and not mode_filter:
        line_cf = line.casefold().strip()
        filtered = []
        for p in patterns:
            if any(str(l.get("line") or "").casefold() == line_cf for l in p.get("legs", [])):
                filtered.append(p)
        if filtered:
            patterns = filtered

    if mode_filter:
        # Defensive post-filter in addition to the OTP mode restriction.
        filtered = []
        for p in patterns:
            transit = [l for l in p.get("legs", []) if l.get("line")]
            if transit and all(str(l.get("transport_mode") or l.get("mode") or "").casefold() == mode_filter for l in transit):
                filtered.append(p)
        patterns = filtered

    patterns.sort(key=lambda p: p.get("expected_departure_time") or p.get("expected_start_time") or "")
    if not patterns:
        return {
            "found": False,
            "from_stop": origin["name"],
            "to_stop": dest["name"],
            "mode_filter": mode_filter,
            "line_filter": None if mode_filter else line,
            "message": "Ingen reiser funnet akkurat nå med valgt filter.",
            "retrieved_at": datetime.now().astimezone().isoformat(),
        }

    best = patterns[0]
    return {
        "found": True,
        "from_stop": origin["name"],
        "to_stop": dest["name"],
        "mode_filter": mode_filter,
        "line_filter": None if mode_filter else line,
        "line": best.get("first_line"),
        "transport_mode": best.get("first_mode"),
        "destination": best.get("first_destination_display") or dest["name"],
        "aimed_departure_time": best.get("aimed_departure_time"),
        "expected_departure_time": best.get("expected_departure_time"),
        "expected_arrival_time": best.get("expected_end_time"),
        "delay_seconds": best.get("delay_seconds"),
        "direct": best.get("direct"),
        "number_of_transit_legs": best.get("number_of_transit_legs"),
        "legs": best.get("legs"),
        "alternatives": patterns[1:4],
        "retrieved_at": datetime.now().astimezone().isoformat(),
    }


@mcp.tool()
def commute_home(min_transfer_minutes: int = 3) -> dict[str, Any]:
    """FAST PATH for Bjørnar's work commute. In one MCP call, recommend the latest sensible departure from Ullevål stadion to Majorstuen that still connects to line 3 toward Kolsås with at least the requested transfer margin. Prefer realtime expected times; fall back to ~4 min Ullevål→Majorstuen runtime when exact arrival is unavailable."""
    ulleval = _search_stop("Ullevål stadion")
    majorstuen = _search_stop("Majorstuen")

    ul_board = _departure_board(ulleval["id"], number_of_departures=60, time_range_seconds=10800)
    maj_board = _departure_board(majorstuen["id"], number_of_departures=60, time_range_seconds=10800)

    line3 = []
    for d in maj_board.get("departures", []):
        if d.get("cancelled"):
            continue
        if str(d.get("line") or "").strip() != "3":
            continue
        if "kolsås" not in (d.get("destination") or "").casefold():
            continue
        line3.append(d)
    line3.sort(key=lambda d: d.get("expected_departure_time") or d.get("aimed_departure_time") or "")

    southbound = []
    for d in ul_board.get("departures", []):
        if d.get("cancelled"):
            continue
        dest_text = (d.get("destination") or "").casefold()
        if any(x in dest_text for x in ["vestli", "sognsvann", "frognerseteren", "storo"]):
            continue
        if (d.get("transport_mode") or "").lower() != "metro":
            continue
        southbound.append(d)
    southbound.sort(key=lambda d: d.get("expected_departure_time") or d.get("aimed_departure_time") or "")

    candidates = []
    for target in line3[:8]:
        target_dep = target.get("expected_departure_time") or target.get("aimed_departure_time")
        if not target_dep:
            continue
        for u in southbound[:20]:
            u_dep = u.get("expected_departure_time") or u.get("aimed_departure_time")
            if not u_dep:
                continue
            arr_dt = datetime.fromisoformat(u_dep)
            estimated_arrival = arr_dt.timestamp() + 4 * 60
            target_ts = datetime.fromisoformat(target_dep).timestamp()
            margin = (target_ts - estimated_arrival) / 60.0
            if margin >= min_transfer_minutes:
                candidates.append(
                    {
                        "ulleval_departure": u_dep,
                        "ulleval_line": u.get("line"),
                        "ulleval_destination": u.get("destination"),
                        "ulleval_realtime": bool(u.get("realtime")),
                        "majorstuen_arrival_estimated": datetime.fromtimestamp(estimated_arrival, tz=datetime.fromisoformat(u_dep).tzinfo).isoformat(),
                        "majorstuen_arrival_basis": "fallback_runtime_4_min",
                        "line3_departure": target_dep,
                        "line3_realtime": bool(target.get("realtime")),
                        "line3_aimed_departure": target.get("aimed_departure_time"),
                        "transfer_margin_minutes": round(margin, 1),
                    }
                )

    if not candidates:
        return {
            "found": False,
            "message": "Fant ingen forbindelse med ønsket overgangsmargin i søkevinduet.",
            "min_transfer_minutes": min_transfer_minutes,
            "retrieved_at": datetime.now().astimezone().isoformat(),
        }

    candidates.sort(key=lambda c: c["ulleval_departure"], reverse=True)
    best = candidates[0]
    return {
        "found": True,
        "recommendation": best,
        "alternatives": candidates[1:4],
        "min_transfer_minutes": min_transfer_minutes,
        "note": "Majorstuen-arrival uses the established ~4 minute fallback runtime; departure times use Entur expected/realtime data when available.",
        "retrieved_at": datetime.now().astimezone().isoformat(),
    }


@mcp.tool()
def search_stops(name: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search Entur stop places by name. Use only for unusual stop-name resolution, not for normal next-departure questions."""
    first = _search_stop(name)
    return [first]


@mcp.tool()
def departures(stop_place_id: str, number_of_departures: int = 20, time_range_seconds: int = 7200) -> dict[str, Any]:
    """Raw departure board. Use only when the fast-path tools do not cover the request."""
    return _departure_board(stop_place_id, number_of_departures, time_range_seconds)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )
