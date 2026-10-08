# Ruter-sanntid

Liten privat MCP-bro for ChatGPT som henter stoppesteder og sanntidsavganger direkte fra Enturs åpne API-er.

## Verktøy
- `next_departure(from_stop, destination, line=None, transport_mode=None)` — ekte punkt-til-punkt-søk. `transport_mode="metro"` (eller `"T-bane"`/`"bane"`) tillater bare T-bane på alle kollektivstrekninger, med gange til/fra og ved bytte. Bruk `"bus"`/`"buss"`, `"tram"`/`"trikk"`, `"rail"`/`"tog"`, `"coach"`, `"water"`/`"ferge"` eller `"air"`/`"fly"` for andre transportmidler. Uten filter vurderes alle transportmidler som før.
- `line="3"` kan kombineres med `transport_mode="metro"`; reisen må inneholde linje 3, men bytter kan bruke andre T-banelinjer. Eldre kall med transportmiddel i `line`, for eksempel `line="bus"`, fungerer fortsatt. Ingen treff gir `found=false`; ugyldige eller motstridende transportfiltre gir en feil.
- `commute_home(min_transfer_minutes=2, walk_to_station_minutes=None)` — «Dra fra jobb»: tre kommende T-baneforbindelser fra Ullevål via Majorstuen til Ringstabekk. For hver kommende linje 3 velges minst mulig venting på Majorstuen, med minst to minutter fra ankomst til avgang (inkludert plattformbytte). Gangtid fra jobb til Ullevål-plattformen er fem minutter; kan overstyres med argumentet eller `WORK_TO_ULLEVAL_MINUTES`. Resultatet inneholder tidspunkt for å gå fra jobb, begge avganger, overgangsmargin og ankomst Ringstabekk. Faktiske daterte stoppsekvenser brukes i stedet for et fast kjøretidsanslag. Eksisterende kall med eksplisitt tre minutters margin støttes fortsatt.
- `switch_at_smestad(bus_buffer_minutes=1)` — «Bytte?»: antar ankomst Smestad med neste bane fra Ringstabekk mot sentrum. Sammenligner direkte buss fra Smestad med å bli på samme bane og bytte til T-bane på Majorstuen. Entur beregner gange fra selve T-banestasjonen til bussen; ett ekstra minutt legges til før bussen kan anbefales. Live avgang og ankomst for bussen må vise tidligere ankomst; ved lik tid eller manglende busssanntid anbefales T-banen. Hvis metroalternativet ikke kan beregnes, gis ingen sammenlignende anbefaling. `next_departure("Ringstabekk", "Ullevål stadion", line="bytte")` gir samme beregning for verter med eldre verktøykatalog.
- `search_stops(name)` — søker stoppested og returnerer NSR StopPlace-ID.
- `departures(stop_place_id)` — henter avgangstavle med planlagt og forventet/sanntidsbasert avgangstid.

## Drift
Kjøres som en Streamable HTTP MCP-server på Render.

## Datakilde
Entur Journey Planner v3 og Geocoder v3.

## Tester
Kjør `python -m unittest -v test_transport_filter test_commute test_switch` for transportfilter, pendleralternativer og byttevurdering, inkludert forsinkelser, gangtid, kanselleringer, manglende sanntid, samme antatte bane og avganger over midnatt.
