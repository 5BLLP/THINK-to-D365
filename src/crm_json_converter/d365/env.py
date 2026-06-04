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


def load_config() -> D365Config:
    load_dotenv()
    required = [
        "D365_TENANT_ID",
        "D365_CLIENT_ID",
        "D365_CLIENT_SECRET",
        "D365_RESOURCE_URL",
        "D365_CUSTOMER_ENTITY_SET",
        "D365_CUSTOMER_MATCH_FIELD",
        "D365_CUSTOMER_PRIMARY_ID_FIELD",
        "D365_AGENCY_ENTITY_SET",
        "D365_AGENCY_MATCH_FIELD",
        "D365_AGENCY_PRIMARY_ID_FIELD",
        "D365_PAYMENT_ENTITY_SET",
        "D365_PAYMENT_MATCH_FIELD",
        "D365_PAYMENT_PRIMARY_ID_FIELD",
        "D365_PAYMENT_ITEM_ENTITY_SET",
        "D365_PAYMENT_ITEM_MATCH_FIELD",
        "D365_PAYMENT_ITEM_PRIMARY_ID_FIELD",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError(f"Missing D365 configuration values: {', '.join(missing)}")

    tables = {
        "customer": D365TableConfig(
            entity_set=os.environ["D365_CUSTOMER_ENTITY_SET"],
            match_field=os.environ["D365_CUSTOMER_MATCH_FIELD"],
            primary_id_field=os.environ["D365_CUSTOMER_PRIMARY_ID_FIELD"],
        ),
        "agency": D365TableConfig(
            entity_set=os.environ["D365_AGENCY_ENTITY_SET"],
            match_field=os.environ["D365_AGENCY_MATCH_FIELD"],
            primary_id_field=os.environ["D365_AGENCY_PRIMARY_ID_FIELD"],
        ),
        "payment": D365TableConfig(
            entity_set=os.environ["D365_PAYMENT_ENTITY_SET"],
            match_field=os.environ["D365_PAYMENT_MATCH_FIELD"],
            primary_id_field=os.environ["D365_PAYMENT_PRIMARY_ID_FIELD"],
        ),
        "payment_item": D365TableConfig(
            entity_set=os.environ["D365_PAYMENT_ITEM_ENTITY_SET"],
            match_field=os.environ["D365_PAYMENT_ITEM_MATCH_FIELD"],
            primary_id_field=os.environ["D365_PAYMENT_ITEM_PRIMARY_ID_FIELD"],
            lookup_fields=("_jh_entitlementid_value", "jh_name"),
        ),
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
    resource_url = os.environ["D365_RESOURCE_URL"].rstrip("/")

    return D365Config(
        tenant_id=os.environ["D365_TENANT_ID"],
        client_id=os.environ["D365_CLIENT_ID"],
        client_secret=os.environ["D365_CLIENT_SECRET"],
        resource_url=resource_url,
        tables=tables,
        batch=batch,
        logging=logging,
    )
