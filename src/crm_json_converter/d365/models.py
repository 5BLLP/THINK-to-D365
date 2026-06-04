from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class D365TableConfig:
    entity_set: str
    match_field: str
    primary_id_field: str
    lookup_fields: tuple[str, ...] | None = None


@dataclass(frozen=True)
class D365BatchConfig:
    enabled: bool
    parallel: bool
    batch_size: int
    max_workers: int
    retry_attempts: int = 10
    request_timeout_seconds: int = 120


@dataclass(frozen=True)
class D365LogConfig:
    log_path: str | None
    log_dir: str


@dataclass(frozen=True)
class D365Config:
    tenant_id: str
    client_id: str
    client_secret: str
    resource_url: str
    tables: dict[str, D365TableConfig]
    batch: D365BatchConfig
    logging: D365LogConfig
