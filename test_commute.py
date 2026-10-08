import copy
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch
import app

BASE = datetime.fromisoformat("2026-10-09T15:00:00+02:00")


def at(minutes, seconds=0):
    return (BASE + timedelta(minutes=minutes, seconds=seconds)).isoformat()


def call(dep, arr, *, code="5", stop="maj", seconds=0, identity=None, cancelled=False):
    return {
        "aimedDepartureTime": at(dep), "expectedDepartureTime": at(dep),
        "realtime": True, "cancellation": cancelled, "forBoarding": True,
        "serviceJourney": {"id": identity or str(dep), "line": {"publicCode": code, "transportMode": "metro"}},
        "destinationDisplay": {"frontText": "Kolsås" if code == "3" else "Ringen via Majorstuen"},
        "serviceJourneyEstimatedCalls": {"next": [{
            "expectedArrivalTime": at(arr, seconds), "aimedArrivalTime": at(arr, seconds),
            "quay": {"stopPlace": {"id": stop}}, "realtime": True,
            "cancellation": False, "forAlighting": True,
        }]},
    }


def boards(incoming, targets):
    return {"ulleval": {"estimatedCalls": incoming}, "majorstuen": {"estimatedCalls": targets}}


class CommuteTests(unittest.TestCase):
    def select(self, incoming, targets, *, walk=0, now=BASE, minimum=2):
        return app._select_commute_options(boards(incoming, targets), "maj", "ring", now, minimum, walk)

    def test_latest_feasible_arrival_minimises_wait(self):
        result = self.select([call(1, 6), call(4, 9), call(5, 10), call(6, 11)], [call(12, 27, code="3", stop="ring")])
        self.assertEqual(result[0]["ulleval_departure"], at(5))
        self.assertEqual(result[0]["transfer_margin_seconds"], 120)

    def test_margin_is_never_rounded_up_to_two_minutes(self):
        result = self.select([call(5, 10, seconds=1)], [call(12, 27, code="3", stop="ring")])
        self.assertEqual(result, [])

    def test_first_three_targets_are_chronological_and_not_last_in_window(self):
        arrivals = [call(d, d + 5) for d in (5, 20, 35, 50)]
        targets = [call(d, d + 15, code="3", stop="ring") for d in (57, 27, 12, 42)]
        result = self.select(arrivals, targets)
        self.assertEqual([r["line3_departure"] for r in result], [at(12), at(27), at(42)])

    def test_duplicate_targets_are_removed(self):
        target = call(12, 27, code="3", stop="ring")
        result = self.select([call(5, 10)], [target, copy.deepcopy(target)])
        self.assertEqual(len(result), 1)

    def test_walk_excludes_trains_that_cannot_be_reached_and_subtracts_from_leave_time(self):
        targets = [call(12, 27, code="3", stop="ring"), call(27, 42, code="3", stop="ring")]
        result = self.select([call(5, 10), call(20, 25)], targets, walk=6)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["leave_work_at"], at(14))

    def test_wrong_direction_bus_cancellations_and_non_alighting_are_excluded(self):
        wrong = call(5, 10, stop="north")
        bus = call(5, 10)
        bus["serviceJourney"]["line"]["transportMode"] = "bus"
        non_alighting = call(5, 10)
        non_alighting["serviceJourneyEstimatedCalls"]["next"][0]["forAlighting"] = False
        incoming = [wrong, bus, non_alighting, call(5, 10, cancelled=True)]
        self.assertEqual(self.select(incoming, [call(12, 27, code="3", stop="ring")]), [])
        self.assertEqual(self.select([call(5, 10)], [call(12, 27, code="3", stop="east")]), [])
        self.assertEqual(self.select([call(5, 10)], [call(12, 27, code="3", stop="ring", cancelled=True)]), [])

    def test_actual_arrival_used_instead_of_fixed_four_minutes(self):
        result = self.select([call(5, 11)], [call(12, 27, code="3", stop="ring")])
        self.assertEqual(result, [])

    def test_parent_station_ids_match(self):
        incoming, target = call(5, 10, stop="maj-child"), call(12, 27, code="3", stop="ring-child")
        incoming["serviceJourneyEstimatedCalls"]["next"][0]["quay"]["stopPlace"]["parent"] = {"id": "maj"}
        target["serviceJourneyEstimatedCalls"]["next"][0]["quay"]["stopPlace"]["parent"] = {"id": "ring"}
        self.assertEqual(len(self.select([incoming], [target])), 1)

    def test_expected_delays_change_selected_train(self):
        early, late = call(4, 9), call(5, 10)
        late["serviceJourneyEstimatedCalls"]["next"][0]["expectedArrivalTime"] = at(11)
        result = self.select([early, late], [call(12, 27, code="3", stop="ring")])
        self.assertEqual(result[0]["ulleval_departure"], at(4))

    def test_midnight_times_keep_dates(self):
        now = BASE.replace(hour=23, minute=59)
        incoming, target = call(5, 10), call(12, 27, code="3", stop="ring")
        for item in (incoming, target):
            for name in ("aimedDepartureTime", "expectedDepartureTime"):
                item[name] = (datetime.fromisoformat(item[name]) + timedelta(hours=9)).isoformat()
            onward = item["serviceJourneyEstimatedCalls"]["next"][0]
            for name in ("expectedArrivalTime", "aimedArrivalTime"):
                onward[name] = (datetime.fromisoformat(onward[name]) + timedelta(hours=9)).isoformat()
        result = self.select([incoming], [target], now=now)
        self.assertTrue(result[0]["line3_departure"].startswith("2026-10-10T00:12"))

    def test_default_threshold_and_legacy_three_minute_argument(self):
        with patch.object(app, "_search_stop", side_effect=lambda name: {"name": name, "id": name}), patch.object(app, "_commute_boards", return_value={}), patch.object(app, "_select_commute_options", return_value=[]) as select:
            self.assertEqual(app.commute_home()["min_transfer_minutes"], 2)
            self.assertEqual(app.commute_home(3)["min_transfer_minutes"], 3)
            self.assertEqual(select.call_count, 2)
        with self.assertRaises(ValueError):
            app.commute_home(1)


if __name__ == "__main__":
    unittest.main()
