import os
from datetime import datetime, timedelta
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

CLIENT_NAME = os.getenv("ENTUR_CLIENT_NAME", "bjornar-ruter-sanntid")
GEOCODER_URL = "https://api.entur.io/geocoder/v3/autocomplete"
JOURNEY_URL = "https://api.entur.io/journey-planner/v3/graphql"
WORK_TO_ULLEVAL_MINUTES = float(os.getenv("WORK_TO_ULLEVAL_MINUTES", "5"))

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
        transport_mode = _mode_from_filter(transport_mode)
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
            "service_journey_id": (leg.get("serviceJourney") or {}).get("id"),
            "realtime": bool(leg.get("realtime")),
            "departure_realtime": bool((leg.get("fromEstimatedCall") or {}).get("realtime")),
            "arrival_realtime": bool((leg.get("toEstimatedCall") or {}).get("realtime")),
            "cancelled": bool((leg.get("fromEstimatedCall") or {}).get("cancellation") or (leg.get("toEstimatedCall") or {}).get("cancellation")),
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
    if value is None:
        return None
    v = " ".join(value.casefold().strip().replace("-", " ").split())
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
        "coach": "coach",
        "water": "water",
        "ferry": "water",
        "ferge": "water",
        "båt": "water",
        "air": "air",
        "fly": "air",
    }
    if v not in aliases:
        raise ValueError("Ukjent transportmiddel. Bruk metro/T-bane, bus/buss, tram/trikk, rail/tog, coach, water/ferge eller air/fly.")
    return aliases[v]


def _transit_legs(pattern: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        leg for leg in pattern.get("legs", [])
        if leg.get("line") or str(leg.get("mode") or "").casefold() not in ("foot", "walk", "walking")
    ]


@mcp.tool()
def next_departure(
    from_stop: str,
    destination: str,
    line: str | None = None,
    transport_mode: str | None = None,
) -> dict[str, Any]:
    """FAST PATH. Find the next real point-to-point journey. Set transport_mode='metro' for only T-bane, 'bus' for only buses, 'tram', 'rail', 'coach', 'water' or 'air'; Norwegian aliases are accepted. Omit transport_mode for all modes. Optional line restricts journeys to those containing that line, while transfers may use other lines of the selected mode. Legacy mode words in line (metro/bane/T-bane, bus/buss, tram/trikk, rail/tog) remain supported. Never substitute a different mode when no matching journey exists."""
    # Keep the shortcut callable by hosts whose tool catalogue predates switch_at_smestad.
    if line and line.strip().casefold() in ("bytte", "bytte?"):
        if from_stop.strip().casefold() != "ringstabekk" or destination.strip().casefold() != "ullevål stadion" or transport_mode:
            raise ValueError("Bytte?-snarveien gjelder Ringstabekk → Ullevål stadion uten transportfilter.")
        return switch_at_smestad()
    mode_filter = _mode_from_filter(transport_mode)
    legacy_mode = None
    line_filter = line.strip() if line else None
    if line_filter:
        try:
            legacy_mode = _mode_from_filter(line_filter)
        except ValueError:
            pass  # An unrecognised mode word can still be a public line code.
    if legacy_mode:
        if mode_filter and mode_filter != legacy_mode:
            raise ValueError("Transportmiddelet i line og transport_mode er motstridende.")
        mode_filter = legacy_mode
        line_filter = None

    origin = _search_stop(from_stop)
    dest = _search_stop(destination)

    patterns = [
        _normalise_trip(p)
        for p in _trip_patterns(origin["id"], dest["id"], 8, transport_mode=mode_filter)
    ]

    if line_filter:
        line_cf = line_filter.casefold()
        patterns = [
            p for p in patterns
            if any(str(leg.get("line") or "").casefold() == line_cf for leg in _transit_legs(p))
        ]

    if mode_filter:
        # Defensive post-filter in addition to the OTP mode restriction.
        filtered = []
        for p in patterns:
            transit = _transit_legs(p)
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
            "line_filter": line_filter,
            "message": "Ingen reiser funnet akkurat nå med valgt filter.",
            "retrieved_at": datetime.now().astimezone().isoformat(),
        }

    best = patterns[0]
    return {
        "found": True,
        "from_stop": origin["name"],
        "to_stop": dest["name"],
        "mode_filter": mode_filter,
        "line_filter": line_filter,
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


def _commute_boards(ulleval_id: str, majorstuen_id: str) -> dict[str, Any]:
    # Relative next calls belong to this dated departure, including after midnight.
    query = """
    query Commute($ul: String!, $maj: String!) {
      ulleval: stopPlace(id: $ul) {
        estimatedCalls(numberOfDepartures: 100, timeRange: 10800, includeCancelledTrips: true,
          filters: [{select: [{transportModes: [{transportMode: metro}]}]}]) {
          ...CommuteCall
          serviceJourneyEstimatedCalls {
            next(count: 3) { ...OnwardCall }
          }
        }
      }
      majorstuen: stopPlace(id: $maj) {
        estimatedCalls(numberOfDepartures: 100, timeRange: 10800, includeCancelledTrips: true,
          filters: [{select: [{transportModes: [{transportMode: metro}]}]}]) {
          ...CommuteCall
          serviceJourneyEstimatedCalls {
            next(count: 10) { ...OnwardCall }
          }
        }
      }
    }
    fragment CommuteCall on EstimatedCall {
      realtime cancellation forBoarding
      aimedDepartureTime expectedDepartureTime
      destinationDisplay { frontText }
      serviceJourney { id line { publicCode transportMode } }
    }
    fragment OnwardCall on EstimatedCall {
      realtime cancellation forAlighting
      aimedArrivalTime expectedArrivalTime
      quay { stopPlace { id name parent { id } } }
    }
    """
    return _graphql(query, {"ul": ulleval_id, "maj": majorstuen_id})


def _call_time(call: dict[str, Any], kind: str) -> datetime | None:
    value = call.get(f"expected{kind}Time") or call.get(f"aimed{kind}Time")
    return datetime.fromisoformat(value) if value else None


def _onward_call(call: dict[str, Any], stop_id: str) -> dict[str, Any] | None:
    for onward in (call.get("serviceJourneyEstimatedCalls") or {}).get("next") or []:
        place = ((onward.get("quay") or {}).get("stopPlace") or {})
        if stop_id in (place.get("id"), (place.get("parent") or {}).get("id")):
            if onward.get("cancellation") or onward.get("forAlighting") is False:
                return None
            return onward
    return None


def _select_commute_options(
    boards: dict[str, Any],
    majorstuen_id: str,
    ringstabekk_id: str,
    now: datetime,
    min_transfer_minutes: float,
    walk_minutes: float,
) -> list[dict[str, Any]]:
    ready_at = now + timedelta(minutes=walk_minutes)
    minimum_seconds = min_transfer_minutes * 60
    incoming = []
    for call in (boards.get("ulleval") or {}).get("estimatedCalls") or []:
        journey = call.get("serviceJourney") or {}
        line = journey.get("line") or {}
        departure = _call_time(call, "Departure")
        arrival_call = _onward_call(call, majorstuen_id)
        arrival = _call_time(arrival_call, "Arrival") if arrival_call else None
        if (
            call.get("cancellation") or call.get("forBoarding") is False
            or line.get("transportMode") != "metro"
            or not departure or departure < ready_at
            or not arrival or arrival <= departure
        ):
            continue
        incoming.append((call, departure, arrival_call, arrival))

    options = []
    seen_targets = set()
    targets = (boards.get("majorstuen") or {}).get("estimatedCalls") or []
    targets = sorted(targets, key=lambda c: (_call_time(c, "Departure") or now).timestamp())
    for target in targets:
        journey = target.get("serviceJourney") or {}
        line = journey.get("line") or {}
        target_dep = _call_time(target, "Departure")
        ring_call = _onward_call(target, ringstabekk_id)
        ring_arrival = _call_time(ring_call, "Arrival") if ring_call else None
        if (
            target.get("cancellation") or target.get("forBoarding") is False
            or line.get("transportMode") != "metro" or str(line.get("publicCode")) != "3"
            or not target_dep or target_dep <= now
            or not ring_arrival or ring_arrival <= target_dep
        ):
            continue
        identity = (journey.get("id"), target_dep.isoformat())
        if identity in seen_targets:
            continue
        seen_targets.add(identity)
        feasible = [
            (u, dep, arr_call, arr, (target_dep - arr).total_seconds())
            for u, dep, arr_call, arr in incoming
            if (target_dep - arr).total_seconds() >= minimum_seconds
        ]
        if not feasible:
            continue
        # Minimum wait for each upcoming line 3; latest Ullevål departure breaks ties.
        u, dep, arr_call, arr, margin_seconds = min(
            feasible, key=lambda item: (item[4], -item[1].timestamp())
        )
        u_line = (u.get("serviceJourney") or {}).get("line") or {}
        leave_work = dep - timedelta(minutes=walk_minutes)
        options.append({
            "leave_work_at": leave_work.isoformat(),
            "walk_to_station_minutes": walk_minutes,
            "ulleval_departure": dep.isoformat(),
            "ulleval_aimed_departure": u.get("aimedDepartureTime"),
            "ulleval_line": u_line.get("publicCode"),
            "ulleval_destination": (u.get("destinationDisplay") or {}).get("frontText"),
            "ulleval_realtime": bool(u.get("realtime")),
            "ulleval_service_journey_id": (u.get("serviceJourney") or {}).get("id"),
            "majorstuen_arrival": arr.isoformat(),
            "majorstuen_arrival_estimated": arr.isoformat(),
            "majorstuen_arrival_basis": "entur_expected" if arr_call.get("realtime") else "entur_scheduled",
            "majorstuen_arrival_realtime": bool(arr_call.get("realtime")),
            "line3_departure": target_dep.isoformat(),
            "line3_aimed_departure": target.get("aimedDepartureTime"),
            "line3_destination": (target.get("destinationDisplay") or {}).get("frontText"),
            "line3_realtime": bool(target.get("realtime")),
            "line3_service_journey_id": journey.get("id"),
            "ringstabekk_arrival": ring_arrival.isoformat(),
            "ringstabekk_arrival_realtime": bool(ring_call.get("realtime")),
            "transfer_margin_seconds": margin_seconds,
            "transfer_margin_minutes": round(margin_seconds / 60, 2),
        })
        if len(options) == 3:
            break
    return options


@mcp.tool()
def commute_home(
    min_transfer_minutes: float = 2,
    walk_to_station_minutes: float | None = None,
) -> dict[str, Any]:
    """FAST PATH for 'Dra fra jobb'. Return the next three metro connections Ullevål stadion → Majorstuen → Ringstabekk, with line 3 westbound. For each upcoming line 3, minimise waiting at Majorstuen while keeping at least min_transfer_minutes (default 2) between Entur arrival and departure. Subtract the configured walk from work to the Ullevål platform to give leave_work_at. Use real dated stop calls; do not assume a fixed travel time. The margin includes time to change platforms. Report the recommendation and two alternatives in chronological order."""
    walk = WORK_TO_ULLEVAL_MINUTES if walk_to_station_minutes is None else walk_to_station_minutes
    if not 0 <= walk <= 120 or not 2 <= min_transfer_minutes <= 60:
        raise ValueError("Gangtid må være 0–120 minutter og overgangsmargin 2–60 minutter.")
    ulleval = _search_stop("Ullevål stadion")
    majorstuen = _search_stop("Majorstuen")
    ringstabekk = _search_stop("Ringstabekk")
    boards = _commute_boards(ulleval["id"], majorstuen["id"])
    now = datetime.now().astimezone()
    options = _select_commute_options(
        boards, majorstuen["id"], ringstabekk["id"], now, min_transfer_minutes, walk
    )
    result = {
        "found": bool(options),
        "from_stop": ulleval["name"],
        "via_stop": majorstuen["name"],
        "to_stop": ringstabekk["name"],
        "min_transfer_minutes": min_transfer_minutes,
        "walk_to_station_minutes": walk,
        "number_of_options": len(options),
        "retrieved_at": datetime.now().astimezone().isoformat(),
        "note": "Overgangsmarginen er tiden fra ankomst til avgang på Majorstuen, inkludert plattformbytte. Tidene er Enturs forventede tider når sanntid finnes, ellers rutetider.",
    }
    if options:
        result["recommendation"] = options[0]
        result["alternatives"] = options[1:3]
    else:
        result["message"] = "Fant ingen kommende forbindelse med ønsket overgangsmargin og gangtid i søkevinduet."
        result["alternatives"] = []
    return result


def _switch_origin_train(ring_id: str, smestad_id: str, majorstuen_id: str) -> dict[str, Any] | None:
    query = """
    query NextRingTrain($id: String!) {
      stopPlace(id: $id) {
        estimatedCalls(numberOfDepartures: 30, timeRange: 10800,
          filters: [{select: [{transportModes: [{transportMode: metro}]}]}]) {
          realtime cancellation forBoarding
          aimedDepartureTime expectedDepartureTime
          destinationDisplay { frontText }
          serviceJourney { id line { publicCode transportMode } }
          serviceJourneyEstimatedCalls {
            next(count: 10) {
              realtime cancellation forAlighting
              aimedArrivalTime expectedArrivalTime
              quay { id stopPlace { id name parent { id } } }
            }
          }
        }
      }
    }
    """
    data = _graphql(query, {"id": ring_id})
    return _select_switch_origin_train(data, smestad_id, majorstuen_id, datetime.now().astimezone())


def _select_switch_origin_train(data: dict[str, Any], smestad_id: str, majorstuen_id: str, now: datetime) -> dict[str, Any] | None:
    calls = (data.get("stopPlace") or {}).get("estimatedCalls") or []
    calls = sorted(calls, key=lambda c: (_call_time(c, "Departure") or now).timestamp())
    for call in calls:
        departure = _call_time(call, "Departure")
        line = (call.get("serviceJourney") or {}).get("line") or {}
        if call.get("cancellation") or call.get("forBoarding") is False or not departure or departure < now or line.get("transportMode") != "metro":
            continue
        smestad = _onward_call(call, smestad_id)
        majorstuen = _onward_call(call, majorstuen_id)
        smestad_at = _call_time(smestad, "Arrival") if smestad else None
        majorstuen_at = _call_time(majorstuen, "Arrival") if majorstuen else None
        if smestad_at and majorstuen_at and departure < smestad_at < majorstuen_at:
            return {
                "line": line.get("publicCode"),
                "destination": (call.get("destinationDisplay") or {}).get("frontText"),
                "service_journey_id": (call.get("serviceJourney") or {}).get("id"),
                "ringstabekk_departure": departure.isoformat(),
                "ringstabekk_departure_realtime": bool(call.get("realtime")),
                "smestad_arrival": smestad_at.isoformat(),
                "smestad_arrival_realtime": bool(smestad.get("realtime")),
                "smestad_metro_stop_id": ((smestad.get("quay") or {}).get("stopPlace") or {}).get("id"),
                "majorstuen_arrival": majorstuen_at.isoformat(),
                "majorstuen_arrival_realtime": bool(majorstuen.get("realtime")),
            }
    return None


def _switch_trip_patterns(from_id: str, to_id: str, date_time: str, mode: str) -> list[dict[str, Any]]:
    # Starting at the metro child stop includes the actual walk to the bus stop.
    query = """
    query SwitchTrips($from: String!, $to: String!, $at: DateTime!, $mode: TransportMode!) {
      trip(from: {place: $from}, to: {place: $to}, dateTime: $at,
        modes: {accessMode: foot, egressMode: foot, directMode: foot,
          transportModes: [{transportMode: $mode}]}, numTripPatterns: 10) {
        tripPatterns {
          duration expectedStartTime expectedEndTime
          legs {
            mode realtime duration aimedStartTime expectedStartTime aimedEndTime expectedEndTime
            fromPlace { name } toPlace { name }
            line { publicCode name transportMode }
            serviceJourney { id }
            fromEstimatedCall { realtime cancellation destinationDisplay { frontText } }
            toEstimatedCall { realtime cancellation }
          }
        }
      }
    }
    """
    data = _graphql(query, {"from": from_id, "to": to_id, "at": date_time, "mode": _mode_from_filter(mode)})
    return [_normalise_trip(p) for p in (data.get("trip") or {}).get("tripPatterns") or []]


def _leg_time(leg: dict[str, Any], kind: str) -> datetime | None:
    value = leg.get(f"expected_{kind}_time") or leg.get(f"aimed_{kind}_time")
    return datetime.fromisoformat(value) if value else None


def _switch_metro_option(train: dict[str, Any], patterns: list[dict[str, Any]]) -> dict[str, Any] | None:
    maj_arrival = datetime.fromisoformat(train["majorstuen_arrival"])
    feasible = []
    for pattern in patterns:
        transit = _transit_legs(pattern)
        if len(transit) != 2 or any(l.get("cancelled") or (l.get("transport_mode") or l.get("mode")) != "metro" for l in transit):
            continue
        first, second = transit
        if first.get("from") != "Smestad" or first.get("to") != "Majorstuen" or second.get("from") != "Majorstuen" or second.get("to") != "Ullevål stadion":
            continue
        legs = pattern["legs"]
        first_index, second_index = legs.index(first), legs.index(second)
        walk_seconds = sum(l.get("duration_seconds") or 0 for l in legs[first_index + 1:second_index])
        # Evaluate the onward metro from the assumed train's arrival, never from
        # another train's arrival. This also handles a later planner first leg
        # sharing the same onward metro while the user stays on the original train.
        departure = _leg_time(second, "departure")
        arrival = _leg_time(second, "arrival")
        if not departure or not arrival or arrival <= departure or departure < maj_arrival + timedelta(seconds=walk_seconds):
            continue
        feasible.append({
            "line": second.get("line"), "destination": second.get("destination_display"),
            "majorstuen_departure": departure.isoformat(),
            "majorstuen_arrival": train["majorstuen_arrival"],
            "transfer_walk_seconds": walk_seconds,
            "transfer_margin_seconds": (departure - maj_arrival).total_seconds(),
            "ulleval_arrival": arrival.isoformat(),
            "departure_realtime": bool(second.get("departure_realtime")),
            "arrival_realtime": bool(second.get("arrival_realtime")),
            "service_journey_id": second.get("service_journey_id"),
        })
    return min(feasible, key=lambda p: datetime.fromisoformat(p["ulleval_arrival"])) if feasible else None


def _switch_bus_option(train: dict[str, Any], patterns: list[dict[str, Any]], buffer_minutes: float) -> dict[str, Any] | None:
    smestad_arrival = datetime.fromisoformat(train["smestad_arrival"])
    feasible = []
    for pattern in patterns:
        transit = _transit_legs(pattern)
        if len(transit) != 1:
            continue
        bus = transit[0]
        if bus.get("cancelled") or (bus.get("transport_mode") or bus.get("mode")) != "bus" or bus.get("from") != "Smestad" or bus.get("to") != "Ullevål stadion":
            continue
        initial = pattern["legs"][:pattern["legs"].index(bus)]
        # Reject paths without a verified walk from the metro child stop to the bus stop.
        walk_seconds = sum(l.get("duration_seconds") or 0 for l in initial)
        if walk_seconds <= 0:
            continue
        ready = smestad_arrival + timedelta(seconds=walk_seconds, minutes=buffer_minutes)
        departure = _leg_time(bus, "departure")
        arrival = _leg_time(bus, "arrival")
        if not departure or not arrival or departure < ready or arrival <= departure:
            continue
        feasible.append({
            "line": bus.get("line"), "destination": bus.get("destination_display"),
            "smestad_departure": departure.isoformat(),
            "aimed_departure": bus.get("aimed_departure_time"),
            "delay_seconds": pattern.get("delay_seconds"),
            "walk_to_bus_seconds": walk_seconds,
            "bus_ready_at": ready.isoformat(),
            "boarding_margin_after_walk_seconds": (departure - smestad_arrival).total_seconds() - walk_seconds,
            "ulleval_arrival": arrival.isoformat(),
            "departure_realtime": bool(bus.get("departure_realtime")),
            "arrival_realtime": bool(bus.get("arrival_realtime")),
            "service_journey_id": bus.get("service_journey_id"),
        })
    # Prefer a live option; a timetable-only result is shown as provisional.
    live = [p for p in feasible if p["departure_realtime"] and p["arrival_realtime"]]
    candidates = live or feasible
    return min(candidates, key=lambda p: datetime.fromisoformat(p["ulleval_arrival"])) if candidates else None


def _switch_decision(train: dict[str, Any], bus_patterns: list[dict[str, Any]], metro_patterns: list[dict[str, Any]], buffer_minutes: float) -> dict[str, Any]:
    bus = _switch_bus_option(train, bus_patterns, buffer_minutes)
    metro = _switch_metro_option(train, metro_patterns)
    gain = None
    recommendation = "stay_on_metro"
    reason = "no_reachable_bus"
    if not metro:
        recommendation, reason = "undetermined", "metro_comparison_unavailable"
    elif bus:
        gain = (datetime.fromisoformat(metro["ulleval_arrival"]) - datetime.fromisoformat(bus["ulleval_arrival"])).total_seconds()
        if not bus["departure_realtime"] or not bus["arrival_realtime"]:
            reason = "bus_has_no_live_prediction"
        elif gain > 0:
            recommendation, reason = "switch_to_bus", "bus_arrives_earlier"
        else:
            reason = "metro_arrives_no_later"
    return {
        "found": bool(metro), "recommendation": recommendation, "reason": reason,
        "assumed_train": train, "bus": bus, "metro": metro,
        "bus_time_gain_seconds": gain,
        "bus_time_gain_minutes": round(gain / 60, 2) if gain is not None else None,
        "bus_buffer_minutes": buffer_minutes,
    }


@mcp.tool()
def switch_at_smestad(bus_buffer_minutes: float = 1) -> dict[str, Any]:
    """FAST PATH for 'Bytte?'. Assume the next citybound metro from Ringstabekk is the user's train. Compare its actual Smestad arrival with live direct buses to Ullevål stadion, including Entur's walk from the metro stop and one minute extra boarding margin. Compare with staying on that same train to Majorstuen and taking the earliest reachable metro to Ullevål. Recommend the bus only when reachable and live departure/arrival predictions show an earlier arrival; prefer metro on ties or missing bus realtime. Report the assumed Smestad arrival, both options and the time difference."""
    if not 0 <= bus_buffer_minutes <= 30:
        raise ValueError("Ekstra margin ved bussen må være 0–30 minutter.")
    ring, smestad, majorstuen, ulleval = [_search_stop(n) for n in ("Ringstabekk", "Smestad", "Majorstuen", "Ullevål stadion")]
    train = _switch_origin_train(ring["id"], smestad["id"], majorstuen["id"])
    if not train or not train.get("smestad_metro_stop_id"):
        return {"found": False, "recommendation": "undetermined", "message": "Fant ikke neste bane fra Ringstabekk mot Smestad og Majorstuen.", "retrieved_at": datetime.now().astimezone().isoformat()}
    metro_patterns = _switch_trip_patterns(train["smestad_metro_stop_id"], ulleval["id"], train["smestad_arrival"], "metro")
    bus_patterns = _switch_trip_patterns(train["smestad_metro_stop_id"], ulleval["id"], train["smestad_arrival"], "bus")
    result = _switch_decision(train, bus_patterns, metro_patterns, bus_buffer_minutes)
    result["retrieved_at"] = datetime.now().astimezone().isoformat()
    result["assumption"] = "Ankomst Smestad med neste bane fra Ringstabekk mot sentrum."
    return result


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
