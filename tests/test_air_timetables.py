import json
import io
import copy
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
    def test_combined_weekday_holiday_pdf_is_discovered(self):
        html = ('<a href="01-BN.pdf">01-BN | Benevento - Napoli (feriale+festiva)</a>'
                '<a href="holiday.pdf">01-BN | Benevento - Napoli festiva</a>')
        self.assertEqual(air.discover_pdf(html, 'https://aircampania.it/'),
                         'https://aircampania.it/01-BN.pdf')

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
            for name in ("orari_eav_air.csv", "data.js", "line_updates.json"):
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
            for name in ("orari_eav_air.csv", "data.js", "line_updates.json"):
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


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.previous = {
            "AIR": {"updated_at": "2026-10-25T19:50:55+00:00", "checked_at": "2026-10-25T19:50:55+00:00"},
            "EAV": {"updated_at": "2026-09-05T16:47:58+02:00", "checked_at": None},
            "MOT.TAM": {"updated_at": "2026-09-05T16:47:58+02:00", "checked_at": None},
        }
        self.timestamp = "2026-10-31T00:23:00+00:00"

    def test_unchanged_live_check_updates_confirmation_only(self):
        result = air.update_air_metadata(self.previous, self.timestamp, changed=False, live_check=True)
        self.assertEqual(result["AIR"]["checked_at"], self.timestamp)
        self.assertEqual(result["AIR"]["updated_at"], self.previous["AIR"]["updated_at"])
        self.assertEqual(result["EAV"], self.previous["EAV"])
        self.assertEqual(result["MOT.TAM"], self.previous["MOT.TAM"])
        self.assertNotEqual(self.previous["AIR"]["checked_at"], self.timestamp)

    def test_actual_change_updates_both_dates(self):
        result = air.update_air_metadata(self.previous, self.timestamp, changed=True, live_check=True)
        self.assertEqual(result["AIR"], {"updated_at": self.timestamp, "checked_at": self.timestamp})

    def test_local_pdf_does_not_claim_online_confirmation(self):
        result = air.update_air_metadata(self.previous, self.timestamp, changed=False, live_check=False)
        self.assertEqual(result, self.previous)

    def test_metadata_is_embedded_in_generated_javascript(self):
        text = air.generate_data.schedule_js([], [], self.previous)
        payload = json.loads(text.split(" = ", 1)[1].removesuffix(";\n"))
        self.assertEqual(payload["lineUpdates"], self.previous)

    def test_live_unchanged_run_applies_confirmation_and_preserves_other_lines(self):
        tables = [air.extract_table(example_table(), 1), air.extract_table(example_table(True), 1)]
        trips, _ = air.apply_rules(tables, {"ignored_trips": []})
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            current = folder / "orari_eav_air.csv"
            javascript = folder / "data.js"
            metadata = folder / "line_updates.json"
            with patch.object(air.generate_data, "read_mottam_journeys", return_value=[]):
                air.prepare_proposal([], trips, folder, self.previous)
            original_csv = current.read_bytes()
            with patch.object(air, "CURRENT_CSV", current), \
                 patch.object(air.generate_data, "OUTPUT_PATH", javascript), \
                 patch.object(air.generate_data, "LINE_UPDATES_PATH", metadata), \
                 patch.object(air.generate_data, "read_mottam_journeys", return_value=[]), \
                 patch.object(air, "download", side_effect=[(b"html", "https://aircampania.it/"), (b"%PDF-test", "https://aircampania.it/current.pdf")]), \
                 patch.object(air, "discover_pdf", return_value="https://aircampania.it/current.pdf"), \
                 patch.object(air, "parse_pdf", return_value=(tables, date(2026, 9, 15))), \
                 patch("sys.stdout", new=io.StringIO()):
                result = air.main(["--output-dir", str(folder / "report"), "--as-of", "2026-10-25", "--apply"])
            self.assertEqual(result, 0)
            report = json.loads((folder / "report" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "unchanged")
            self.assertTrue(report["applied"])
            self.assertTrue(report["metadata_updated"])
            final_metadata = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual(final_metadata["AIR"]["checked_at"], report["checked_at"])
            self.assertEqual(final_metadata["AIR"]["updated_at"], self.previous["AIR"]["updated_at"])
            self.assertEqual(final_metadata["EAV"], self.previous["EAV"])
            self.assertEqual(final_metadata["MOT.TAM"], self.previous["MOT.TAM"])
            self.assertEqual(current.read_bytes(), original_csv)


class DocumentTests(unittest.TestCase):
    def run_document(self, text, tables, as_of=date(2026, 10, 25)):
        page = SimpleNamespace(extract_text=lambda: text, extract_tables=lambda: tables, find_tables=lambda: [])
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

    def test_conflicting_effective_dates_require_review(self):
        with self.assertRaises(air.ScheduleError):
            air.effective_date('ORARIO IN VIGORE dal 19/10/2026\nOrari in vigore dal 15 Settembre 2026')

    def test_legacy_table_without_weekday_context_requires_review(self):
        with self.assertRaises(air.ScheduleError):
            self.run_document('01-BN Orari in vigore dal 15 Settembre 2026',
                              [example_table(), example_table(True)])

    def test_complete_document_with_two_directions(self):
        tables, starts_on = self.run_document(
            "01-BN Orari validi nei giorni feriali. Orari in vigore dal 15 Settembre 2026",
            [example_table(), example_table(True)],
        )
        self.assertEqual(starts_on, date(2026, 9, 15))
        self.assertEqual({table["direction"] for table in tables}, set(air.DIRECTIONS))


class MixedPdfTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parent / 'fixtures' / 'air-mixed.json').read_text(encoding='utf-8'))

    def run_fixture(self, payload=None, as_of=date(2026, 10, 26)):
        pages = []
        for item in (payload or self.fixture)['pages']:
            table = SimpleNamespace(extract=lambda item=item: [item['header']],
                                    rows=[SimpleNamespace(cells=item['cells'])])
            def extract(settings, item=item):
                self.assertEqual(settings['vertical_strategy'], 'explicit')
                self.assertEqual(settings['horizontal_strategy'], 'text')
                self.assertEqual(settings['explicit_vertical_lines'],
                                 [cell[0] for cell in item['cells'][:-1]] + [item['cells'][-2][2]])
                return [item['table']]
            pages.append(SimpleNamespace(extract_text=lambda item=item: item['text'],
                                         find_tables=lambda table=table: [table], extract_tables=extract,
                                         chars=item.get('chars', [])))
        document = MagicMock()
        document.__enter__.return_value = SimpleNamespace(pages=pages)
        with patch.dict('sys.modules', {'pdfplumber': SimpleNamespace(open=lambda path: document)}):
            return air.parse_pdf(Path('mixed.pdf'), as_of)

    def test_real_mixed_document_preserves_columns_and_excludes_only_holiday_services(self):
        tables, starts_on = self.run_fixture()
        self.assertEqual(starts_on, date(2026, 10, 19))
        self.assertEqual([len(t['holiday_columns']) for t in tables], [3, 5])
        self.assertEqual([len(t['trips']) for t in tables], [15, 13])
        self.assertEqual([(i['departure'], i['arrival']) for i in tables[0]['trips']], [
            ('05:00', '06:30'), ('05:30', '07:15'), ('05:50', '07:35'), ('06:00', '08:00'),
            ('06:30', '08:30'), ('07:25', '09:25'), ('08:30', '10:30'), ('09:30', '11:30'),
            ('10:30', '12:30'), ('11:30', '13:30'), ('13:35', '15:35'), ('15:15', '17:30'),
            ('17:00', '19:00'), ('18:30', '20:30'), ('19:40', '21:35'),
        ])
        self.assertEqual([(i['departure'], i['arrival']) for i in tables[1]['trips']], [
            ('06:30', '08:30'), ('07:35', '09:35'), ('08:45', '10:45'), ('09:30', '11:30'),
            ('10:30', '12:30'), ('11:30', '13:30'), ('13:15', '15:15'), ('14:15', '16:15'),
            ('15:30', '17:30'), ('16:30', '18:30'), ('17:45', '19:45'), ('19:15', '21:15'),
            ('21:35', '23:05'),
        ])

    def test_unknown_or_contradictory_day_and_code_require_review(self):
        for days, code in [('sabato', 'A6__FE6'), ('Dom e festivi', 'A6__FE6'),
                           ('LMmGVS', 'A6__DOM'), ('LMmGVS', 'NEW_CODE'), ('', 'A6__FE6')]:
            payload = copy.deepcopy(self.fixture)
            payload['pages'][0]['table'][0][1] = code
            day_row = next(r for r in payload['pages'][0]['table']
                           if air.normalized(r[0]) == 'giorni di effettuazione')
            day_row[1] = days
            with self.subTest(days=days, code=code), self.assertRaises(air.ScheduleError):
                self.run_fixture(payload)

    def test_missing_days_header_requires_review(self):
        payload = copy.deepcopy(self.fixture)
        payload['pages'][0]['table'] = [r for r in payload['pages'][0]['table']
                                       if air.normalized(r[0]) != 'giorni di effettuazione']
        with self.assertRaises(air.ScheduleError):
            self.run_fixture(payload)

    def test_unnamed_column_with_content_cannot_disappear(self):
        payload = copy.deepcopy(self.fixture)
        cell = payload['pages'][0]['cells'][-1]
        payload['pages'][0]['chars'] = [{'text': '9', 'top': cell[3] + 10,
                                       'x0': cell[0] + 1, 'x1': cell[0] + 2}]
        with self.assertRaises(air.ScheduleError):
            self.run_fixture(payload)

    def test_future_mixed_schedule_is_not_applied_early(self):
        with self.assertRaisesRegex(air.ScheduleError, 'futuro'):
            self.run_fixture(as_of=date(2026, 10, 18))


if __name__ == "__main__":
    unittest.main()
