import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import check_air_timetables as air
from scripts import check_eav_timetables as eav
from scripts import generate_data as data

FIXTURES = Path(__file__).parent / "fixtures"


class CityStopsTests(unittest.TestCase):
    def air_tables(self):
        source = json.loads((FIXTURES / "air-mixed.json").read_text(encoding="utf-8"))
        return [air.extract_table(page["table"], i + 1) for i, page in enumerate(source["pages"])]

    def eav_table(self, index):
        rows = json.loads((FIXTURES / "eav-cells.json").read_text(encoding="utf-8"))["tables"][index]
        return SimpleNamespace(extract=lambda: [row["values"] for row in rows],
                               rows=[SimpleNamespace(cells=row["cells"]) for row in rows])

    def test_air_city_stops_keep_exact_column_times_and_original_endpoints(self):
        tables = self.air_tables()
        first = tables[0]["trips"][0]["stops"]
        self.assertEqual([stop["time"] for stop in first],
                         ["05:00", "05:02", "05:07", "05:09", "06:19", "06:28", "06:30"])
        self.assertEqual(first[1]["name"], "Benevento, Bar 2000")
        self.assertEqual(first[0]["name"], air.BENEVENTO)
        self.assertEqual(first[-1]["name"], air.NAPOLI)
        self.assertTrue(all(stop["name"].startswith(("Benevento", "Napoli")) for stop in first))
        reverse = tables[1]["trips"][0]["stops"]
        self.assertEqual(reverse[0]["name"], air.NAPOLI)
        self.assertEqual(reverse[-1]["name"], air.BENEVENTO)
        self.assertEqual([stop["time"] for stop in reverse],
                         ["06:30", "06:31", "06:40", "08:10", "08:19", "08:30"])

    def test_air_deduplication_keeps_distinct_stop_variants_and_exact_exclusions(self):
        tables = self.air_tables()
        duplicate = copy.deepcopy(tables[0]["trips"][0])
        duplicate["stops"][1]["time"] = "05:03"
        tables[0]["trips"].extend([duplicate, copy.deepcopy(duplicate)])
        trips, _ = air.apply_rules(tables, {"ignored_trips": []})
        details = air.stop_details_for(tables, trips)
        key = data.trip_key(air.Trip("benevento_napoli", "05:00", "06:30").csv_row())
        self.assertEqual(len(details[key]), 2)
        rules = {"ignored_trips": [{"direction": "benevento_napoli", "departure": "05:00",
                                   "arrival": "06:30", "reason": "Test"}]}
        allowed, _ = air.apply_rules(tables, rules)
        self.assertNotIn(key, air.stop_details_for(tables, allowed))

    def test_air_invalid_urban_time_blocks_update(self):
        source = json.loads((FIXTURES / "air-mixed.json").read_text(encoding="utf-8"))
        table = source["pages"][0]["table"]
        row = next(row for row in table if (row[0] or "").startswith("Benevento, Via Almerico"))
        row[1] = "07:00"
        with self.assertRaises(air.ScheduleError):
            air.extract_table(table, 1)

    def test_eav_fast_services_do_not_inherit_appia_or_liberta_from_bus_1(self):
        trips = eav.extract_table(self.eav_table(0), 1)["trips"]
        slow = next(trip for trip in trips if trip.number == "3775" and trip.category == "BUS 1")
        fast = next(trip for trip in trips if trip.number == "3775" and trip.category == "BUS 2 (veloce)")
        self.assertEqual([stop["time"] for stop in slow.stops], ["05:44", "05:50", "05:52", "07:22"])
        self.assertEqual([stop["time"] for stop in fast.stops], ["05:44", "07:03"])
        self.assertNotEqual(data.trip_key(slow.csv_row()), data.trip_key(fast.csv_row()))
        reverse = eav.extract_table(self.eav_table(1), 1)["trips"][0]
        self.assertEqual([stop["time"] for stop in reverse.stops], ["07:00", "08:31", "08:34", "08:39"])

    def test_sidecar_update_preserves_other_operator_and_generator_round_trip(self):
        air_trip = air.Trip("benevento_napoli", "05:00", "06:30")
        eav_trip = eav.EavTrip("benevento_napoli", "05:44", "07:03", "BUS 2 (veloce)", "3775")
        def endpoints(trip, directions):
            return [{"name": name, "time": time, "note": ""}
                    for name, time in zip(directions[trip.direction], (trip.departure, trip.arrival))]
        air_details = {data.trip_key(air_trip.csv_row()): [endpoints(air_trip, air.DIRECTIONS)]}
        eav_details = {data.trip_key(eav_trip.csv_row()): [endpoints(eav_trip, eav.DIRECTIONS)]}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with patch.object(data, "CSV_PATH", folder / "orari_eav_air.csv"):
                data.stops_path("EAV").write_text(json.dumps(eav_details), encoding="utf-8")
                result = data.prepare_stop_details("AIR", air_details, folder)
                self.assertEqual(result, {**eav_details, **air_details})
                self.assertEqual(data.read_stop_details(), result)
                payload = json.loads(data.schedule_js([air_trip.csv_row(), eav_trip.csv_row()], [], {}).split(" = ")[1][:-2])
                self.assertEqual(payload["stopDetails"], result)
                bad = copy.deepcopy(air_details)
                next(iter(bad.values()))[0][-1]["time"] = "07:00"
                data.stops_path("AIR").write_text(json.dumps(bad), encoding="utf-8")
                with self.assertRaises(ValueError):
                    data.read_stop_details()

    def test_rollback_removes_new_sidecar_when_later_write_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            output = folder / "proposal"
            output.mkdir()
            sidecar, javascript = folder / "air_stops.json", folder / "data.js"
            javascript.write_bytes(b"old")
            (output / sidecar.name).write_bytes(b"new")
            (output / javascript.name).write_bytes(b"new")
            replace = air.replace_bytes
            def fail(path, content):
                if path == javascript:
                    raise OSError("Test")
                replace(path, content)
            with patch.object(air, "replace_bytes", side_effect=fail), self.assertRaises(OSError):
                air.apply_proposal(output, {sidecar: None, javascript: b"old"})
            self.assertFalse(sidecar.exists())
            self.assertEqual(javascript.read_bytes(), b"old")

    def test_air_change_to_only_an_intermediate_stop_is_applied_and_updates_modification_date(self):
        tables = self.air_tables()
        rules = json.loads(air.RULES_PATH.read_text(encoding="utf-8"))
        trips, _ = air.apply_rules(tables, rules)
        previous = data.read_line_updates()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            current, javascript, metadata = [folder / name for name in ("orari_eav_air.csv", "data.js", "line_updates.json")]
            metadata.write_text(json.dumps(previous), encoding="utf-8")
            with patch.object(data, "CSV_PATH", current), patch.object(data, "OUTPUT_PATH", javascript), \
                 patch.object(data, "LINE_UPDATES_PATH", metadata), patch.object(data, "read_mottam_journeys", return_value=[]), \
                 patch.object(air, "CURRENT_CSV", current):
                air.prepare_proposal([], trips, folder, previous, air.stop_details_for(tables, trips))
                csv_before = current.read_bytes()
                tables[0]["trips"][0]["stops"][1]["time"] = "05:03"
                with patch.object(air, "download", side_effect=[(b"html", rules["index_url"]), (b"%PDF-test", "https://aircampania.it/current.pdf")]), \
                     patch.object(air, "discover_pdf", return_value="https://aircampania.it/current.pdf"), \
                     patch.object(air, "parse_pdf", return_value=(tables, air.date(2026, 10, 19))), \
                     patch("sys.stdout", new=io.StringIO()):
                    code = air.main(["--apply", "--output-dir", str(folder / "report")])
                self.assertEqual(code, 0)
                report = json.loads((folder / "report" / "report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["added"], [])
                self.assertEqual(report["removed"], [])
                self.assertTrue(report["stops_changed"])
                self.assertTrue(report["applied"])
                self.assertEqual(current.read_bytes(), csv_before)
                updates = data.read_line_updates()
                self.assertEqual(updates["AIR"]["updated_at"], report["checked_at"])
                self.assertEqual(updates["EAV"], previous["EAV"])
                self.assertEqual(updates["MOT.TAM"], previous["MOT.TAM"])


if __name__ == "__main__":
    unittest.main()
