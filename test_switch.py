import copy
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch
import app

BASE = datetime.fromisoformat("2026-10-09T07:00:00+02:00")


def at(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat()


TRAIN = {"line": "3", "service_journey_id": "current-train", "smestad_arrival": at(0), "majorstuen_arrival": at(300)}


def transit(mode, origin, destination, departure, arrival, *, live=True, cancelled=False):
    return {"mode": mode, "transport_mode": mode, "from": origin, "to": destination,
            "line": "23" if mode == "bus" else "3" if origin == "Smestad" else "5",
            "expected_departure_time": at(departure), "expected_arrival_time": at(arrival),
            "departure_realtime": live, "arrival_realtime": live,
            "service_journey_id": "current-train" if origin == "Smestad" and mode == "metro" else str(departure),
            "cancelled": cancelled}


def bus(dep=300, arrival=720, *, walk=103, live=True, cancelled=False):
    return {"legs": [{"mode": "foot", "duration_seconds": walk}, transit("bus", "Smestad", "Ullevål stadion", dep, arrival, live=live, cancelled=cancelled)], "delay_seconds": 0}


def metro(dep=600, arrival=900, *, walk=152, first_arrival=300):
    return {"legs": [transit("metro", "Smestad", "Majorstuen", 0, first_arrival), {"mode": "foot", "duration_seconds": walk}, transit("metro", "Majorstuen", "Ullevål stadion", dep, arrival)]}


class SwitchTests(unittest.TestCase):
    def decide(self, buses, metros=None):
        return app._switch_decision(TRAIN, buses, [metro()] if metros is None else metros, 1)

    def test_faster_live_bus_recommended(self):
        result = self.decide([bus()])
        self.assertEqual(result["recommendation"], "switch_to_bus")
        self.assertEqual(result["bus_time_gain_seconds"], 180)

    def test_delayed_bus_and_tie_favour_metro(self):
        for arrival in (900, 1000):
            result = self.decide([bus(arrival=arrival)])
            self.assertEqual(result["recommendation"], "stay_on_metro")

    def test_walking_and_extra_minute_are_required_before_boarding(self):
        self.assertEqual(self.decide([bus(dep=162)])["reason"], "no_reachable_bus")
        result = self.decide([bus(dep=163)])
        self.assertEqual(result["recommendation"], "switch_to_bus")
        self.assertEqual(result["bus"]["boarding_margin_after_walk_seconds"], 60)

    def test_zero_walk_parent_stop_shortcut_is_not_recommended(self):
        self.assertEqual(self.decide([bus(walk=0)])["reason"], "no_reachable_bus")

    def test_cancelled_or_wrong_direction_bus_is_excluded(self):
        wrong = bus()
        wrong["legs"][1]["to"] = "Lysaker"
        self.assertEqual(self.decide([bus(cancelled=True), wrong])["reason"], "no_reachable_bus")

    def test_missing_live_prediction_is_explicit_and_favours_metro(self):
        result = self.decide([bus(live=False)])
        self.assertEqual(result["recommendation"], "stay_on_metro")
        self.assertEqual(result["reason"], "bus_has_no_live_prediction")

    def test_reachable_live_alternative_is_used(self):
        result = self.decide([bus(dep=150, arrival=600), bus(dep=300, arrival=720)])
        self.assertEqual(result["bus"]["smestad_departure"], at(300))

    def test_metro_reachability_uses_current_train_not_an_earlier_planner_train(self):
        result = self.decide([bus()], [metro(dep=400, first_arrival=100)])
        self.assertFalse(result["found"])
        self.assertEqual(result["recommendation"], "undetermined")

    def test_later_planner_first_leg_can_share_reachable_onward_train(self):
        pattern = metro(dep=600, first_arrival=420)
        pattern["legs"][0]["service_journey_id"] = "later-train"
        result = self.decide([bus()], [pattern])
        self.assertEqual(result["metro"]["majorstuen_arrival"], at(300))
        self.assertEqual(result["metro"]["transfer_margin_seconds"], 300)

    def test_earliest_arrival_matters_more_than_first_departure(self):
        result = self.decide([bus(dep=300, arrival=850), bus(dep=360, arrival=780)], [metro(dep=600, arrival=1000), metro(dep=630, arrival=900)])
        self.assertEqual(result["bus"]["ulleval_arrival"], at(780))
        self.assertEqual(result["metro"]["ulleval_arrival"], at(900))

    def test_compatibility_entry_point_preserves_other_requests(self):
        with patch.object(app, "switch_at_smestad", return_value={"recommendation": "stay_on_metro"}) as switch:
            result = app.next_departure("Ringstabekk", "Ullevål stadion", line="Bytte?")
            self.assertEqual(result["recommendation"], "stay_on_metro")
            switch.assert_called_once_with()
        with self.assertRaises(ValueError):
            app.next_departure("Smestad", "Ullevål stadion", line="bytte")

    def test_next_ring_train_is_future_citybound_and_not_cancelled(self):
        def call(dep, *, cancelled=False, west=False):
            stops = ("west",) if west else ("sm", "maj")
            return {"expectedDepartureTime": at(dep), "cancellation": cancelled,
                    "serviceJourney": {"id": str(dep), "line": {"publicCode": "3", "transportMode": "metro"}},
                    "serviceJourneyEstimatedCalls": {"next": [{"expectedArrivalTime": at(dep + 600 + index * 300), "quay": {"stopPlace": {"id": stop}}, "forAlighting": True} for index, stop in enumerate(stops)]}}
        data = {"stopPlace": {"estimatedCalls": [call(600), call(-60), call(60, cancelled=True), call(120, west=True), call(180)]}}
        result = app._select_switch_origin_train(data, "sm", "maj", BASE)
        self.assertEqual(result["ringstabekk_departure"], at(180))

    def test_night_comparison_uses_full_dates(self):
        train = {**TRAIN, "smestad_arrival": "2026-10-09T23:59:00+02:00", "majorstuen_arrival": "2026-10-10T00:04:00+02:00"}
        b, m = bus(), metro()
        shift = datetime.fromisoformat(train["smestad_arrival"]) - BASE
        for pattern in (b, m):
            for leg in pattern["legs"]:
                for key in ("expected_departure_time", "expected_arrival_time"):
                    if key in leg:
                        leg[key] = (datetime.fromisoformat(leg[key]) + shift).isoformat()
        result = app._switch_decision(train, [b], [m], 1)
        self.assertEqual(result["recommendation"], "switch_to_bus")
        self.assertEqual(result["bus_time_gain_seconds"], 180)


if __name__ == "__main__":
    unittest.main()
