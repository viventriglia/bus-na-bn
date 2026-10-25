from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TIMETABLES_PATH = ROOT / "assets" / "timetables"
CSV_PATH = TIMETABLES_PATH / "orari_eav_air.csv"
MOTTAM_PATH = TIMETABLES_PATH / "orari_mottam.csv"
LINE_UPDATES_PATH = TIMETABLES_PATH / "line_updates.json"
OUTPUT_PATH = ROOT / "data.js"

COLUMNS = [
    "orario partenza",
    "orario arrivo",
    "stazione partenza",
    "stazione arrivo",
    "linea",
]
OPTIONAL_COLUMNS = ["categoria", "numero corsa", "note"]
TRIP_COLUMNS = COLUMNS + OPTIONAL_COLUMNS

MOTTAM_COLUMNS = [
    "corsa_id",
    "linea",
    "direzione",
    "ordine",
    "fermata",
    "orario",
    "nota",
]


def read_trips() -> list[list[str]]:
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter=";")
        missing = [column for column in COLUMNS if column not in (reader.fieldnames or [])]

        if missing:
            raise SystemExit(f"Colonne mancanti nel CSV: {', '.join(missing)}")

        if reader.fieldnames not in (COLUMNS, TRIP_COLUMNS):
            raise ValueError("Schema CSV non riconosciuto: evitare di perdere colonne aggiuntive")
        columns = reader.fieldnames

        return [
            [(row[column] or "").strip() for column in columns]
            for row in reader
            if any((row.get(column) or "").strip() for column in COLUMNS)
        ]


def columns_for_trips(trips: list[list[str]]) -> list[str]:
    if any(len(row) > len(TRIP_COLUMNS) or len(row) < len(COLUMNS) for row in trips):
        raise ValueError("Numero di campi inatteso negli orari")
    return TRIP_COLUMNS if any(len(row) > len(COLUMNS) for row in trips) else COLUMNS


def normalized_trips(trips: list[list[str]], columns: list[str]) -> list[list[str]]:
    return [row + [""] * (len(columns) - len(row)) for row in trips]


def read_mottam_journeys(path: Path | None = None) -> list[dict[str, object]]:
    with (path or MOTTAM_PATH).open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter=";")
        missing = [column for column in MOTTAM_COLUMNS if column not in (reader.fieldnames or [])]

        if missing:
            raise SystemExit(f"Colonne mancanti in {MOTTAM_PATH.name}: {', '.join(missing)}")

        journeys: dict[str, dict[str, object]] = {}
        for row_number, row in enumerate(reader, start=2):
            values = {column: (row.get(column) or "").strip() for column in MOTTAM_COLUMNS}
            if not any(values.values()):
                continue

            journey_id = values["corsa_id"]
            if not journey_id:
                raise SystemExit(f"Riga {row_number} di {MOTTAM_PATH.name}: corsa_id mancante")

            try:
                order = int(values["ordine"])
            except ValueError as error:
                raise SystemExit(
                    f"Riga {row_number} di {MOTTAM_PATH.name}: ordine non valido"
                ) from error

            journey = journeys.setdefault(
                journey_id,
                {
                    "id": journey_id,
                    "line": values["linea"],
                    "direction": values["direzione"],
                    "stops": [],
                },
            )
            journey["stops"].append(
                {
                    "order": order,
                    "name": values["fermata"],
                    "time": values["orario"],
                    "note": values["nota"],
                }
            )

    result = list(journeys.values())
    for journey in result:
        journey["stops"].sort(key=lambda stop: stop["order"])

    return result


def read_line_updates() -> dict[str, dict[str, str | None]]:
    updates = json.loads(LINE_UPDATES_PATH.read_text(encoding="utf-8"))
    for line in ("AIR", "EAV", "MOT.TAM"):
        entry = updates[line]
        for key in ("updated_at", "checked_at"):
            value = entry[key]
            if value is None and key == "checked_at":
                continue
            if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
                raise ValueError(f"Data {key} non valida per {line}")
    return updates


def schedule_js(trips: list[list[str]], journeys: list[dict], line_updates: dict) -> str:
    columns = columns_for_trips(trips)
    payload = {
        "columns": columns,
        "trips": normalized_trips(trips, columns),
        "journeys": journeys,
        "lineUpdates": line_updates,
    }
    js = '"use strict";\n\nwindow.BUS_SCHEDULE_DATA = '
    js += json.dumps(payload, ensure_ascii=False, indent=2)
    js += ";\n"
    return js


def main() -> None:
    trips = read_trips()
    journeys = read_mottam_journeys()
    js = schedule_js(trips, journeys, read_line_updates())

    OUTPUT_PATH.write_text(js, encoding="utf-8")
    print(f"Generato {OUTPUT_PATH.name}: {len(trips) + len(journeys)} corse")


if __name__ == "__main__":
    main()
