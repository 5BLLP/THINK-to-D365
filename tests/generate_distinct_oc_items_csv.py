from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path


SAMPLE_PATH = Path("samples/muse-crm-order-items-live-2026-05-25_04-07-15.json")
OUTPUT_PATH = Path("output/distinct_oc_id_oc_desc.csv")


def main() -> int:
    records = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    counts: Counter[tuple[str, str]] = Counter()

    for record in records:
        oc_id = record.get("oc_id")
        oc_desc = record.get("oc_desc")
        if oc_id is None or oc_desc is None:
            continue
        counts[(str(oc_id), str(oc_desc))] += 1

    rows = sorted(
        ((oc_id, oc_desc, count) for (oc_id, oc_desc), count in counts.items()),
        key=lambda row: (int(row[0]) if row[0].isdigit() else row[0], row[1]),
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["oc_id", "oc_desc", "occurrences"])
        writer.writerows(rows)

    print(f"Wrote {len(rows)} distinct rows to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
