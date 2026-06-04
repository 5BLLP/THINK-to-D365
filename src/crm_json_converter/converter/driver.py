from __future__ import annotations

from typing import Any

from .json_io import extract_records_container, rebuild_payload
from .mappings import get_table_mapping
from .sanitize import sanitize_record


def transform_records(table_name: str, payload: Any) -> tuple[Any, list[str]]:
    mapping = get_table_mapping(table_name)
    field_map = {field.source_column: field for field in mapping.fields}
    records, container = extract_records_container(payload)
    errors: list[str] = []
    sanitized_records = [
        sanitize_record(record, field_map, record_index, errors)
        for record_index, record in enumerate(records, start=1)
    ]
    return rebuild_payload(payload, container, sanitized_records), errors
