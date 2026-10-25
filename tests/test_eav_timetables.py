import io
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts import check_eav_timetables as eav
from scripts import check_air_timetables as air

FIXTURE = Path(__file__).parent / "fixtures" / "eav-cells.json"


def fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def make_table(rows):
    return SimpleNamespace(extract=lambda: [row["values"] for row in rows],
                           rows=[SimpleNamespace(cells=row["cells"]) for row in rows])


def mock_pdf(payload):
    document = MagicMock()
    page = SimpleNamespace(extract_text=lambda: payload["text"],
                           find_tables=lambda: [make_table(rows) for rows in payload["tables"]])
    document.__enter__.return_value = SimpleNamespace(pages=[page])
    return SimpleNamespace(open=lambda path: document)


def parse_fixture(payload=None, expected_date=None):
    with patch.dict("sys.modules", {"pdfplumber": mock_pdf(payload or fixture())}):
        return eav.parse_pdf(Path("example.pdf"), date(2026, 10, 25), expected_date)


def card(day=14, month="settembre", year=2026, slug="new-schedule", wording="autobus sostitutivi"):
    return (f'<article><h5>Orari {wording} linea Benevento-Napoli in vigore dal {day} {month} {year}</h5>'
            f'<a class="read-more" href="/download/{slug}/">Leggi di più</a></article>')


class DiscoveryTests(unittest.TestCase):
    def test_new_date_and_slug_are_discovered(self):
        url, starts_on = eav.discover_detail(card(day=1, month="ottobre", slug="renamed-2026"), eav.INDEX_URL, date(2026, 10, 25))
        self.assertEqual(url, "https://www.eavsrl.it/download/renamed-2026/")
        self.assertEqual(starts_on, date(2026, 10, 1))

    def test_latest_current_document_and_not_future_is_selected(self):
        html = card(day=1, month="agosto", slug="old") + card(slug="current") + card(day=1, month="novembre", slug="future")
        self.assertEqual(eav.discover_detail(html, eav.INDEX_URL, date(2026, 10, 25))[0], "https://www.eavsrl.it/download/current/")

    def test_future_only_document_is_not_applied(self):
        with self.assertRaises(eav.ScheduleError):
            eav.discover_detail(card(day=1, month="novembre"), eav.INDEX_URL, date(2026, 10, 25))

    def test_same_effective_date_with_two_documents_is_ambiguous(self):
        with self.assertRaises(eav.ScheduleError):
            eav.discover_detail(card(slug="one") + card(slug="two"), eav.INDEX_URL, date(2026, 10, 25))

    def test_train_schedule_does_not_match_bus_schedule(self):
        with self.assertRaises(eav.ScheduleError):
            eav.discover_detail(card(wording="treni"), eav.INDEX_URL, date(2026, 10, 25))

    def test_download_uses_current_id_and_refresh(self):
        html = '<a href="#" class="wpdm-download-link" data-downloadurl="/download/new/?wpdmdl=99999&amp;refresh=NEW">Download</a>'
        self.assertEqual(eav.discover_download(html, "https://www.eavsrl.it/download/new/"),
                         "https://www.eavsrl.it/download/new/?wpdmdl=99999&refresh=NEW")

    def test_missing_or_foreign_download_is_rejected(self):
        for html in ("no button", '<a class="wpdm-download-link" data-downloadurl="https://example.com/?wpdmdl=1">Download</a>'):
            with self.subTest(html=html), self.assertRaises(eav.ScheduleError):
                eav.discover_download(html, "https://www.eavsrl.it/")


class PdfTests(unittest.TestCase):
    def test_real_pdf_cells_produce_all_28_services_in_two_categories(self):
        trips, tables, starts_on = parse_fixture()
        self.assertEqual(starts_on, date(2026, 9, 14))
        self.assertEqual(len(trips), 28)
        self.assertEqual(sum(trip.category == "BUS 2 (veloce)" for trip in trips), 7)
        self.assertEqual(sum(trip.direction == "benevento_napoli" for trip in trips), 16)
        self.assertEqual(sum(trip.direction == "napoli_benevento" for trip in trips), 12)
        self.assertEqual(sum(len(table["partial"]) for table in tables), 4)

    def test_two_categories_with_identical_number_and_times_are_distinct(self):
        payload = fixture()
        destination = payload["tables"][0][-1]["values"]
        destination[8] = destination[7]
        trips, _, _ = parse_fixture(payload)
        same = [trip for trip in trips if trip.number == "3775"]
        self.assertEqual(len(same), 2)
        self.assertEqual({trip.arrival for trip in same}, {"07:22"})
        self.assertEqual({trip.category for trip in same}, {"BUS 1", "BUS 2 (veloce)"})

    def test_notes_are_assigned_by_physical_cells_not_forward_filled(self):
        trips, _, _ = parse_fixture()
        late = next(trip for trip in trips if trip.direction == "benevento_napoli" and trip.departure == "18:49")
        self.assertIn("30 giugno", late.notes)
        self.assertIn("24 e il 31 dicembre", late.notes)
        fast = next(trip for trip in trips if trip.direction == "benevento_napoli" and trip.number == "3791" and trip.category == "BUS 2 (veloce)")
        self.assertEqual(fast.notes, "")

    def test_partial_services_do_not_acquire_times_from_adjacent_columns(self):
        trips, _, _ = parse_fixture()
        self.assertFalse(any(trip.number in {"3770", "3792"} for trip in trips))

    def test_changed_validity_legend_blocks_confirmation(self):
        payload = fixture()
        payload["text"] = payload["text"].replace("30 giugno", "1 luglio")
        with self.assertRaises(eav.ScheduleError):
            parse_fixture(payload)

    def test_document_date_must_match_selected_detail(self):
        with self.assertRaises(eav.ScheduleError):
            parse_fixture(expected_date=date(2026, 9, 15))

    def test_missing_direction_and_scans_are_rejected(self):
        payload = fixture()
        payload["tables"].pop()
        with self.assertRaises(eav.ScheduleError):
            parse_fixture(payload)
        payload = fixture()
        payload.update({"text": "", "tables": []})
        with self.assertRaises(eav.ScheduleError):
            parse_fixture(payload)

    def test_unknown_or_missing_category_is_not_treated_as_cancellation(self):
        for label in ("BUS 3", ""):
            payload = fixture()
            payload["tables"][0][3]["values"][8] = label
            with self.subTest(label=label), self.assertRaises(eav.ScheduleError):
                parse_fixture(payload)

    def test_legacy_rows_are_enriched_without_reclassifying_old_services_as_removed(self):
        trips, _, _ = parse_fixture()
        slow = [trip for trip in trips if trip.category == "BUS 1" and trip.departure != "18:49"]
        rows = [trip.csv_row()[:5] for trip in slow]
        current, enriched = eav.current_trips(rows, trips)
        self.assertEqual(enriched, 20)
        self.assertEqual(len(set(trips) - set(current)), 8)
        self.assertEqual(set(current) - set(trips), set())


class ApplicationTests(unittest.TestCase):
    def run_isolated(self, pdf_bytes=b"%PDF-test"):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            folder = Path(temporary)
            before = {}
            for constant in ("CSV_PATH", "MOTTAM_PATH", "OUTPUT_PATH", "LINE_UPDATES_PATH"):
                original = getattr(eav.generate_data, constant)
                target = folder / original.name
                shutil.copy2(original, target)
                before[constant] = target.read_bytes()
                stack.enter_context(patch.object(eav.generate_data, constant, target))
            stack.enter_context(patch("sys.stdout", new=io.StringIO()))
            detail = '<a class="wpdm-download-link" data-downloadurl="https://www.eavsrl.it/download/new/?wpdmdl=12345&amp;refresh=current">Download</a>'
            stack.enter_context(patch.object(eav, "download", side_effect=[
                (card().encode("utf-8"), eav.INDEX_URL),
                (detail.encode("utf-8"), "https://www.eavsrl.it/download/new/"),
                (pdf_bytes, "https://www.eavsrl.it/download/new/?wpdmdl=12345&refresh=current"),
            ]))
            stack.enter_context(patch.dict("sys.modules", {"pdfplumber": mock_pdf(fixture())}))
            output = folder / "report"
            code = eav.main(["--apply", "--as-of", "2026-10-25", "--output-dir", str(output)])
            after = {constant: getattr(eav.generate_data, constant).read_bytes() for constant in before}
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            if code == 0:
                # A later AIR refresh must preserve every EAV category/number/note.
                rows, air_trips = air.read_current(eav.generate_data.CSV_PATH)
                proposal = folder / "air-proposal"
                proposal.mkdir()
                air.prepare_proposal(rows, air_trips, proposal)
                stack.enter_context(patch.object(eav.generate_data, "CSV_PATH", proposal / "orari_eav_air.csv"))
                after_air = eav.generate_data.read_trips()
                self.assertEqual([row for row in rows if row[4] == "EAV"], [row for row in after_air if row[4] == "EAV"])
            return code, report, before, after

    def test_full_update_preserves_air_and_mottam_and_updates_eav_confirmation(self):
        code, report, before, after = self.run_isolated()
        self.assertEqual(code, 0)
        self.assertTrue(report["applied"])
        self.assertEqual(report["trip_count"], 28)
        self.assertEqual(before["MOTTAM_PATH"], after["MOTTAM_PATH"])
        old = json.loads(before["LINE_UPDATES_PATH"])
        new = json.loads(after["LINE_UPDATES_PATH"])
        self.assertEqual(new["AIR"], old["AIR"])
        self.assertEqual(new["MOT.TAM"], old["MOT.TAM"])
        self.assertEqual(new["EAV"]["checked_at"], report["checked_at"])
        payload = json.loads(after["OUTPUT_PATH"].decode("utf-8").replace("\r\n", "\n").split(" = ", 1)[1].removesuffix(";\n"))
        self.assertEqual(payload["columns"], eav.generate_data.TRIP_COLUMNS)
        self.assertEqual(sum(row[4] == "EAV" for row in payload["trips"]), 28)
        self.assertEqual(sum(row[4] == "AIR" for row in payload["trips"]), 28)

    def test_html_download_instead_of_pdf_never_changes_data(self):
        code, report, before, after = self.run_isolated(b"<html>download expired</html>")
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "error")
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
