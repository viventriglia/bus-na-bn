import io
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from html import escape
from pathlib import Path
from unittest.mock import patch

from scripts import check_mottam_timetables as mot

FIXTURE = Path(__file__).parent / "fixtures" / "mottam.html"


def fixture():
    return FIXTURE.read_text(encoding="utf-8")


def table_rows():
    parser = mot.TableParser()
    parser.feed(fixture())
    return next(table for table in parser.tables if table[0] == ["ANDATA"])


def table_html(rows):
    return "<table>" + "".join("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>" for row in rows) + "</table>"


class DiscoveryTests(unittest.TestCase):
    def test_same_page_anchor(self):
        self.assertEqual(mot.discover_schedule(fixture(), mot.HOME_URL), mot.HOME_URL)

    def test_moved_schedule_page_is_discovered_by_label(self):
        html = '<a href="/orari/napoli-2027.html"><b>ORARI NAPOLI</b></a>'
        self.assertEqual(mot.discover_schedule(html, mot.HOME_URL), "https://www.mottam.com/orari/napoli-2027.html")

    def test_external_schedule_is_rejected(self):
        with self.assertRaises(mot.ScheduleError):
            mot.discover_schedule('<a href="https://example.com/">ORARI NAPOLI</a>', mot.HOME_URL)

    def test_conflicting_links_are_rejected(self):
        with self.assertRaises(mot.ScheduleError):
            mot.discover_schedule('<a href="a.html">ORARI NAPOLI</a><a href="b.html">ORARI NAPOLI</a>', mot.HOME_URL)

    def test_navigation_missing_still_requires_full_table(self):
        self.assertEqual(mot.discover_schedule("no links", mot.HOME_URL), mot.HOME_URL)
        with self.assertRaises(mot.ScheduleError):
            mot.parse_schedule("no links or table")

    def test_word_windows_1252_encoding(self):
        html = fixture().replace('charset="utf-8"', 'charset="windows-1252"')
        self.assertEqual(mot.parse_schedule(mot.decode_html(html.encode("cp1252"))), mot.parse_schedule(fixture()))


class ParserTests(unittest.TestCase):
    def test_all_six_journeys_and_twenty_four_stops(self):
        journeys = mot.parse_schedule(fixture())
        self.assertEqual([item["id"] for item in journeys], ["MOTTAM-A1", "MOTTAM-A2", "MOTTAM-A3", "MOTTAM-R1", "MOTTAM-R2", "MOTTAM-R3"])
        self.assertEqual(len(mot.csv_rows(journeys)), 24)
        self.assertEqual(journeys[0]["stops"], [(mot.MUSTILLI, "06:45"), (mot.BAR, "06:50"), (mot.GARIBALDI, "08:00"), (mot.DEPRETIS, "")])
        self.assertEqual(journeys[-1]["stops"], [(mot.DEPRETIS, "17:05"), (mot.MERIDIONALE, "17:20"), (mot.BAR, "18:20"), (mot.MUSTILLI, "18:25")])

    def test_cosmetic_changes_and_tariffs_do_not_change_timetables(self):
        modified = fixture().replace("6.45", "06:45").replace("€ 6,50", "€ 9,00").replace("via MUSTILLI", "via\n MUSTILLI")
        self.assertEqual(mot.parse_schedule(modified), mot.parse_schedule(fixture()))

    def test_added_journey_is_parsed_without_fixed_column_count(self):
        rows = table_rows()
        rows[4].append("CORSA 4 (dal lunedì al venerdì)")
        rows[5].append(rows[5][2].replace("17.05", "19.05").replace("17.20", "19.20").replace("18.20", "20.20").replace("18.25", "20.25"))
        result = mot.parse_schedule(table_html(rows))
        self.assertEqual(len(result), 7)
        self.assertEqual(result[-1]["id"], "MOTTAM-R4")

    def test_removed_journey_preserves_remaining_publisher_ids(self):
        rows = table_rows()
        del rows[1][1]
        del rows[2][1]
        result = mot.parse_schedule(table_html(rows))
        self.assertEqual(len(result), 5)
        self.assertEqual(result[1]["id"], "MOTTAM-A3")

    def test_missing_cell_is_not_treated_as_removed_journey(self):
        rows = table_rows()
        del rows[2][1]
        with self.assertRaises(mot.ScheduleError):
            mot.parse_schedule(table_html(rows))

    def test_unknown_stop_blocks_confirmation(self):
        with self.assertRaises(mot.ScheduleError):
            mot.parse_schedule(fixture().replace("Bar 2000", "Nuova fermata"))

    def test_invalid_or_descending_time_is_rejected(self):
        for new_time in ("25.45", "09.45", "6.99"):
            with self.subTest(new_time=new_time), self.assertRaises(mot.ScheduleError):
                mot.parse_schedule(fixture().replace("6.45", new_time))

    def test_two_schedule_tables_are_ambiguous(self):
        with self.assertRaises(mot.ScheduleError):
            mot.parse_schedule(fixture() + fixture())

    def test_missing_return_direction_is_rejected(self):
        with self.assertRaises(mot.ScheduleError):
            mot.parse_schedule(table_html(table_rows()[:3]))

    def test_duplicate_course_number_is_rejected(self):
        rows = table_rows()
        rows[1][1] = rows[1][0]
        with self.assertRaises(mot.ScheduleError):
            mot.parse_schedule(table_html(rows))

    def test_new_weekend_only_header_needs_manual_review(self):
        with self.assertRaises(mot.ScheduleError):
            mot.parse_schedule(fixture().replace("dal lunedì al sabato", "solo sabato"))


class ApplicationTests(unittest.TestCase):
    def run_isolated(self, html, online):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            folder = Path(temporary)
            originals = {}
            for constant in ("MOTTAM_PATH", "OUTPUT_PATH", "LINE_UPDATES_PATH", "CSV_PATH"):
                original = getattr(mot.generate_data, constant)
                target = folder / original.name
                shutil.copy2(original, target)
                originals[constant] = target.read_bytes()
                stack.enter_context(patch.object(mot.generate_data, constant, target))
            stack.enter_context(patch("sys.stdout", new=io.StringIO()))
            output = folder / "report"
            args = ["--apply", "--output-dir", str(output)]
            if online:
                stack.enter_context(patch.object(mot, "download", return_value=(html.encode("utf-8"), mot.HOME_URL)))
            else:
                source = folder / "input.html"
                source.write_text(html, encoding="utf-8")
                args += ["--html", str(source)]
            code = mot.main(args)
            after = {constant: getattr(mot.generate_data, constant).read_bytes() for constant in originals}
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            return code, report, originals, after

    def test_live_unchanged_confirmation_preserves_all_timetables(self):
        code, report, before, after = self.run_isolated(fixture(), online=True)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "unchanged")
        self.assertTrue(report["applied"])
        self.assertEqual(before["MOTTAM_PATH"], after["MOTTAM_PATH"])
        self.assertEqual(before["CSV_PATH"], after["CSV_PATH"])
        old_metadata = json.loads(before["LINE_UPDATES_PATH"])
        metadata = json.loads(after["LINE_UPDATES_PATH"])
        self.assertEqual(metadata["MOT.TAM"]["updated_at"], old_metadata["MOT.TAM"]["updated_at"])
        self.assertEqual(metadata["MOT.TAM"]["checked_at"], report["checked_at"])
        self.assertEqual(metadata["AIR"], old_metadata["AIR"])
        self.assertEqual(metadata["EAV"], old_metadata["EAV"])
        payload = json.loads(after["OUTPUT_PATH"].decode("utf-8").replace("\r\n", "\n").split(" = ", 1)[1].removesuffix(";\n"))
        self.assertEqual(payload["lineUpdates"], metadata)
        self.assertEqual(len(payload["journeys"]), 6)

    def test_local_html_does_not_advance_online_confirmation(self):
        code, report, before, after = self.run_isolated(fixture(), online=False)
        self.assertEqual(code, 0)
        self.assertFalse(report["applied"])
        self.assertEqual(before, after)

    def test_source_error_does_not_modify_any_file(self):
        code, report, before, after = self.run_isolated(fixture().replace("Bar 2000", "Unknown stop"), online=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "error")
        self.assertEqual(before, after)

    def test_time_change_updates_mottam_only(self):
        code, report, before, after = self.run_isolated(fixture().replace("ore 15.50", "ore 15.55"), online=True)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "changes")
        self.assertEqual(report["modified"][0]["id"], "MOTTAM-R1")
        self.assertEqual(before["CSV_PATH"], after["CSV_PATH"])
        metadata = json.loads(after["LINE_UPDATES_PATH"])
        self.assertEqual(metadata["MOT.TAM"]["updated_at"], report["checked_at"])
        self.assertIn(b"15:55", after["MOTTAM_PATH"])


if __name__ == "__main__":
    unittest.main()
