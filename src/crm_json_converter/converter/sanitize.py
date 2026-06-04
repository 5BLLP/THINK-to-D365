from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from .models import FieldMapping


_NUMERIC_ONLY_OPTION_SET_FIELDS = {"order_status", "payment_status"}


def sanitize_record(
    record: dict[str, Any],
    field_map: dict[str, FieldMapping],
    record_index: int,
    errors: list[str],
) -> dict[str, Any]:
    sanitized = dict(record)
    for key, value in record.items():
        field = field_map.get(key)
        if field is None:
            continue
        sanitized[key] = sanitize_value(value, field, record_index, errors)
    return sanitized


def sanitize_value(value: Any, field: FieldMapping, record_index: int, errors: list[str]) -> Any:
    if value is None:
        return None
    if field.lookup_target:
        return str(value)
    if field.data_type == "guid":
        return sanitize_guid(value, field, record_index, errors)
    if field.data_type == "string":
        return sanitize_string(value, field, record_index, errors)
    if field.data_type == "whole_number":
        return sanitize_number(value, field, record_index, errors, allow_decimal=False)
    if field.data_type in {"decimal", "currency"}:
        return sanitize_number(value, field, record_index, errors, allow_decimal=True)
    if field.data_type == "boolean":
        return sanitize_boolean(value, field, record_index, errors)
    if field.data_type == "two_options":
        return sanitize_two_options(value, field, record_index, errors)
    if field.data_type == "optionset":
        return sanitize_option_set(value, field, record_index, errors)
    if field.data_type in {"datetime", "date"}:
        return sanitize_datetime(value, field, record_index, errors)
    return value


def sanitize_guid(value: Any, field: FieldMapping, record_index: int, errors: list[str]) -> str | None:
    text = str(value).strip()
    try:
        UUID(text)
    except (ValueError, AttributeError):
        errors.append(f"record {record_index} field '{field.source_column}': invalid GUID, set to null")
        return None
    return text


def sanitize_string(value: Any, field: FieldMapping, record_index: int, errors: list[str]) -> str:
    text = str(value)
    if field.max_length is not None and len(text) > field.max_length:
        errors.append(
            f"record {record_index} field '{field.source_column}': truncated from {len(text)} to {field.max_length} characters"
        )
        return text[: field.max_length]
    return text


def sanitize_number(
    value: Any,
    field: FieldMapping,
    record_index: int,
    errors: list[str],
    *,
    allow_decimal: bool,
) -> int | float | None:
    parsed: int | float
    try:
        if isinstance(value, bool):
            raise ValueError
        if allow_decimal:
            parsed = float(value)
        else:
            if isinstance(value, str) and ("." in value or "e" in value.lower()):
                raise ValueError
            parsed = int(value)
    except (TypeError, ValueError):
        errors.append(f"record {record_index} field '{field.source_column}': invalid number, set to null")
        return None

    if field.minimum is not None and parsed < field.minimum:
        errors.append(f"record {record_index} field '{field.source_column}': clamped to minimum {field.minimum}")
        parsed = field.minimum
    if field.maximum is not None and parsed > field.maximum:
        errors.append(f"record {record_index} field '{field.source_column}': clamped to maximum {field.maximum}")
        parsed = field.maximum

    return float(parsed) if allow_decimal else int(parsed)


def sanitize_two_options(value: Any, field: FieldMapping, record_index: int, errors: list[str]) -> int | None:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"yes", "y", "true", "1"}:
            return 1
        if lowered in {"no", "n", "false", "0"}:
            return 0
    if isinstance(value, (int, float)) and value in {0, 1}:
        return int(value)
    errors.append(f"record {record_index} field '{field.source_column}': invalid two-option value, set to null")
    return None


def sanitize_boolean(value: Any, field: FieldMapping, record_index: int, errors: list[str]) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"yes", "y", "true", "1"}:
            return True
        if lowered in {"no", "n", "false", "0"}:
            return False
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(int(value))
    errors.append(f"record {record_index} field '{field.source_column}': invalid boolean value, set to null")
    return None


def sanitize_option_set(value: Any, field: FieldMapping, record_index: int, errors: list[str]) -> int | None:
    options = field.options or {}
    allowed_values = set(options.values())
    if not options:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered.isdigit():
                return int(lowered)
        if isinstance(value, (int, float)) and int(value) == value:
            return int(value)
    if field.source_column in _NUMERIC_ONLY_OPTION_SET_FIELDS:
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                numeric = int(text)
                if numeric in allowed_values:
                    return numeric
        if isinstance(value, (int, float)) and int(value) == value and int(value) in allowed_values:
            return int(value)
        errors.append(f"record {record_index} field '{field.source_column}': invalid option value, set to null")
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        for label, option_value in options.items():
            if lowered == label.lower():
                return option_value
        if value.isdigit():
            numeric = int(value)
            if numeric in allowed_values:
                return numeric
    if isinstance(value, (int, float)) and int(value) in allowed_values:
        return int(value)
    errors.append(f"record {record_index} field '{field.source_column}': invalid option value, set to null")
    return None


def sanitize_datetime(value: Any, field: FieldMapping, record_index: int, errors: list[str]) -> str | None:
    text = str(value).strip()
    for parser in (
        lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00")),
        lambda raw: datetime.strptime(raw, "%b %d %Y %I:%M:%S:%f%p"),
    ):
        try:
            parsed = parser(text)
        except ValueError:
            continue
        return parsed.isoformat()
    errors.append(f"record {record_index} field '{field.source_column}': invalid datetime, set to null")
    return None
