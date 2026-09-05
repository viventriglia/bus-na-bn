from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TIMETABLES_PATH = ROOT / "assets" / "timetables"
CSV_PATH = TIMETABLES_PATH / "orari_eav_air.csv"
MOTTAM_PATH = TIMETABLES_PATH / "orari_mottam.csv"
OUTPUT_PATH = ROOT / "data.js"

COLUMNS = [
    "orario partenza",
    "orario arrivo",
    "stazione partenza",
    "stazione arrivo",
    "linea",
]

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

        return [
            [row[column].strip() for column in COLUMNS]
            for row in reader
            if any((row.get(column) or "").strip() for column in COLUMNS)
        ]


def read_mottam_journeys() -> list[dict[str, object]]:
    with MOTTAM_PATH.open(newline="", encoding="utf-8-sig") as source:
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


def main() -> None:
    trips = read_trips()
    journeys = read_mottam_journeys()

    payload = {
        "columns": COLUMNS,
        "trips": trips,
        "journeys": journeys,
    }
    js = '"use strict";\n\nwindow.BUS_SCHEDULE_DATA = '
    js += json.dumps(payload, ensure_ascii=False, indent=2)
    js += ";\n"

    OUTPUT_PATH.write_text(js, encoding="utf-8")
    print(f"Generato {OUTPUT_PATH.name}: {len(trips) + len(journeys)} corse")


if __name__ == "__main__":
    main()
