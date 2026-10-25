"""Extract MOT.TAM's weekday journeys from the official Naples HTML table.

Default: report and proposals only. --apply also updates MOT.TAM's CSV,
metadata and data.js, after a complete and unambiguous extraction.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.request import Request, urlopen

try:
    from . import generate_data
    from .check_air_timetables import LinkParser, ScheduleError, apply_proposal, minutes, normalized, parse_time
except ImportError:
    import generate_data
    from check_air_timetables import LinkParser, ScheduleError, apply_proposal, minutes, normalized, parse_time


ROOT = Path(__file__).resolve().parents[1]
HOME_URL = "https://www.mottam.com/"
MUSTILLI = "Benevento, Terminal bus - via Mustilli"
BAR = "Benevento, Bar 2000"
GARIBALDI = "Napoli, Piazza Garibaldi"
DEPRETIS = "Napoli, via De Pretis (Piazza Municipio)"
MERIDIONALE = "Napoli, Corso Meridionale"
TIME = r"(\d{1,2}[:.]\d{2})"
PIAZZA = r"(?:p\.?\s*zza|piazza)"
OUTBOUND = re.compile(
    rf"ore\s+{TIME}\s+partenza\s+da\s+benevento\s+via\s+mustilli\s*,?\s*"
    rf"ore\s+{TIME}\s*[\"“”']?\s*bar\s+2000\s*[\"“”']?\s*"
    rf"arrivo\s+a\s+napoli\s+{PIAZZA}\s+garibaldi\s+(?:h\.?|ore)\s*{TIME}\s*"
    rf"(?:circa\s*)?,?\s*prosegue\s+per\s+via\s+de\s*pretis\s*"
    rf"\(\s*{PIAZZA}\s+municipio\s*\)\s*\.?", re.I,
)
INBOUND = re.compile(
    rf"ore\s+{TIME}\s+partenza\s+da\s+via\s+de\s*pretis\s*"
    rf"\(\s*ultima\s+fermata\s+autobus\s+urbani\s*\)\s*"
    rf"ore\s+{TIME}\s+corso\s+meridionale\s+arrivo\s+benevento\s+bar\s+2000\s*"
    rf"ore\s+{TIME}\s*[-–—]\s*via\s+mustilli\s+ore\s+{TIME}\s*\.?", re.I,
)


def official_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in {"mottam.com", "www.mottam.com"}
            and parsed.port in {None, 443} and parsed.username is None and parsed.password is None)


def download(url: str) -> tuple[bytes, str]:
    if not official_url(url):
        raise ScheduleError("URL esterno al sito HTTPS ufficiale MOT.TAM")
    request = Request(url, headers={"User-Agent": "BN-NA-timetable-check/1.0"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=40) as response:
                final_url = response.geturl()
                if not official_url(final_url):
                    raise ScheduleError("Redirect MOT.TAM verso una fonte inattesa")
                content = response.read(5_000_001)
                if len(content) > 5_000_000:
                    raise ScheduleError("Pagina MOT.TAM troppo grande")
                return content, final_url
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)
    raise ScheduleError("Download MOT.TAM non riuscito")


def decode_html(content: bytes) -> str:
    declaration = re.search(rb"charset\s*=\s*['\"]?\s*([a-zA-Z0-9_-]+)", content[:8192], re.I)
    encoding = declaration[1].decode("ascii") if declaration else "utf-8-sig"
    # Decode strictly: a damaged accented header must not become a confirmed source.
    return content.decode(encoding)


def discover_schedule(html: str, home_url: str) -> str:
    parser = LinkParser()
    parser.feed(html)
    candidates = set()
    for href, label in parser.links:
        if normalized(label) in {"orari napoli", "orari benevento napoli"}:
            url = urldefrag(urljoin(home_url, href))[0]
            if not official_url(url):
                raise ScheduleError("Il link ORARI NAPOLI non appartiene a MOT.TAM")
            candidates.add(url)
    if len(candidates) > 1:
        raise ScheduleError("Piu' pagine distinte per ORARI NAPOLI: verifica manuale necessaria")
    # If navigation disappears, accept only a complete table on the home page.
    return candidates.pop() if candidates else urldefrag(home_url)[0]


class TableParser(HTMLParser):
    """Keep each table's rows/cells separate, including Word-exported markup."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.stack: list[dict] = []
        self.ignored: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored.append(tag)
            return
        if self.ignored:
            return
        if tag == "table":
            self.stack.append({"rows": [], "row": None, "cell": None})
        elif self.stack:
            table = self.stack[-1]
            if tag == "tr":
                if table["row"] is not None:
                    raise ScheduleError("Righe HTML sovrapposte")
                table["row"] = []
            elif tag in {"td", "th"}:
                if table["row"] is None or table["cell"] is not None:
                    raise ScheduleError("Celle HTML non allineate")
                table["cell"] = []
            elif tag in {"br", "p", "div", "h1", "h2"} and table["cell"] is not None:
                table["cell"].append(" ")

    def handle_data(self, data):
        if self.stack and not self.ignored and self.stack[-1]["cell"] is not None:
            self.stack[-1]["cell"].append(data)

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if not self.stack:
            return
        table = self.stack[-1]
        if tag in {"td", "th"} and table["cell"] is not None:
            cell = re.sub(r"\s+", " ", "".join(table["cell"])).strip()
            table["row"].append(cell)
            table["cell"] = None
        elif tag == "tr" and table["row"] is not None:
            if table["cell"] is not None:
                raise ScheduleError("Cella HTML non chiusa")
            table["rows"].append(table["row"])
            table["row"] = None
        elif tag == "table":
            if table["row"] is not None:
                raise ScheduleError("Tabella HTML incompleta")
            self.tables.append(self.stack.pop()["rows"])


def parse_journey(direction: str, number: int, text: str) -> dict:
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()
    pattern = OUTBOUND if direction == "andata" else INBOUND
    match = pattern.fullmatch(text)
    if match is None:
        raise ScheduleError(f"{direction} corsa {number}: testo o fermate non riconosciuti")
    times = [parse_time(value) for value in match.groups()]
    numeric = [minutes(value) for value in times]
    if any(later < earlier for earlier, later in zip(numeric, numeric[1:])):
        raise ScheduleError(f"{direction} corsa {number}: orari non crescenti")
    if not 30 <= numeric[-1] - numeric[0] <= 240:
        raise ScheduleError(f"{direction} corsa {number}: durata implausibile")
    if direction == "andata":
        prefix = "A"
        stops = [(MUSTILLI, times[0]), (BAR, times[1]), (GARIBALDI, times[2]), (DEPRETIS, "")]
    else:
        prefix = "R"
        stops = [(DEPRETIS, times[0]), (MERIDIONALE, times[1]), (BAR, times[2]), (MUSTILLI, times[3])]
    return {"id": f"MOTTAM-{prefix}{number}", "direction": direction, "stops": stops}


def parse_schedule(html: str) -> list[dict]:
    parser = TableParser()
    parser.feed(html)
    if parser.stack:
        raise ScheduleError("Pagina HTML troncata")
    candidates = [table for table in parser.tables if all(
        any(len(row) == 1 and normalized(row[0]) == label for row in table)
        for label in ("andata", "ritorno")
    )]
    if len(candidates) != 1:
        raise ScheduleError(f"Attesa una tabella ANDATA/RITORNO, trovate {len(candidates)}")
    result = []
    direction = None
    headers = None
    completed = set()
    for row in candidates[0]:
        if not any(row):
            continue
        label = normalized(row[0]) if len(row) == 1 else ""
        if label in {"andata", "ritorno"}:
            if headers is not None or label in completed:
                raise ScheduleError("Sezioni MOT.TAM incomplete o ripetute")
            direction = label
            headers = []
        elif direction and headers == []:
            for cell in row:
                match = re.fullmatch(r"corsa (\d+) dal lunedi al (?:venerdi|sabato)", normalized(cell))
                if not match or not 1 <= int(match[1]) <= 99:
                    raise ScheduleError(f"Intestazione della corsa non riconosciuta: {cell}")
                headers.append(int(match[1]))
            if len(headers) != len(set(headers)):
                raise ScheduleError("Numeri di corsa duplicati")
        elif direction and headers:
            if len(row) != len(headers):
                raise ScheduleError("Numero di descrizioni diverso dalle intestazioni")
            result.extend(parse_journey(direction, number, cell) for number, cell in zip(headers, row))
            completed.add(direction)
            direction, headers = None, None
        else:
            raise ScheduleError("Riga inattesa nella tabella MOT.TAM")
    if completed != {"andata", "ritorno"} or direction is not None:
        raise ScheduleError("Manca una direzione completa nella tabella MOT.TAM")
    signatures = [(journey["direction"], tuple(journey["stops"])) for journey in result]
    if len(signatures) != len(set(signatures)):
        raise ScheduleError("Corse MOT.TAM duplicate")
    return result


def csv_rows(journeys: list[dict]) -> list[list[str]]:
    return [[journey["id"], "MOT.TAM", journey["direction"], str(order), name, clock, ""]
            for journey in journeys for order, (name, clock) in enumerate(journey["stops"], start=1)]


def read_current(path: Path) -> dict[str, list[list[str]]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.reader(stream, delimiter=";")
        if next(reader, None) != generate_data.MOTTAM_COLUMNS:
            raise ScheduleError("Colonne del CSV MOT.TAM inattese")
        rows = list(reader)
    result = group_rows(rows)
    if not result:
        raise ScheduleError("CSV MOT.TAM corrente vuoto")
    return result


def group_rows(rows: list[list[str]]) -> dict[str, list[list[str]]]:
    result = {}
    for row in rows:
        if len(row) != len(generate_data.MOTTAM_COLUMNS) or row[1] != "MOT.TAM":
            raise ScheduleError("Riga CSV MOT.TAM non valida")
        result.setdefault(row[0], []).append(row)
    for journey_id, stops in result.items():
        stops.sort(key=lambda row: int(row[3]))
        if [int(row[3]) for row in stops] != list(range(1, len(stops) + 1)):
            raise ScheduleError(f"Ordine fermate non valido per {journey_id}")
    return result


def prepare_proposal(journeys: list[dict], updates: dict, output: Path) -> None:
    candidate = output / generate_data.MOTTAM_PATH.name
    with candidate.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter=";", lineterminator="\n")
        writer.writerow(generate_data.MOTTAM_COLUMNS)
        writer.writerows(csv_rows(journeys))
    (output / "data.js").write_text(generate_data.schedule_js(
        generate_data.read_trips(), generate_data.read_mottam_journeys(candidate), updates
    ), encoding="utf-8")
    (output / generate_data.LINE_UPDATES_PATH.name).write_text(
        json.dumps(updates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def markdown_report(report: dict) -> str:
    if report["status"] == "error":
        return f"# Controllo MOT.TAM non riuscito\n\n{report['error']}\n\nNessun dato MOT.TAM modificato.\n"
    lines = ["# Controllo MOT.TAM Benevento - Napoli", "", f"Esito: **{report['status']}**", "",
             f"Fonte: {report['source_url'] or report['local_html']}",
             f"Controllato: {report['checked_at']}",
             f"Corse: {report['journey_count']} (entrambe le direzioni)", ""]
    for key, title in (("added", "Corse aggiunte"), ("removed", "Corse rimosse"), ("modified", "Corse modificate")):
        lines.extend([f"## {title}", ""])
        lines.extend(f"- {item['id']}: {item.get('description', '')}" for item in report[key])
        if not report[key]:
            lines.append("Nessuna.")
        lines.append("")
    lines.extend(["Ogni corsa conserva tutte le fermate. Via De Pretis in andata resta senza orario, "
                  "come nella fonte. Validita' trattata come lun-ven secondo la scelta del sito.", "",
                  "File e data di conferma aggiornati." if report["applied"] else "Solo report e file proposti.", ""])
    return "\n".join(lines)


def describe(rows: list[list[str]]) -> str:
    return " → ".join(f"{row[5] or 'n.d.'} {row[4]}" for row in rows)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", type=Path, help="HTML locale: non avanza la conferma online")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "tmp" / "mottam-check")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    published = generate_data.TIMETABLES_PATH.resolve()
    if output == ROOT or output == published or published in output.parents:
        parser.error("Output da tenere separato dai dati pubblicati")
    output.mkdir(parents=True, exist_ok=True)
    filenames = (generate_data.MOTTAM_PATH.name, "data.js", generate_data.LINE_UPDATES_PATH.name)
    for name in filenames:
        (output / name).unlink(missing_ok=True)
    report = {"status": "error", "checked_at": datetime.now(timezone.utc).isoformat(), "applied": False}
    try:
        source_url = None
        if args.html:
            content = args.html.read_bytes()
            html = decode_html(content)
        else:
            content, home_url = download(HOME_URL)
            html = decode_html(content)
            source_url = discover_schedule(html, home_url)
            if source_url != urldefrag(home_url)[0]:
                content, source_url = download(source_url)
                html = decode_html(content)
        (output / "source.html").write_bytes(content)
        journeys = parse_schedule(html)
        originals = {path: path.read_bytes() for path in (
            generate_data.MOTTAM_PATH, generate_data.OUTPUT_PATH, generate_data.LINE_UPDATES_PATH
        )}
        current = read_current(generate_data.MOTTAM_PATH)
        proposed = group_rows(csv_rows(journeys))
        added = [{"id": key, "description": describe(proposed[key])} for key in sorted(proposed.keys() - current.keys())]
        removed = [{"id": key, "description": describe(current[key])} for key in sorted(current.keys() - proposed.keys())]
        modified = [{"id": key, "description": f"{describe(current[key])} => {describe(proposed[key])}",
                     "before": current[key], "after": proposed[key]}
                    for key in sorted(current.keys() & proposed.keys()) if current[key] != proposed[key]]
        changed = bool(added or removed or modified)
        previous_updates = generate_data.read_line_updates()
        updates = {line: dict(entry) for line, entry in previous_updates.items()}
        if changed:
            updates["MOT.TAM"]["updated_at"] = report["checked_at"]
        if args.html is None:
            updates["MOT.TAM"]["checked_at"] = report["checked_at"]
        prepare_proposal(journeys, updates, output)
        if not changed:
            (output / generate_data.MOTTAM_PATH.name).write_bytes(originals[generate_data.MOTTAM_PATH])
        report.update({"status": "changes" if changed else "unchanged", "source_url": source_url,
                       "local_html": str(args.html) if args.html else None,
                       "html_sha256": hashlib.sha256(content).hexdigest(), "journey_count": len(journeys),
                       "added": added, "removed": removed, "modified": modified,
                       "metadata_updated": updates != previous_updates, "journeys": journeys})
        if args.apply and (changed or updates != previous_updates):
            apply_proposal(output, originals)
            report["applied"] = True
    except Exception as error:
        report.update({"status": "error", "error": f"{type(error).__name__}: {error}"})
        for name in filenames:
            (output / name).unlink(missing_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = markdown_report(report)
    (output / "report.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
