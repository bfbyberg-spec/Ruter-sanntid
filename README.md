# Ruter-sanntid

Liten privat MCP-bro for ChatGPT som henter stoppesteder og sanntidsavganger direkte fra Enturs åpne API-er.

## Verktøy
- `next_departure(from_stop, destination, line=None, transport_mode=None)` — ekte punkt-til-punkt-søk. `transport_mode="metro"` (eller `"T-bane"`/`"bane"`) tillater bare T-bane på alle kollektivstrekninger, med gange til/fra og ved bytte. Bruk `"bus"`/`"buss"`, `"tram"`/`"trikk"`, `"rail"`/`"tog"`, `"coach"`, `"water"`/`"ferge"` eller `"air"`/`"fly"` for andre transportmidler. Uten filter vurderes alle transportmidler som før.
- `line="3"` kan kombineres med `transport_mode="metro"`; reisen må inneholde linje 3, men bytter kan bruke andre T-banelinjer. Eldre kall med transportmiddel i `line`, for eksempel `line="bus"`, fungerer fortsatt. Ingen treff gir `found=false`; ugyldige eller motstridende transportfiltre gir en feil.
- `commute_home(min_transfer_minutes=3)` — eksisterende pendlersøk fra Ullevål via Majorstuen mot Kolsås.
- `search_stops(name)` — søker stoppested og returnerer NSR StopPlace-ID.
- `departures(stop_place_id)` — henter avgangstavle med planlagt og forventet/sanntidsbasert avgangstid.

## Drift
Kjøres som en Streamable HTTP MCP-server på Render.

## Datakilde
Entur Journey Planner v3 og Geocoder v3.

## Tester
Kjør `python -m unittest -v test_transport_filter` for transportfilter, bakoverkompatibilitet og ingen-treff-situasjoner.
