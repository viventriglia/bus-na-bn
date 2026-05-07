from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "orari_eav_air.csv"
OUTPUT_PATH = ROOT / "data.js"

COLUMNS = [
    "orario partenza",
    "orario arrivo",
    "stazione partenza",
    "stazione arrivo",
    "linea",
]


def main() -> None:
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter=";")
        missing = [column for column in COLUMNS if column not in (reader.fieldnames or [])]

        if missing:
            raise SystemExit(f"Colonne mancanti nel CSV: {', '.join(missing)}")

        trips = [
            [row[column].strip() for column in COLUMNS]
            for row in reader
            if any((row.get(column) or "").strip() for column in COLUMNS)
        ]

    payload = {
        "columns": COLUMNS,
        "trips": trips,
    }
    js = '"use strict";\n\nwindow.BUS_SCHEDULE_DATA = '
    js += json.dumps(payload, ensure_ascii=False, indent=2)
    js += ";\n"

    OUTPUT_PATH.write_text(js, encoding="utf-8")
    print(f"Generato {OUTPUT_PATH.name}: {len(trips)} corse")


if __name__ == "__main__":
    main()
