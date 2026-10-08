import unittest
from unittest.mock import patch

import app


def pattern(*legs):
    return {"expectedStartTime": "2026-10-08T12:00:00+02:00", "legs": list(legs)}


def leg(mode, code=None):
    return {"mode": mode, "line": {"publicCode": code, "transportMode": mode} if code else None}


class TransportFilterTests(unittest.TestCase):
    def search(self, patterns, **kwargs):
        with patch.object(app, "_search_stop", side_effect=lambda name: {"id": name, "name": name}), patch.object(app, "_trip_patterns", return_value=patterns) as trips:
            result = app.next_departure("Smestad", "Ullevål stadion", **kwargs)
            return result, trips.call_args.kwargs["transport_mode"]

    def test_default_keeps_bus(self):
        result, mode = self.search([pattern(leg("bus", "23"))])
        self.assertIsNone(mode)
        self.assertEqual(result["line"], "23")
        self.assertTrue(result["direct"])

    def test_metro_requires_every_transit_leg_including_no_line_code(self):
        valid = pattern(leg("foot"), leg("metro", "3"), leg("foot"), leg("metro", "5"))
        result, mode = self.search([
            pattern(leg("bus", "23")),
            pattern(leg("metro", "3"), leg("bus")),
            pattern(leg("foot")), valid,
        ], transport_mode=" T-bane ")
        self.assertEqual(mode, "metro")
        self.assertTrue(result["found"])
        self.assertEqual(result["number_of_transit_legs"], 2)
        self.assertFalse(result["direct"])
        self.assertEqual(result["alternatives"], [])

    def test_explicit_and_legacy_bus_filters(self):
        for kwargs in ({"transport_mode": "buss"}, {"line": "bus"}):
            result, mode = self.search([pattern(leg("metro", "3")), pattern(leg("bus", "23"))], **kwargs)
            self.assertEqual(mode, "bus")
            self.assertEqual(result["line"], "23")

    def test_legacy_metro_and_line_number_with_mode(self):
        result, mode = self.search([pattern(leg("metro", "3"), leg("metro", "5"))], line="3", transport_mode="metro")
        self.assertEqual(mode, "metro")
        self.assertEqual(result["line_filter"], "3")
        result, mode = self.search([pattern(leg("metro", "3"))], line="bane")
        self.assertEqual(mode, "metro")
        self.assertIsNone(result["line_filter"])

    def test_no_match_never_falls_back(self):
        for kwargs in ({"transport_mode": "metro"}, {"line": "99"}):
            result, _ = self.search([pattern(leg("bus", "23"))], **kwargs)
            self.assertFalse(result["found"])

    def test_invalid_or_conflicting_filters_fail_before_network(self):
        with patch.object(app, "_search_stop") as lookup:
            for kwargs in ({"transport_mode": "spaceship"}, {"transport_mode": ""}, {"line": "bus", "transport_mode": "metro"}):
                with self.assertRaises(ValueError):
                    app.next_departure("Smestad", "Ullevål stadion", **kwargs)
            lookup.assert_not_called()

    def test_query_restricts_entur_and_keeps_default_query_unrestricted(self):
        for mode in (None, "metro", "bus"):
            with patch.object(app, "_graphql", return_value={"trip": {"tripPatterns": []}}) as graphql:
                app._trip_patterns("NSR:StopPlace:1", "NSR:StopPlace:2", transport_mode=mode)
                query = graphql.call_args.args[0]
                if mode:
                    self.assertIn(f"transportMode: {mode}", query)
                    self.assertIn("accessMode: foot", query)
                else:
                    self.assertNotIn("modes:", query)


if __name__ == "__main__":
    unittest.main()
