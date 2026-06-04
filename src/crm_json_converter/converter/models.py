from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldMapping:
    source_column: str | None
    crm_column: str | None
    crm_schema_name: str | None
    data_type: str
    lookup_target: str | None = None
    lookup_bind_entity_set: str | None = None
    lookup_bind_key: str | None = None
    options: dict[str, int] | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    max_length: int | None = None
    notes: str | None = None


@dataclass(frozen=True)
class TableMapping:
    source_table: str
    target_entity: str
    fields: tuple[FieldMapping, ...]
    d365_enabled: bool = True
