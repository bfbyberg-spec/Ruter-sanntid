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

# Tiny in-process cache. Stop IDs are effectively stable and this avoids repeated geocoder calls.
_STOP_CACHE: dict[str, dict[str, Any]] = {}


def _client() -> httpx.Client:
    return httpx.Client(timeout=15.0, headers=HEADERS)


def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    with _client() as client:
        r = client.post(
            JOURNEY_URL,
            headers={"Content-Type": "application/json"},
            json={"query": query, "variables": variables},
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

    # Prefer metro stop places when present, then the first result.
    def score(feature: dict[str, Any]) -> int:
        p = feature.get("properties", {})
        modes = [str(x).lower() for x in (p.get("transportModes") or [])]
        return 0 if "metro" in modes else 1

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


def _mins_between(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60.0


@mcp.tool()
def next_departure(from_stop: str, destination: str, line: str | None = None) -> dict[str, Any]:
    """FAST PATH. In one MCP call, find the next live departure from a named stop toward a destination. Optionally filter by line number. Use this for questions like 'Neste bane fra Ringstabekk til Kolsås?'."""
    stop = _search_stop(from_stop)
    board = _departure_board(stop["id"], number_of_departures=50, time_range_seconds=14400)

    dest_cf = destination.casefold().strip()
    line_cf = line.casefold().strip() if line else None
    matches = []
    for d in board.get("departures", []):
        if d.get("cancelled"):
            continue
        d_dest = (d.get("destination") or "").casefold()
        d_line = str(d.get("line") or "").casefold()
        if dest_cf not in d_dest:
            continue
        if line_cf and line_cf != d_line:
            continue
        matches.append(d)

    if not matches:
        return {
            "from_stop": board.get("stop_name") or stop["name"],
            "destination_filter": destination,
            "line_filter": line,
            "found": False,
            "message": "Ingen matchende avganger funnet i søkevinduet.",
            "retrieved_at": board.get("retrieved_at"),
        }

    matches.sort(key=lambda d: d.get("expected_departure_time") or d.get("aimed_departure_time") or "")
    nxt = matches[0]
    aimed = nxt.get("aimed_departure_time")
    expected = nxt.get("expected_departure_time") or aimed
    delay_seconds = None
    if aimed and expected:
        delay_seconds = round((datetime.fromisoformat(expected) - datetime.fromisoformat(aimed)).total_seconds())

    return {
        "from_stop": board.get("stop_name") or stop["name"],
        "destination": nxt.get("destination"),
        "line": nxt.get("line"),
        "aimed_departure_time": aimed,
        "expected_departure_time": expected,
        "realtime": bool(nxt.get("realtime")),
        "delay_seconds": delay_seconds,
        "cancelled": bool(nxt.get("cancelled")),
        "quay": nxt.get("quay"),
        "retrieved_at": board.get("retrieved_at"),
        "next_matches": matches[:3],
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
        # Any metro departure whose destination is south/west of Ullevål will call at Majorstuen.
        # Exclude clearly northbound destinations.
        dest = (d.get("destination") or "").casefold()
        if any(x in dest for x in ["vestli", "sognsvann", "frognerseteren", "storo"]):
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
            # Entur departure-board at Ullevål does not guarantee the call-at-Majorstuen arrival in this response,
            # so use the established ~4 minute runtime fallback and label it explicitly.
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

    # Latest Ullevål departure that still makes a viable line 3 connection.
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
