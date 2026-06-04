from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def extract_records_container(payload: Any) -> tuple[list[dict[str, Any]], str]:
    if isinstance(payload, list):
        if any(not isinstance(record, dict) for record in payload):
            raise ValueError("Records must be a list of JSON objects.")
        return payload, "list"
    if isinstance(payload, dict) and "records" in payload:
        records = payload["records"]
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ValueError("The 'records' value must be a list of JSON objects.")
        return records, "records_object"
    raise ValueError("Input JSON must be a list of records or an object with a 'records' key.")


def rebuild_payload(original_payload: Any, container: str, records: list[dict[str, Any]]) -> Any:
    if container == "list":
        return records
    cloned = dict(original_payload)
    cloned["records"] = records
    return cloned


def get_sanitized_records(payload: Any) -> list[dict[str, Any]]:
    records, _ = extract_records_container(payload)
    return records
