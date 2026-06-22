from __future__ import annotations

from uuid import UUID, uuid5
from typing import Any

from .mappings import get_table_mapping


def _build_lookup_bind_path(entity_set: str, key_name: str, key_value: Any) -> str:
    if isinstance(key_value, bool):
        text = str(key_value).lower()
        return f"/{entity_set}({key_name}={text})"
    if isinstance(key_value, (int, float)):
        return f"/{entity_set}({key_name}={key_value})"
    if key_name == "jh_thinkidnbr":
        text = str(key_value).strip()
        if text.isdigit():
            return f"/{entity_set}({key_name}={int(text)})"
    text = str(key_value).replace("'", "''")
    return f"/{entity_set}({key_name}='{text}')"


def _build_entity_bind_path(entity_set: str, record_id: Any) -> str:
    return f"/{entity_set}({record_id})"


def build_entitlement_guid(sanitized_record: dict[str, Any]) -> UUID:
    order_id = str(sanitized_record.get("orderhdr_id") or "").strip()
    if not order_id:
        raise ValueError("entitlement requires orderhdr_id to build jh_entitlementid")
    try:
        return UUID(order_id)
    except (TypeError, ValueError):
        return uuid5(UUID("12345678-1234-5678-1234-567812345678"), f"jh_entitlementid:{order_id}")


def build_entitlement_id(sanitized_record: dict[str, Any]) -> str:
    return str(build_entitlement_guid(sanitized_record))


def build_payment_name(sanitized_record: dict[str, Any]) -> str:
    order_id = str(sanitized_record.get("orderhdr_id") or "").strip()
    sequence = str(sanitized_record.get("order_item_seq") or "").strip()
    if not order_id or not sequence:
        raise ValueError("payment requires both orderhdr_id and order_item_seq to build jh_name")
    value = f"{order_id}:{sequence}"
    if len(value) > 100:
        raise ValueError(f"payment jh_name '{value}' exceeds 100 characters")
    return value


def _normalize_payment_item_name(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return " ".join(value.split())

def build_d365_payload(
    table_name: str,
    sanitized_record: dict[str, Any],
    *,
    import_id: str | None = None,
) -> dict[str, Any]:
    mapping = get_table_mapping(table_name)
    if not mapping.d365_enabled:
        raise ValueError(f"Table '{table_name}' is not enabled for D365 push.")

    payload: dict[str, Any] = {}
    if table_name == "customer":
        payload["customertypecode"] = 10
    elif table_name == "agency":
        payload["customertypecode"] = 50
    payload["jh_integrationsource"] = "THINK"
    if import_id is not None:
        payload["jh_importid"] = import_id
    for field in mapping.fields:
        if not field.crm_schema_name:
            continue
        if table_name == "agency" and field.source_column in {"fname", "initial_name", "lname", "suffix"}:
            continue
        if table_name == "payment_item" and field.source_column in {"fname", "lname"}:
            continue
        if table_name == "payment_item" and field.source_column == "company":
            continue
        if table_name == "entitlement" and field.crm_schema_name == "jh_entitlementid":
            payload[field.crm_schema_name] = build_entitlement_id(sanitized_record)
            continue
        if table_name in {"payment", "payment_item"} and field.crm_schema_name == "jh_name":
            payload[field.crm_schema_name] = build_payment_name(sanitized_record)
            continue
        if field.source_column not in sanitized_record:
            continue
        value = sanitized_record[field.source_column]
        if value is None:
            continue
        if table_name == "payment_item" and field.crm_schema_name == "jh_entitlementid":
            entitlement_record_id = sanitized_record.get("_jh_entitlement_record_id")
            if entitlement_record_id in {None, ""}:
                raise ValueError("payment_item requires a resolved entitlement record id before payload build")
            payload[f"{field.crm_schema_name}@odata.bind"] = _build_entity_bind_path(
                field.lookup_bind_entity_set or "jh_entitlements",
                entitlement_record_id,
            )
            continue
        if field.crm_schema_name=="jh_countryid":

            country_guid = sanitized_record.get(

                "_jh_country_record_id"

            )
            if country_guid:
                payload[
                    f"{field.crm_schema_name}@odata.bind"
                ] = _build_entity_bind_path(
                        field.lookup_bind_entity_set,
                        country_guid
                )


            continue
        if field.lookup_bind_entity_set and field.lookup_bind_key and value in {None, ""}:
            continue
        if field.lookup_bind_entity_set and field.lookup_bind_key:
            bind_path = _build_lookup_bind_path(
                field.lookup_bind_entity_set,
                field.lookup_bind_key,
                value,
            )


            payload[f"{field.crm_schema_name}@odata.bind"] = bind_path
            continue
        if table_name == "payment_item" and field.crm_schema_name == "jh_name":
            value = _normalize_payment_item_name(value)
        payload[field.crm_schema_name] = value

    if table_name in {"payment", "payment_item"}:
        order_item_seq = sanitized_record.get("order_item_seq")
        if order_item_seq not in {None, ""}:
            payload["jh_sequence"] = order_item_seq
       



    return payload
