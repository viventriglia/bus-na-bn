"""Discover the current EAV replacement-bus PDF, keeping BUS 1 and BUS 2 distinct."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import Request, urlopen

try:
    from . import generate_data
    from .check_air_timetables import MONTHS, ScheduleError, apply_proposal, effective_date, mark_omitted_stops, minutes, normalized, parse_time
except ImportError:
    import generate_data
    from check_air_timetables import MONTHS, ScheduleError, apply_proposal, effective_date, mark_omitted_stops, minutes, normalized, parse_time


ROOT = Path(__file__).resolve().parents[1]
INDEX_URL = "https://www.eavsrl.it/downloads/orari-linee-ferroviarie-orari-linea-napoli-benevento-orari-ferrovie/"
BN_LABEL = "BENEVENTO CENTRALE RFI"
NA_LABEL = "NAPOLI CENTRALE RFI"
BENEVENTO = "Benevento Stazione - via P. Diacono"
NAPOLI = "Napoli Centrale - via G. Ferraris (di fronte Ramada Hotel)"
DIRECTIONS = {"benevento_napoli": (BENEVENTO, NAPOLI), "napoli_benevento": (NAPOLI, BENEVENTO)}
CATEGORIES = {"1": "BUS 1", "2": "BUS 2 (veloce)"}
CATEGORY_RE = re.compile(r"BUS\s*([12])\s*\**", re.I)
NOTES = {"a": "Non si effettua il 24 e il 31 dicembre.",
         "b": "Non si effettua dal 30 giugno al 31 agosto."}


@dataclass(frozen=True, order=True)
class EavTrip:
    direction: str
    departure: str
    arrival: str
    category: str
    number: str
    notes: str = ""
    flags: str = field(default="", compare=False)
    stops: list[dict] = field(default_factory=list, compare=False)

    def csv_row(self) -> list[str]:
        origin, destination = DIRECTIONS[self.direction]
        return [self.departure, self.arrival, origin, destination, "EAV", self.category, self.number, self.notes]


def official_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in {"eavsrl.it", "www.eavsrl.it"}
            and parsed.port in {None, 443} and parsed.username is None and parsed.password is None)


def download(url: str, limit: int = 5_000_000) -> tuple[bytes, str]:
    if not official_url(url):
        raise ScheduleError("URL esterno al sito HTTPS ufficiale EAV")
    request = Request(url, headers={"User-Agent": "BN-NA-timetable-check/1.0", "Cache-Control": "no-cache"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=40) as response:
                final = response.geturl()
                if not official_url(final):
                    raise ScheduleError("Redirect EAV verso un dominio inatteso")
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise ScheduleError("Documento EAV oltre il limite previsto")
                return data, final
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)
    raise ScheduleError("Download EAV non riuscito")


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.articles = []
        self.links = []
        self.article = None
        self.heading = None

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            if self.article is not None:
                raise ScheduleError("Schede EAV annidate non riconosciute")
            self.article = {"headings": [], "links": []}
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.article is not None:
            self.heading = []
        elif tag == "a":
            item = dict(attrs)
            self.links.append(item)
            if self.article is not None:
                self.article["links"].append(item)

    def handle_data(self, data):
        if self.heading is not None:
            self.heading.append(data)

    def handle_endtag(self, tag):
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.heading is not None:
            self.article["headings"].append(" ".join(self.heading))
            self.heading = None
        elif tag == "article" and self.article is not None:
            self.articles.append(self.article)
            self.article = None


def title_date(title: str) -> date:
    match = re.search(r"\bdal\s+(\d{1,2})\s+(\w+)\s+(\d{4})", title, re.I)
    if match is None or match[2].casefold() not in MONTHS:
        raise ScheduleError("Decorrenza non riconosciuta nel titolo della scheda EAV")
    return date(int(match[3]), MONTHS[match[2].casefold()], int(match[1]))


def discover_detail(html: str, index_url: str, as_of: date) -> tuple[str, date]:
    parser = PageParser()
    parser.feed(html)
    if parser.article is not None:
        raise ScheduleError("Elenco EAV troncato")
    candidates = set()
    for article in parser.articles:
        headings = [title for title in article["headings"] if
                    {"autobus", "sostitutivi", "benevento", "napoli"}.issubset(normalized(title).split())]
        if not headings:
            continue
        if len(headings) != 1:
            raise ScheduleError("Titoli multipli nella scheda EAV")
        starts_on = title_date(headings[0])
        links = {urljoin(index_url, item["href"]) for item in article["links"] if item.get("href") and
                 ("read-more" in (item.get("class") or "").split() or item.get("title") == headings[0])}
        if len(links) != 1:
            raise ScheduleError("Link Leggi di piu' assente o ambiguo")
        url = links.pop()
        if not official_url(url):
            raise ScheduleError("Scheda EAV su dominio inatteso")
        candidates.add((starts_on, url))
    current = [(day, url) for day, url in candidates if day <= as_of]
    if not current:
        raise ScheduleError("Nessun orario autobus EAV gia' in vigore nell'elenco")
    latest = max(day for day, _ in current)
    urls = {url for day, url in current if day == latest}
    if len(urls) != 1:
        raise ScheduleError("Piu' PDF EAV con la stessa decorrenza: verifica manuale")
    return urls.pop(), latest


def discover_download(html: str, detail_url: str) -> str:
    parser = PageParser()
    parser.feed(html)
    urls = {urljoin(detail_url, item["data-downloadurl"]) for item in parser.links
            if item.get("data-downloadurl") and "wpdm-download-link" in (item.get("class") or "").split()}
    if len(urls) != 1:
        raise ScheduleError("Pulsante Download EAV assente o ambiguo")
    url = urls.pop()
    package = parse_qs(urlsplit(url).query).get("wpdmdl", [])
    if not official_url(url) or len(package) != 1 or not package[0].isdigit():
        raise ScheduleError("Collegamento di download EAV non riconosciuto")
    return url


def cells_in_band(row, values: list, band: tuple) -> list[tuple[str, tuple]]:
    result = []
    for text, cell in zip(values, row.cells):
        if cell is not None and min(cell[2], band[2]) - max(cell[0], band[0]) > 0.4:
            result.append((text or "", cell))
    return result


def single_cell(row, values: list, band: tuple) -> str:
    found = cells_in_band(row, values, band)
    if len(found) != 1 or found[0][1][0] > band[0] + 0.5 or found[0][1][2] < band[2] - 0.5:
        raise ScheduleError("Cella EAV non allineata alla colonna del bus")
    return found[0][0].strip()


def extract_table(table, page_number: int) -> dict | None:
    values = table.extract()
    endpoints = {}
    for index, row in enumerate(values):
        for label in (BN_LABEL, NA_LABEL):
            if row and normalized(row[0]) == normalized(label):
                if label in endpoints:
                    raise ScheduleError("Capolinea EAV ripetuto nella tabella")
                endpoints[label] = index
    if len(endpoints) != 2:
        return None
    first, last = sorted(endpoints, key=endpoints.get)
    direction = "benevento_napoli" if first == BN_LABEL else "napoli_benevento"
    origin_index, destination_index = endpoints[first], endpoints[last]
    before = values[:origin_index]
    category_rows = [index for index, row in enumerate(before) if any(CATEGORY_RE.fullmatch(cell or "") for cell in row)]
    number_rows = [index for index, row in enumerate(before) if any(re.fullmatch(r"\d{4}", cell or "") for cell in row)]
    weekday_rows = [index for index, row in enumerate(before) if any((cell or "").strip() == "FER" for cell in row)]
    if len(category_rows) != 1 or len(number_rows) != 1 or len(weekday_rows) != 1:
        raise ScheduleError("Intestazioni numero/categoria/FER non riconosciute")
    category_index, number_index, weekday_index = category_rows[0], number_rows[0], weekday_rows[0]
    if not number_index < weekday_index < category_index < origin_index:
        raise ScheduleError("Ordine delle intestazioni EAV inatteso")
    bands = []
    for column, text in enumerate(values[category_index]):
        if not (text or "").strip() or normalized(text) == "categoria bus":
            continue
        if CATEGORY_RE.fullmatch(text) is None:
            raise ScheduleError(f"Categoria EAV non riconosciuta: {text!r}")
        band = table.rows[category_index].cells[column]
        if band is None:
            raise ScheduleError("Categoria senza cella fisica")
        bands.append(band)
    # Every numbered service must be fully partitioned into recognized bus categories.
    # Otherwise a missing/unreadable BUS 2 label could look like a cancelled service.
    for text, parent in zip(values[number_index], table.rows[number_index].cells):
        if parent is None or not re.fullmatch(r"\d{4}", text or ""):
            continue
        children = sorted(band for band in bands if band[0] >= parent[0] - 0.5 and band[2] <= parent[2] + 0.5)
        edge = parent[0]
        for band in children:
            if abs(band[0] - edge) > 0.5:
                raise ScheduleError("Colonna bus mancante o sovrapposta")
            edge = band[2]
        if abs(edge - parent[2]) > 0.5:
            raise ScheduleError("Una corsa numerata non ha categorie bus complete")
    trips, partial = [], []
    for column, text in enumerate(values[category_index]):
        match = CATEGORY_RE.fullmatch(text or "")
        if match is None:
            continue
        band = table.rows[category_index].cells[column]
        if band is None:
            raise ScheduleError("Categoria senza cella fisica")
        number = single_cell(table.rows[number_index], values[number_index], band)
        if not re.fullmatch(r"\d{4}", number):
            raise ScheduleError("Numero di corsa EAV non valido")
        if single_cell(table.rows[weekday_index], values[weekday_index], band) != "FER":
            raise ScheduleError("Validita' EAV non riconosciuta")
        departure = parse_time(single_cell(table.rows[origin_index], values[origin_index], band))
        arrival = parse_time(single_cell(table.rows[destination_index], values[destination_index], band))
        flags = set()
        for index in range(weekday_index + 1, category_index):
            for flag_text, _ in cells_in_band(table.rows[index], values[index], band):
                for flag in flag_text.split():
                    if flag not in {"a", "b", "G"}:
                        raise ScheduleError(f"Nota di validita' EAV sconosciuta: {flag!r}")
                    flags.add(flag)
        category = CATEGORIES[match[1]]
        if departure is None or arrival is None:
            partial.append({"number": number, "category": category, "departure": departure, "arrival": arrival})
            continue
        if not 30 <= minutes(arrival) - minutes(departure) <= 240:
            raise ScheduleError(f"Durata EAV implausibile: {departure}-{arrival}")
        notes = " ".join(NOTES[flag] for flag in ("a", "b") if flag in flags)
        stops = []
        omitted_times = []
        previous = minutes(departure)
        for index in range(origin_index, destination_index + 1):
            label = (values[index][0] or "").strip()
            when = parse_time(single_cell(table.rows[index], values[index], band))
            if when is None:
                continue
            if not re.match(r"^(benevento|napoli)\b", normalized(label)):
                omitted_times.append(minutes(when))
                continue
            if not previous <= minutes(when) <= minutes(arrival):
                raise ScheduleError(f"Orario incoerente per una fermata urbana EAV: {label} {when}")
            name = {normalized(BN_LABEL): BENEVENTO, normalized(NA_LABEL): NAPOLI,
                    "benevento appia": "Benevento Appia", "benevento r liberta": "Benevento, Rione Libertà",
                    "benevento p corvo": "Benevento, Pontecorvo"}.get(normalized(label), label.title())
            stops.append({"name": name, "time": when, "note": ""})
            previous = minutes(when)
        mark_omitted_stops(stops, omitted_times)
        trips.append(EavTrip(direction, departure, arrival, category, number, notes, " ".join(sorted(flags)), stops))
    if not trips:
        raise ScheduleError(f"Nessuna corsa EAV completa per {direction}")
    return {"direction": direction, "page": page_number, "trips": trips, "partial": partial}


def parse_pdf(path: Path, as_of: date, expected_date: date | None = None) -> tuple[list[EavTrip], list[dict], date]:
    import pdfplumber

    tables, texts = [], []
    with pdfplumber.open(path) as pdf:
        if not 1 <= len(pdf.pages) <= 10:
            raise ScheduleError("Numero di pagine EAV inatteso")
        for page_number, page in enumerate(pdf.pages, start=1):
            texts.append(page.extract_text() or "")
            for table in page.find_tables():
                parsed = extract_table(table, page_number)
                if parsed:
                    tables.append(parsed)
    text = "\n".join(texts)
    starts_on = effective_date(text)
    if starts_on > as_of or (expected_date is not None and starts_on != expected_date):
        raise ScheduleError("La decorrenza del PDF EAV e' futura o diversa dalla scheda")
    if "IL SERVIZIO NON SI EFFETTUA LA DOMENICA E FESTIVI" not in text.upper():
        raise ScheduleError("Documento EAV feriale non riconosciuto")
    trips = []
    for direction in DIRECTIONS:
        matches = [table for table in tables if table["direction"] == direction]
        if len(matches) != 1:
            raise ScheduleError(f"Attesa una tabella EAV per {direction}, trovate {len(matches)}")
        trips.extend(matches[0]["trips"])
    flags = {flag for trip in trips for flag in trip.flags.split()}
    legend = normalized(text)
    if "a" in flags and "a non si effettua il 24 ed il 31 dicembre" not in legend:
        raise ScheduleError("Legenda della nota EAV a cambiata")
    if "b" in flags and "b non si effettua dal 30 giugno al 31 agosto" not in legend:
        raise ScheduleError("Legenda della nota EAV b cambiata")
    if len(trips) != len(set(trips)):
        raise ScheduleError("Colonne EAV duplicate o ambigue")
    return sorted(trips), tables, starts_on


def current_trips(rows: list[list[str]], proposed: list[EavTrip]) -> tuple[list[EavTrip], int]:
    result, enriched = [], 0
    for row in rows:
        if row[4] != "EAV":
            continue
        departure, arrival, origin, destination, _ = row[:5]
        direction = next((key for key, stops in DIRECTIONS.items() if stops == (origin, destination)), None)
        if direction is None:
            raise ScheduleError("Tratta EAV nel CSV diversa dai capolinea previsti")
        extras = (row[5:] + ["", "", ""])[:3]
        category, number, notes = extras
        if not category or not number:
            matches = [trip for trip in proposed if trip.direction == direction and trip.departure == departure
                       and trip.arrival == arrival and (not category or trip.category == category)]
            if len(matches) > 1:
                raise ScheduleError("Migrazione EAV ambigua: piu' categorie per la vecchia riga")
            if matches:
                result.append(matches[0])
                enriched += 1
                continue
        if category and category not in CATEGORIES.values():
            raise ScheduleError("Categoria EAV nel CSV sconosciuta")
        result.append(EavTrip(direction, departure, arrival, category, number, notes))
    if not result:
        raise ScheduleError("Nessuna corsa EAV nel CSV attuale")
    return result, enriched


def prepare_proposal(rows: list[list[str]], trips: list[EavTrip], updates: dict, output: Path) -> None:
    proposed = [trip.csv_row() for trip in trips] + [row for row in rows if row[4] != "EAV"]
    proposed = generate_data.normalized_trips(proposed, generate_data.TRIP_COLUMNS)
    with (output / generate_data.CSV_PATH.name).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter=";", lineterminator="\n")
        writer.writerow(generate_data.TRIP_COLUMNS)
        writer.writerows(proposed)
    details = {generate_data.trip_key(trip.csv_row()): [trip.stops] for trip in trips if trip.stops}
    combined = generate_data.prepare_stop_details("EAV", details, output)
    (output / "data.js").write_text(generate_data.schedule_js(proposed, generate_data.read_mottam_journeys(), updates, combined), encoding="utf-8")
    (output / generate_data.LINE_UPDATES_PATH.name).write_text(json.dumps(updates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def describe(trip: dict) -> str:
    return f"{trip['direction']} {trip['departure']}–{trip['arrival']} · {trip['category']} · corsa {trip['number']}"


def markdown_report(report: dict) -> str:
    if report["status"] == "error":
        return f"# Controllo EAV non riuscito\n\n{report['error']}\n\nNessun dato EAV modificato.\n"
    lines = ["# Controllo EAV Benevento - Napoli", "", f"Esito: **{report['status']}**", "",
             f"Scheda: {report['detail_url']}", f"PDF: {report['pdf_url'] or report['local_pdf']}",
             f"Decorrenza: {report['effective_from']}", f"Corse complete: {report['trip_count']}",
             f"Righe precedenti arricchite con categoria e numero: {report['enriched']}", ""]
    lines.extend([f"Fermate urbane aggiornate: {'sì' if report.get('stops_changed') else 'no'}.", ""])
    for key, title in (("added", "Da aggiungere"), ("removed", "Da rimuovere")):
        lines.extend([f"## {title}", ""])
        lines.extend(f"- {describe(trip)}" for trip in report[key])
        if not report[key]:
            lines.append("Nessuna.")
        lines.append("")
    lines.extend(["BUS 1 e BUS 2 (veloce) restano corse distinte. Non sono escluse categorie EAV.", "",
                  "Le corse senza entrambi i capolinea centrali restano nel report, non vengono importate.",
                  "Le eccezioni a/b del PDF sono riportate come note; nessun filtro di calendario aggiunto.", "",
                  "Dati e conferma aggiornati." if report["applied"] else "Solo report e proposte.", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, help="PDF locale: non avanza la conferma online")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--output-dir", type=Path, default=ROOT / "tmp" / "eav-check")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    published = generate_data.TIMETABLES_PATH.resolve()
    if output == ROOT or output == published or published in output.parents:
        parser.error("Directory di output da tenere separata dai file pubblicati")
    output.mkdir(parents=True, exist_ok=True)
    stops_path = generate_data.stops_path("EAV")
    names = (generate_data.CSV_PATH.name, "data.js", generate_data.LINE_UPDATES_PATH.name, stops_path.name)
    for name in names:
        (output / name).unlink(missing_ok=True)
    report = {"status": "error", "checked_at": datetime.now(timezone.utc).isoformat(), "applied": False}
    try:
        detail_url, pdf_url, expected_date = None, None, None
        if args.pdf:
            content = args.pdf.read_bytes()
        else:
            index, index_url = download(INDEX_URL)
            (output / "index.html").write_bytes(index)
            detail_url, expected_date = discover_detail(index.decode("utf-8-sig"), index_url, args.as_of)
            detail, detail_url = download(detail_url)
            (output / "detail.html").write_bytes(detail)
            download_url = discover_download(detail.decode("utf-8-sig"), detail_url)
            content, pdf_url = download(download_url, 15_000_000)
        if not content.startswith(b"%PDF-"):
            raise ScheduleError("Il Download EAV non ha restituito un PDF")
        pdf_path = output / "source.pdf"
        pdf_path.write_bytes(content)
        trips, tables, starts_on = parse_pdf(pdf_path, args.as_of, expected_date)
        originals = {path: path.read_bytes() for path in (generate_data.CSV_PATH, generate_data.OUTPUT_PATH, generate_data.LINE_UPDATES_PATH)}
        originals[stops_path] = stops_path.read_bytes() if stops_path.exists() else None
        details = {generate_data.trip_key(trip.csv_row()): [trip.stops] for trip in trips if trip.stops}
        details_changed = details != generate_data.read_stop_details(stops_path)
        rows = generate_data.read_trips()
        current, enriched = current_trips(rows, trips)
        added, removed = sorted(set(trips) - set(current)), sorted(set(current) - set(trips))
        changed = bool(added or removed or enriched or details_changed)
        previous_updates = generate_data.read_line_updates()
        updates = {line: dict(entry) for line, entry in previous_updates.items()}
        if changed:
            updates["EAV"]["updated_at"] = report["checked_at"]
        if args.pdf is None:
            updates["EAV"]["checked_at"] = report["checked_at"]
        prepare_proposal(rows, trips, updates, output)
        if not changed:
            (output / generate_data.CSV_PATH.name).write_bytes(originals[generate_data.CSV_PATH])
        report.update({"status": "changes" if changed else "unchanged", "detail_url": detail_url, "pdf_url": pdf_url,
                       "local_pdf": str(args.pdf) if args.pdf else None, "effective_from": starts_on.isoformat(),
                       "pdf_sha256": hashlib.sha256(content).hexdigest(), "trip_count": len(trips), "enriched": enriched,
                       "stops_changed": details_changed,
                       "added": [asdict(trip) for trip in added], "removed": [asdict(trip) for trip in removed],
                       "trips": [asdict(trip) for trip in trips],
                       "partial": [item for table in tables for item in table["partial"]]})
        if args.apply and (changed or updates != previous_updates):
            apply_proposal(output, originals)
            report["applied"] = True
    except Exception as error:
        report.update({"status": "error", "error": f"{type(error).__name__}: {error}"})
        for name in names:
            (output / name).unlink(missing_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = markdown_report(report)
    (output / "report.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
