"""Discover AIR's weekday PDF, compare timetables, optionally apply valid changes.

Tracked files are updated only with --apply. Exit codes: 0 success, 1 error,
2 changes when --fail-on-changes is specified. PDF extraction uses cells, never
zipped lists of times (blank cells would misalign journeys).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

try:
    from . import generate_data
except ImportError:
    import generate_data


ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "scripts" / "air_rules.json"
CURRENT_CSV = ROOT / "assets" / "timetables" / "orari_eav_air.csv"
BENEVENTO = "Benevento, Terminal bus - via Mustilli"
NAPOLI = "Napoli Metropark - Terminal bus"
DIRECTIONS = {
    "benevento_napoli": (BENEVENTO, NAPOLI),
    "napoli_benevento": (NAPOLI, BENEVENTO),
}
ENDPOINT_ALIASES = {
    BENEVENTO: {"benevento terminal bus via mustilli", "benevento via mustilli terminal bus"},
    NAPOLI: {"napoli metropark terminal bus", "napoli c so lucci terminal metropark"},
}
MONTHS = {
    month: index for index, month in enumerate(
        ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
         "agosto", "settembre", "ottobre", "novembre", "dicembre"), start=1
    )
}


class ScheduleError(ValueError):
    """An ambiguous or invalid source must never replace the published data."""


@dataclass(frozen=True, order=True)
class Trip:
    direction: str
    departure: str
    arrival: str

    def csv_row(self) -> list[str]:
        origin, destination = DIRECTIONS[self.direction]
        return [self.departure, self.arrival, origin, destination, "AIR"]


def normalized(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "").casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.href is not None:
            self.links.append((self.href, " ".join(self.parts)))
            self.href = None


def official_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"aircampania.it", "www.aircampania.it"}
        and parsed.port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


def discover_pdf(html: str, page_url: str) -> str:
    parser = LinkParser()
    parser.feed(html)
    candidates = set()
    for href, label in parser.links:
        words = normalized(label).split()
        route = bool(re.search(r"\b(?:01 bn|bn 01)\b", normalized(label)))
        # Combined feriale+festiva documents are filtered by column, below.
        if route and {"benevento", "napoli", "feriale"}.issubset(words):
            url = urljoin(page_url, href)
            if not official_url(url):
                raise ScheduleError("Il PDF individuato non appartiene al sito HTTPS ufficiale AIR")
            candidates.add(url)

    if len(candidates) != 1:
        raise ScheduleError(
            f"Atteso un unico link 01-BN Benevento-Napoli feriale; trovati {len(candidates)}. "
            "Nessun fallback al vecchio PDF: verificare la pagina ufficiale."
        )
    return candidates.pop()


def download(url: str, max_bytes: int) -> tuple[bytes, str]:
    if not official_url(url):
        raise ScheduleError("URL esterno al sito ufficiale AIR")
    request = Request(url, headers={"User-Agent": "BN-NA-timetable-check/1.0"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=40) as response:
                final_url = response.geturl()
                if not official_url(final_url):
                    raise ScheduleError("Redirect del sito AIR verso una fonte inattesa")
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise ScheduleError("Download oltre la dimensione massima prevista")
                return body, final_url
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)
    raise ScheduleError("Download non riuscito")


def parse_time(value: str | None) -> str | None:
    text = (value or "").strip()
    if text in {"", "-", "--", "|"}:
        return None
    match = re.fullmatch(r"\**\s*(\d{1,2})[:.](\d{2})\s*\**", text)
    if not match:
        raise ScheduleError(f"Cella orario non riconosciuta: {text!r}")
    hours, minutes = map(int, match.groups())
    if hours > 23 or minutes > 59:
        raise ScheduleError(f"Orario fuori intervallo: {text!r}")
    return f"{hours:02}:{minutes:02}"


def minutes(value: str) -> int:
    hours, mins = map(int, value.split(":"))
    return 60 * hours + mins


def extract_table(table: list[list[str | None]], page_number: int) -> dict | None:
    endpoints = {}
    for row_index, row in enumerate(table):
        for column, cell in enumerate(row):
            for stop in (BENEVENTO, NAPOLI):
                if normalized(cell) in ENDPOINT_ALIASES[stop]:
                    if stop in endpoints:
                        raise ScheduleError("Capolinea ripetuto nella stessa tabella")
                    endpoints[stop] = (row_index, column, row)
    if len(endpoints) != 2:
        return None

    origin, destination = sorted(endpoints, key=lambda stop: endpoints[stop][0])
    direction = next(key for key, stops in DIRECTIONS.items() if stops == (origin, destination))
    origin_index, label_column, origin_row = endpoints[origin]
    destination_index, other_column, destination_row = endpoints[destination]
    if label_column != other_column or len(origin_row) != len(destination_row):
        raise ScheduleError("Colonne dei capolinea non allineate")

    headers = [row for row in table[:origin_index] if any(
        normalized(cell).startswith("validit") or normalized(cell) == "stagionalita corsa" for cell in row
    )]
    if len(headers) != 1 or len(headers[0]) != len(origin_row):
        raise ScheduleError("Intestazione di validità assente o non allineata")
    header = headers[0]
    modern = normalized(header[label_column]) == "stagionalita corsa"
    days_headers = [row for row in table[:origin_index]
                    if normalized(row[label_column]) == "giorni di effettuazione"]
    if modern and (len(days_headers) != 1 or len(days_headers[0]) != len(header)):
        raise ScheduleError("Giorni di effettuazione assenti o non allineati")
    trips = []
    incomplete = []
    warnings = []
    holiday_columns = []
    for column in range(label_column + 1, len(origin_row)):
        validity = (header[column] or "").strip()
        if modern:
            days = normalized(days_headers[0][column])
            code = re.sub(r"_+", "_", validity.upper()).strip("_")
            holiday = days == "dom e festivi" and code == "A6_DOM"
            weekday = days in {"lmmgv", "lmmgvs"} and bool(re.fullmatch(r"[A-Z0-9]+_?FE[56]", code))
            if not holiday and not weekday:
                raise ScheduleError(f"Validità o giorni non riconosciuti/incoerenti: {validity!r}, {days!r}")
            if holiday:
                holiday_columns.append({"column": column + 1, "validity": validity,
                                        "days": days_headers[0][column]})
                continue
        departure = parse_time(origin_row[column])
        arrival = parse_time(destination_row[column])
        if not departure or not arrival:
            if departure or arrival:
                incomplete.append({"column": column + 1, "departure": departure, "arrival": arrival})
            continue
        if not validity:
            raise ScheduleError("Corsa completa senza codice di validità")
        duration = minutes(arrival) - minutes(departure)
        if not 30 <= duration <= 240:
            raise ScheduleError(f"Durata implausibile: {departure}-{arrival}")

        # Validate every available intermediate time against its own column.
        previous = minutes(departure)
        for row in table[origin_index + 1:destination_index]:
            if len(row) != len(origin_row):
                raise ScheduleError("Riga intermedia con numero di colonne inatteso")
            stop_time = parse_time(row[column])
            if stop_time is not None:
                current = minutes(stop_time)
                if current < previous or current > minutes(arrival):
                    warnings.append(
                        f"Pagina {page_number}, colonna {column + 1}: orario intermedio "
                        f"{stop_time} incoerente in {row[label_column]}. "
                        "Il confronto usa esclusivamente gli orari dei due capolinea."
                    )
                previous = current

        trips.append({
            **asdict(Trip(direction, departure, arrival)),
            "validity": validity,
            "page": page_number,
            "column": column + 1,
        })
    if not trips:
        raise ScheduleError(f"Nessuna corsa completa per {direction}")
    return {"direction": direction, "trips": trips, "incomplete_columns": incomplete,
            "holiday_columns": holiday_columns, "warnings": warnings}


def effective_date(text: str) -> date:
    dates = set()
    for match in re.finditer(r"orari(?:o)?\s+in\s+vigore\s+dal\s+(\d{1,2})\s+(\w+)\s+(\d{4})", text, re.I):
        if match[2].casefold() in MONTHS:
            dates.add(date(int(match[3]), MONTHS[match[2].casefold()], int(match[1])))
    for match in re.finditer(r"orari(?:o)?\s+in\s+vigore\s+dal\s+(\d{1,2})/(\d{1,2})/(\d{4})", text, re.I):
        dates.add(date(int(match[3]), int(match[2]), int(match[1])))
    if len(dates) != 1:
        raise ScheduleError("Data di entrata in vigore del PDF non riconosciuta")
    return dates.pop()


def modern_tables(page) -> list[list[list[str | None]]] | None:
    """Extend physical header columns through rows whose grid rules are missing.

    The default extractor merges the last stop and legend into one cell.
    Keep header x coordinates and use text rows, preserving empty time cells.
    """
    headers = []
    for table in page.find_tables():
        for index, row in enumerate(table.extract()):
            if row and normalized(row[0]) == "stagionalita corsa":
                headers.append((row, table.rows[index].cells))
    if not headers:
        return None
    if len(headers) != 1:
        raise ScheduleError("Più griglie di stagionalità nella stessa pagina AIR")
    labels, cells = headers[0]
    while labels and not (labels[-1] or "").strip():
        # pdfplumber can expose a blank margin beyond the last service.
        # Reject any text there: an unnamed service must never disappear.
        margin = cells[-1]
        if margin is None or any(
            char["text"].strip() and char["top"] >= margin[1]
            and margin[0] <= (char["x0"] + char["x1"]) / 2 <= margin[2]
            for char in page.chars
        ):
            raise ScheduleError("Colonna AIR senza codice di stagionalità")
        labels = labels[:-1]
        cells = cells[:-1]
    if len(labels) < 3 or any(not label or cell is None for label, cell in zip(labels, cells)):
        raise ScheduleError("Griglia delle colonne AIR incompleta")
    if any(abs(left[2] - right[0]) > 0.5 for left, right in zip(cells, cells[1:])):
        raise ScheduleError("Colonne AIR non contigue")
    boundaries = [cell[0] for cell in cells] + [cells[-1][2]]
    return page.extract_tables({
        "vertical_strategy": "explicit", "explicit_vertical_lines": boundaries,
        "horizontal_strategy": "text", "text_x_tolerance": 1,
    })


def parse_pdf(path: Path, as_of: date) -> tuple[list[dict], date]:
    import pdfplumber

    tables = []
    texts = []
    with pdfplumber.open(path) as pdf:
        if not 1 <= len(pdf.pages) <= 10:
            raise ScheduleError("Numero di pagine inatteso")
        for page_number, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            texts.append(page_text)
            modern = modern_tables(page)
            for table in modern if modern is not None else page.extract_tables():
                parsed = extract_table(table, page_number)
                if parsed:
                    if modern is None and ("giorni feriali" not in page_text.casefold()
                                           or "giorni festivi" in page_text.casefold()):
                        raise ScheduleError("Tabella senza identificazione univoca dei giorni feriali")
                    tables.append(parsed)

    text = "\n".join(texts)
    if not re.search(r"\b01\s*-\s*BN\b", text, re.I):
        raise ScheduleError("PDF non riconosciuto come linea 01-BN feriale (o scansione senza testo)")
    starts_on = effective_date(text)
    if starts_on > as_of:
        raise ScheduleError(f"Orario futuro: entra in vigore il {starts_on}, non ancora applicabile")
    for direction in DIRECTIONS:
        found = [table for table in tables if table["direction"] == direction]
        if len(found) != 1:
            raise ScheduleError(f"Attesa una tabella completa per {direction}, trovate {len(found)}")
    return tables, starts_on


def apply_rules(tables: list[dict], rules: dict) -> tuple[list[Trip], list[dict]]:
    ignored = {
        Trip(item["direction"], item["departure"], item["arrival"]): item["reason"]
        for item in rules["ignored_trips"]
    }
    result = set()
    exclusions = []
    for table in tables:
        for item in table["trips"]:
            trip = Trip(item["direction"], item["departure"], item["arrival"])
            if trip in ignored:
                exclusions.append({**asdict(trip), "reason": ignored[trip]})
            else:
                result.add(trip)
    for direction in DIRECTIONS:
        if not any(trip.direction == direction for trip in result):
            raise ScheduleError(f"Nessuna corsa dopo le esclusioni per {direction}")
    return sorted(result), exclusions


def read_current(path: Path) -> tuple[list[list[str]], list[Trip]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream, delimiter=";")
        if reader.fieldnames not in (generate_data.COLUMNS, generate_data.TRIP_COLUMNS):
            raise ScheduleError("Schema del CSV attuale inatteso")
        rows = [[(row[column] or "").strip() for column in reader.fieldnames] for row in reader]
    current = []
    for row in rows:
        departure, arrival, origin, destination, operator = row[:5]
        if operator == "AIR":
            direction = next((key for key, stops in DIRECTIONS.items() if stops == (origin, destination)), None)
            if direction is None:
                raise ScheduleError("Tratta AIR nel CSV non prevista dal parser 01-BN")
            current.append(Trip(direction, departure, arrival))
    if len(current) != len(set(current)):
        raise ScheduleError("Corse AIR duplicate nel CSV attuale")
    return rows, current


def prepare_proposal(rows: list[list[str]], trips: list[Trip], output: Path, line_updates: dict | None = None) -> None:
    # Keep every non-AIR row byte-for-byte in content and relative order.
    proposed_rows = [row for row in rows if row[4] != "AIR"] + [trip.csv_row() for trip in trips]
    columns = generate_data.columns_for_trips(proposed_rows)
    proposed_rows = generate_data.normalized_trips(proposed_rows, columns)
    proposed_csv = output / "orari_eav_air.csv"
    with proposed_csv.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter=";", lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(proposed_rows)
    if line_updates is None:
        line_updates = generate_data.read_line_updates()
    (output / "data.js").write_text(
        generate_data.schedule_js(proposed_rows, generate_data.read_mottam_journeys(), line_updates),
        encoding="utf-8",
    )
    (output / generate_data.LINE_UPDATES_PATH.name).write_text(
        json.dumps(line_updates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def update_air_metadata(previous: dict, timestamp: str, changed: bool, live_check: bool) -> dict:
    result = {line: dict(entry) for line, entry in previous.items()}
    if changed:
        result["AIR"]["updated_at"] = timestamp
    if live_check:
        result["AIR"]["checked_at"] = timestamp
    return result


def replace_bytes(path: Path, content: bytes) -> None:
    """Replace one complete file atomically, on Windows as well as Linux."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply_proposal(output: Path, originals: dict[Path, bytes]) -> None:
    """Apply validated files; roll back earlier replacements on failure."""
    for target, original in originals.items():
        if target.read_bytes() != original:
            raise ScheduleError(f"File modificato durante il controllo: {target.name}")
    written = []
    try:
        for target in originals:
            replace_bytes(target, (output / target.name).read_bytes())
            written.append(target)
    except Exception:
        for target in reversed(written):
            replace_bytes(target, originals[target])
        raise


def markdown_report(report: dict) -> str:
    if report["status"] == "error":
        return "# Controllo AIR non riuscito\n\n" + report["error"] + "\n\nGli orari pubblicati non sono stati modificati.\n"
    lines = [
        "# Controllo AIR 01-BN", "",
        f"Esito: **{report['status']}**", "",
        f"Pagina indice: {report['index_url']}",
        f"PDF individuato: {report['pdf_url'] or report['local_pdf']}",
        f"In vigore dal: {report['effective_from']}",
        f"SHA-256 PDF: `{report['pdf_sha256']}`", "",
        "Confronto tra capolinea, per entrambi i sensi. Validità trattata come lun-ven secondo la scelta del sito.",
        "I codici originali restano nel JSON; questo controllo non calcola calendari scolastici o stagionali.", "",
    ]
    for key, title in (("added", "Da aggiungere"), ("removed", "Da rimuovere")):
        lines.extend([f"## {title}", ""])
        lines.extend(
            f"- {item['direction']}: {item['departure']} → {item['arrival']}" for item in report[key]
        )
        if not report[key]:
            lines.append("Nessuna.")
        lines.append("")
    if report["exclusions"]:
        lines.extend(["## Esclusioni concordate", ""])
        lines.extend(
            f"- {item['direction']} {item['departure']}–{item['arrival']}: {item['reason']}"
            for item in report["exclusions"]
        )
    incomplete = sum(len(table["incomplete_columns"]) for table in report["source_tables"])
    holidays = sum(len(table.get("holiday_columns", [])) for table in report["source_tables"])
    warnings = [warning for table in report["source_tables"] for warning in table["warnings"]]
    if warnings:
        lines.extend(["", "## Incongruenze della fonte nelle fermate intermedie", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.extend([
        "", f"Colonne domenicali/festive escluse: {holidays} (dettaglio nel JSON).",
        f"Colonne feriali senza entrambi i capolinea: {incomplete} (dettaglio nel JSON).", "",
        "## Aggiornamento", "",
        ("Dati e date aggiornati: il workflow puo' committare e pubblicare i file."
         if report.get("applied") else "File proposti salvati negli artefatti. Nessun dato pubblicato modificato."), "",
        "Questo report riguarda AIR. MOT.TAM ed EAV hanno controlli separati nello stesso workflow.", "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, help="PDF locale per test riproducibili; salta il download")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "tmp" / "air-check")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--fail-on-changes", action="store_true", help="Exit 2 quando serve una revisione")
    parser.add_argument("--apply", action="store_true", help="Applica CSV e data.js se il confronto valido rileva differenze")
    args = parser.parse_args(argv)
    if args.apply and args.fail_on_changes:
        parser.error("--apply e --fail-on-changes sono alternativi")
    output = args.output_dir.resolve()
    # Proposals must never overwrite the actual publication directory.
    published = (ROOT / "assets" / "timetables").resolve()
    if output == ROOT or output == published or published in output.parents:
        parser.error("La directory di output deve essere separata dai dati pubblicati")
    output.mkdir(parents=True, exist_ok=True)
    # Never leave last run's proposals next to this run's error report.
    for filename in ("orari_eav_air.csv", "data.js", generate_data.LINE_UPDATES_PATH.name):
        (output / filename).unlink(missing_ok=True)
    if args.pdf is None or args.pdf.resolve() != output / "source.pdf":
        (output / "source.pdf").unlink(missing_ok=True)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "status": "error", "applied": False}
    try:
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        pdf_url = None
        index_url = rules["index_url"]
        if args.pdf:
            pdf_bytes = args.pdf.read_bytes()
        else:
            html, index_url = download(index_url, 5_000_000)
            pdf_url = discover_pdf(html.decode("utf-8-sig"), index_url)
            pdf_bytes, pdf_url = download(pdf_url, 20_000_000)
        if not pdf_bytes.startswith(b"%PDF-"):
            raise ScheduleError("La risposta non è un PDF")
        pdf_path = output / "source.pdf"
        pdf_path.write_bytes(pdf_bytes)
        tables, starts_on = parse_pdf(pdf_path, args.as_of)
        proposed, exclusions = apply_rules(tables, rules)
        originals = {
            CURRENT_CSV: CURRENT_CSV.read_bytes(),
            generate_data.OUTPUT_PATH: generate_data.OUTPUT_PATH.read_bytes(),
            generate_data.LINE_UPDATES_PATH: generate_data.LINE_UPDATES_PATH.read_bytes(),
        }
        rows, current = read_current(CURRENT_CSV)
        added = sorted(set(proposed) - set(current))
        removed = sorted(set(current) - set(proposed))
        previous_metadata = generate_data.read_line_updates()
        line_updates = update_air_metadata(
            previous_metadata, report["checked_at"], bool(added or removed), args.pdf is None
        )
        report.update({
            "status": "changes" if added or removed else "unchanged",
            "index_url": index_url,
            "pdf_url": pdf_url,
            "local_pdf": str(args.pdf) if args.pdf else None,
            "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "effective_from": starts_on.isoformat(),
            "added": [asdict(trip) for trip in added],
            "removed": [asdict(trip) for trip in removed],
            "exclusions": exclusions,
            "source_tables": tables,
            "proposed_count": len(proposed),
            "metadata_updated": line_updates != previous_metadata,
        })
        prepare_proposal(rows, proposed, output, line_updates)
        if args.apply and (added or removed or line_updates != previous_metadata):
            apply_proposal(output, originals)
            report["applied"] = True
    except Exception as error:
        report["status"] = "error"
        report["error"] = f"{type(error).__name__}: {error}"
        for filename in ("orari_eav_air.csv", "data.js", generate_data.LINE_UPDATES_PATH.name):
            (output / filename).unlink(missing_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = markdown_report(report)
    (output / "report.md").write_text(summary, encoding="utf-8")
    print(summary)
    if report["status"] == "error":
        return 1
    return 2 if report["status"] == "changes" and args.fail_on_changes else 0


if __name__ == "__main__":
    sys.exit(main())
