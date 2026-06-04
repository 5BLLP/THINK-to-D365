from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


SAMPLE_PATH = Path("samples/muse-crm-customers-live-2026-05-25_04-07-15.json")
BATCH_SIZE = 50


def main() -> int:
    records = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    by_customer_id: dict[str, list[tuple[int, str | None, str | None]]] = defaultdict(list)

    for index, record in enumerate(records, start=1):
        customer_id = str(record.get("customer_id"))
        company = record.get("company")
        ringgold = record.get("ringgold")
        by_customer_id[customer_id].append((index, company, None if ringgold is None else str(ringgold)))

    duplicates = {
        customer_id: entries
        for customer_id, entries in by_customer_id.items()
        if len(entries) > 1
    }
    same_ringgold = {
        customer_id: entries
        for customer_id, entries in duplicates.items()
        if len({ringgold for _, _, ringgold in entries}) == 1
    }
    mixed_ringgold = {
        customer_id: entries
        for customer_id, entries in duplicates.items()
        if len({ringgold for _, _, ringgold in entries}) > 1
    }

    print(f"records: {len(records)}")
    print(f"duplicate customer_id values: {len(duplicates)}")
    print(f"duplicate customer_id values with same ringgold: {len(same_ringgold)}")
    print(f"duplicate customer_id values with mixed ringgold: {len(mixed_ringgold)}")

    for customer_id in sorted(duplicates, key=lambda value: int(value) if value.isdigit() else value):
        entries = duplicates[customer_id]
        chunks = sorted({((index - 1) // BATCH_SIZE) + 1 for index, _, _ in entries})
        ringgold_values = sorted({ringgold for _, _, ringgold in entries}, key=lambda value: "" if value is None else value)
        print(
            f"customer_id={customer_id} count={len(entries)} chunks={chunks} "
            f"ringgold={ringgold_values} "
            f"rows={[(index, company, ringgold) for index, company, ringgold in entries]}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
