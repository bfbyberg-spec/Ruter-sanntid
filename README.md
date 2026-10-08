# Ruter-sanntid

Liten privat MCP-bro for ChatGPT som henter stoppesteder og sanntidsavganger direkte fra Enturs åpne API-er.

## Verktøy
- `search_stops(name)` — søker stoppested og returnerer NSR StopPlace-ID.
- `departures(stop_place_id)` — henter avgangstavle med planlagt og forventet/sanntidsbasert avgangstid.

## Drift
Kjøres som en Streamable HTTP MCP-server på Render.

## Datakilde
Entur Journey Planner v3 og Geocoder v3.
