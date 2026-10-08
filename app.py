import os
from datetime import datetime
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

CLIENT_NAME = os.getenv("ENTUR_CLIENT_NAME", "bjornar-ruter-sanntid")
GEOCODER_URL = "https://api.entur.io/geocoder/v3/autocomplete"
JOURNEY_URL = "https://api.entur.io/journey-planner/v3/graphql"

mcp = FastMCP(
    "Bjørnars Entur Live",
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8000")),
)

HEADERS = {
    "ET-Client-Name": CLIENT_NAME,
    "User-Agent": CLIENT_NAME,
}


def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=15.0, headers={**HEADERS, "Content-Type": "application/json"}) as client:
        r = client.post(JOURNEY_URL, json={"query": query, "variables": variables})
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            raise RuntimeError(str(payload["errors"]))
        return payload["data"]


@mcp.tool()
def search_stops(name: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search Entur stop places by name and return canonical NSR stop IDs."""
    params = {"q": name, "lang": "no", "limit": max(1, min(limit, 10)), "layers": "stopPlace"}
    with httpx.Client(timeout=10.0, headers=HEADERS) as client:
        r = client.get(GEOCODER_URL, params=params)
        r.raise_for_status()
        data = r.json()
    out = []
    for feature in data.get("features", []):
        p = feature.get("properties", {})
        names = p.get("names") or {}
        out.append(
            {
                "id": p.get("id"),
                "name": names.get("default") or p.get("name"),
                "display_name": names.get("display") or p.get("label"),
                "transport_modes": p.get("transportModes") or [],
                "stop_place_types": p.get("stopPlaceTypes") or [],
            }
        )
    return out


@mcp.tool()
def departures(stop_place_id: str, number_of_departures: int = 20, time_range_seconds: int = 7200) -> dict[str, Any]:
    """Get Entur departure-board data with scheduled and realtime/expected times for a stop place."""
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
        return {"stop_place_id": stop_place_id, "error": "Stop place not found"}

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


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
