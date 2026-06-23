from __future__ import annotations

import os
from pathlib import Path

from .models import D365BatchConfig, D365Config, D365LogConfig, D365TableConfig


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"").strip("'"))


def read_bool_env(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def read_int_env(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def read_required_env(name: str, legacy_name: str | None = None) -> str:
    value = os.environ.get(name)
    if value:
        return value
    if legacy_name:
        legacy_value = os.environ.get(legacy_name)
        if legacy_value:
            return legacy_value
    raise ValueError(name)


def load_config() -> D365Config:
    load_dotenv()
    tenant_id = read_required_env("D365_TENANT_ID")
    client_id = read_required_env("D365_CLIENT_ID")
    client_secret = read_required_env("D365_CLIENT_SECRET")
    resource_url = read_required_env("D365_RESOURCE_URL").rstrip("/")

    customer_entity_set = read_required_env("D365_CUSTOMER_ENTITY_SET")
    customer_match_field = read_required_env("D365_CUSTOMER_MATCH_FIELD")
    customer_primary_id_field = read_required_env("D365_CUSTOMER_PRIMARY_ID_FIELD")

    agency_entity_set = read_required_env("D365_AGENCY_ENTITY_SET")
    agency_match_field = read_required_env("D365_AGENCY_MATCH_FIELD")
    agency_primary_id_field = read_required_env("D365_AGENCY_PRIMARY_ID_FIELD")

    payment_entity_set = read_required_env("D365_PAYMENT_ENTITY_SET")
    payment_match_field = read_required_env("D365_PAYMENT_MATCH_FIELD")
    payment_primary_id_field = read_required_env("D365_PAYMENT_PRIMARY_ID_FIELD")

    order_item_entity_set = read_required_env("D365_ORDER_ITEM_ENTITY_SET", "D365_PAYMENT_ITEM_ENTITY_SET")
    order_item_match_field = read_required_env("D365_ORDER_ITEM_MATCH_FIELD", "D365_PAYMENT_ITEM_MATCH_FIELD")
    order_item_primary_id_field = read_required_env("D365_ORDER_ITEM_PRIMARY_ID_FIELD", "D365_PAYMENT_ITEM_PRIMARY_ID_FIELD")

    order_item_table = D365TableConfig(
        entity_set=order_item_entity_set,
        match_field=order_item_match_field,
        primary_id_field=order_item_primary_id_field,
        lookup_fields=("_jh_entitlementid_value", "jh_name"),
    )

    tables = {
        "customer": D365TableConfig(
            entity_set=customer_entity_set,
            match_field=customer_match_field,
            primary_id_field=customer_primary_id_field,
        ),
        "agency": D365TableConfig(
            entity_set=agency_entity_set,
            match_field=agency_match_field,
            primary_id_field=agency_primary_id_field,
        ),
        "entitlement": D365TableConfig(
            entity_set=payment_entity_set,
            match_field="jh_entitlementid",
            primary_id_field=payment_primary_id_field,
        ),
        "payment": D365TableConfig(
            entity_set=payment_entity_set,
            match_field=payment_match_field,
            primary_id_field=payment_primary_id_field,
        ),
        "order_item": order_item_table,
        "payment_item": order_item_table,
    }

    batch_size = max(1, read_int_env("D365_BATCH_SIZE", default=50))
    batch = D365BatchConfig(
        enabled=read_bool_env("D365_BATCH_ENABLED", default=True),
        parallel=read_bool_env("D365_BATCH_PARALLEL", default=True),
        batch_size=batch_size,
        max_workers=max(1, read_int_env("D365_BATCH_MAX_WORKERS", default=4)),
        retry_attempts=min(max(1, read_int_env("D365_BATCH_RETRY_ATTEMPTS", default=10)), 10),
    )
    logging = D365LogConfig(
        log_path=os.environ.get("D365_LOG_PATH"),
        log_dir=os.environ.get("D365_LOG_DIR", "logs"),
    )

    return D365Config(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        resource_url=resource_url,
        tables=tables,
        batch=batch,
        logging=logging,
    )
