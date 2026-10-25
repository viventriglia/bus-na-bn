import json
import io
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts import check_air_timetables as air


def example_table(reverse=False):
    origin, destination = (air.NAPOLI, air.BENEVENTO) if reverse else (air.BENEVENTO, air.NAPOLI)
    return [
        ["Validità:", "A6_FE6", "NC", "A7_FE6", "A6_FE6"],
        [origin, "06:00", "", "06:00", "07:00"],
        ["Fermata intermedia", "06:30", "07:30", "06:30", "07:30"],
        [destination, "08:00", "09:00", "08:00", "09:00"],
    ]


class DiscoveryTests(unittest.TestCase):
    def test_link_survives_month_and_filename_change(self):
        html = '<a href="/wp-content/uploads/2027/02/nuovi-orari-12.pdf?rev=2">' \
               '<strong>01-BN |</strong> Benevento - Napoli <b>feriale</b></a>'
        self.assertEqual(
            air.discover_pdf(html, "https://aircampania.it/linee-e-orari/"),
            "https://aircampania.it/wp-content/uploads/2027/02/nuovi-orari-12.pdf?rev=2",
        )

    def test_ignores_holiday_and_other_routes(self):
        html = ('<a href="holiday.pdf">01-BN Benevento Napoli festiva</a>'
                '<a href="other.pdf">02-BN Benevento Napoli feriale</a>'
                '<a href="weekday.pdf">01-BN Benevento Napoli feriale</a>')
        self.assertTrue(air.discover_pdf(html, "https://aircampania.it/").endswith("weekday.pdf"))

    def test_missing_link_fails_instead_of_reusing_old_pdf(self):
        with self.assertRaises(air.ScheduleError):
            air.discover_pdf("<p>Pagina cambiata</p>", "https://aircampania.it/")

    def test_two_different_weekday_pdfs_are_ambiguous(self):
        html = ''.join(f'<a href="{name}.pdf">01-BN Benevento Napoli feriale</a>' for name in ("a", "b"))
        with self.assertRaises(air.ScheduleError):
            air.discover_pdf(html, "https://aircampania.it/")

    def test_duplicate_link_is_not_ambiguous(self):
        html = '<a href="a.pdf">01-BN Benevento Napoli feriale</a>' * 2
        self.assertTrue(air.discover_pdf(html, "https://aircampania.it/").endswith("a.pdf"))

    def test_external_link_rejected(self):
        html = '<a href="https://example.com/a.pdf">01-BN Benevento Napoli feriale</a>'
        with self.assertRaises(air.ScheduleError):
            air.discover_pdf(html, "https://aircampania.it/")


class TableTests(unittest.TestCase):
    def test_blank_cells_do_not_shift_column_pairing(self):
        parsed = air.extract_table(example_table(), 1)
        self.assertEqual(
            [(item["departure"], item["arrival"]) for item in parsed["trips"]],
            [("06:00", "08:00"), ("06:00", "08:00"), ("07:00", "09:00")],
        )
        self.assertEqual(parsed["incomplete_columns"], [{"column": 3, "departure": None, "arrival": "09:00"}])

    def test_shifted_label_column_supported(self):
        table = [[None, *row] for row in example_table()]
        self.assertEqual(len(air.extract_table(table, 1)["trips"]), 3)

    def test_duplicate_journeys_are_collapsed_after_rules(self):
        tables = [air.extract_table(example_table(), 1), air.extract_table(example_table(True), 1)]
        trips, _ = air.apply_rules(tables, {"ignored_trips": []})
        self.assertEqual(len(trips), 4)

    def test_invalid_duration_blocks_proposal(self):
        table = example_table()
        table[-1][1] = "05:00"
        with self.assertRaises(air.ScheduleError):
            air.extract_table(table, 1)

    def test_missing_validity_header_blocks_proposal(self):
        with self.assertRaises(air.ScheduleError):
            air.extract_table(example_table()[1:], 1)

    def test_source_typo_in_intermediate_stop_is_reported(self):
        table = example_table()
        table[2][1] = "09:30"
        parsed = air.extract_table(table, 1)
        self.assertTrue(parsed["warnings"])
        self.assertEqual(parsed["trips"][0]["arrival"], "08:00")

    def test_manual_exclusions_match_exact_journey_not_departure_only(self):
        tables = [air.extract_table(example_table(), 1), air.extract_table(example_table(True), 1)]
        rules = {"ignored_trips": [{"direction": "benevento_napoli", "departure": "06:00",
                                   "arrival": "08:00", "reason": "Scelta utente"}]}
        trips, exclusions = air.apply_rules(tables, rules)
        self.assertNotIn(air.Trip("benevento_napoli", "06:00", "08:00"), trips)
        self.assertIn(air.Trip("napoli_benevento", "06:00", "08:00"), trips)
        self.assertEqual(len(exclusions), 2)

    def test_time_normalization_and_bad_tokens(self):
        self.assertEqual(air.parse_time("*5.55*"), "05:55")
        self.assertIsNone(air.parse_time("|"))
        for value in ("25:00", "07:65", "07:00 08:00", "non leggibile"):
            with self.subTest(value=value), self.assertRaises(air.ScheduleError):
                air.parse_time(value)

    def test_effective_date_from_document_not_upload_path(self):
        self.assertEqual(air.effective_date("Orari in vigore dal 15 Settembre 2026"), date(2026, 9, 15))


class ProposalTests(unittest.TestCase):
    def test_apply_replaces_both_validated_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            output = folder / "proposal"
            output.mkdir()
            originals = {}
            for name in ("orari_eav_air.csv", "data.js"):
                target = folder / name
                target.write_bytes(b"old data")
                originals[target] = b"old data"
                (output / name).write_bytes(b"new data")
            air.apply_proposal(output, originals)
            self.assertTrue(all(path.read_bytes() == b"new data" for path in originals))

    def test_apply_refuses_to_overwrite_concurrent_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "data.js"
            target.write_bytes(b"user edit")
            with self.assertRaises(air.ScheduleError):
                air.apply_proposal(Path(temporary), {target: b"old data"})
            self.assertEqual(target.read_bytes(), b"user edit")

    def test_apply_rolls_back_if_second_file_cannot_be_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            output = folder / "proposal"
            output.mkdir()
            originals = {}
            for name in ("orari_eav_air.csv", "data.js"):
                target = folder / name
                target.write_bytes(b"original")
                originals[target] = b"original"
                (output / name).write_bytes(b"new data")
            replace = air.replace_bytes

            def fail_on_javascript(path, data):
                if path.name == "data.js":
                    raise OSError("Test write failure")
                replace(path, data)

            with patch.object(air, "replace_bytes", side_effect=fail_on_javascript):
                with self.assertRaises(OSError):
                    air.apply_proposal(output, originals)
            self.assertTrue(all(path.read_bytes() == b"original" for path in originals))

    def test_generation_failure_sets_error_and_removes_stale_proposals(self):
        tables = [air.extract_table(example_table(), 1), air.extract_table(example_table(True), 1)]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            pdf = folder / "input.pdf"
            pdf.write_bytes(b"%PDF-test")
            output = folder / "review"
            output.mkdir()
            (output / "data.js").write_text("stale proposal", encoding="utf-8")
            with patch.object(air, "parse_pdf", return_value=(tables, date(2026, 9, 15))), \
                 patch.object(air, "prepare_proposal", side_effect=OSError("Test disk error")), \
                 patch("sys.stdout", new=io.StringIO()):
                result = air.main(["--pdf", str(pdf), "--output-dir", str(output), "--as-of", "2026-10-25"])
            self.assertEqual(result, 1)
            self.assertEqual(json.loads((output / "report.json").read_text(encoding="utf-8"))["status"], "error")
            self.assertFalse((output / "data.js").exists())

    def test_proposal_preserves_eav_and_mottam(self):
        eav = ["06:20", "08:02", "Stazione BN", "Stazione NA", "EAV"]
        old_air = ["07:00", "09:00", air.BENEVENTO, air.NAPOLI, "AIR"]
        mottam = [{"id": "MOTTAM-A1", "stops": [{"name": "Benevento, Bar 2000", "time": "06:50"}]}]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(air.generate_data, "read_mottam_journeys", return_value=mottam):
                air.prepare_proposal([eav, old_air], [air.Trip("benevento_napoli", "08:30", "10:30")], output)
            text = (output / "data.js").read_text(encoding="utf-8")
            payload = json.loads(text.split(" = ", 1)[1].removesuffix(";\n"))
            self.assertEqual(payload["trips"][0], eav)
            self.assertEqual(payload["journeys"], mottam)
            self.assertEqual(payload["trips"][1][:2], ["08:30", "10:30"])

    def test_error_report_does_not_claim_unchanged(self):
        report = air.markdown_report({"status": "error", "error": "PDF ambiguo"})
        self.assertIn("non riuscito", report)
        self.assertIn("PDF ambiguo", report)


class DocumentTests(unittest.TestCase):
    def run_document(self, text, tables, as_of=date(2026, 10, 25)):
        page = SimpleNamespace(extract_text=lambda: text, extract_tables=lambda: tables)
        document = MagicMock()
        document.__enter__.return_value = SimpleNamespace(pages=[page])
        fake_library = SimpleNamespace(open=lambda path: document)
        with patch.dict("sys.modules", {"pdfplumber": fake_library}):
            return air.parse_pdf(Path("example.pdf"), as_of)

    def test_both_directions_are_required(self):
        with self.assertRaises(air.ScheduleError):
            self.run_document("01-BN Orari validi nei giorni feriali. Orari in vigore dal 15 Settembre 2026",
                              [example_table()])

    def test_future_schedule_is_not_applied_early(self):
        with self.assertRaisesRegex(air.ScheduleError, "futuro"):
            self.run_document("01-BN Orari validi nei giorni feriali. Orari in vigore dal 1 Novembre 2026",
                              [example_table(), example_table(True)])

    def test_scanned_pdf_requires_review(self):
        with self.assertRaises(air.ScheduleError):
            self.run_document("", [])

    def test_complete_document_with_two_directions(self):
        tables, starts_on = self.run_document(
            "01-BN Orari validi nei giorni feriali. Orari in vigore dal 15 Settembre 2026",
            [example_table(), example_table(True)],
        )
        self.assertEqual(starts_on, date(2026, 9, 15))
        self.assertEqual({table["direction"] for table in tables}, set(air.DIRECTIONS))


if __name__ == "__main__":
    unittest.main()
