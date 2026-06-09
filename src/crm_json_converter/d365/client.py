from __future__ import annotations

from datetime import date
import json
import os
import random
import time
from typing import Any

from ..converter import (
    build_d365_payload,
    build_payment_name,
    get_sanitized_records,
    get_source_column_for_schema,
    get_table_mapping,
)
from .batch import D365BatchRunner, _lookup_result_from_item
from .helpers import build_odata_filter, chunked
from .models import D365Config, D365TableConfig
from .structured_log import StructuredLogger


API_VERSION = "v9.2"
_ACCOUNT_TABLE_NAMES = {"customer", "agency"}


def _normalize_whitespace(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return " ".join(value.split())


def _normalize_lookup_key(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _coerce_int_lookup_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    return None


class D365Client:
    def __init__(self, config: D365Config, *, debug_http: bool = False) -> None:
        self.config = config
        self.debug_http = debug_http
        self._requests = __import__("requests")
        self._msal = __import__("msal")
        self.logger = StructuredLogger(config.logging)

        self._msal_app = self._msal.ConfidentialClientApplication(
            client_id=self.config.client_id,
            client_credential=self.config.client_secret,
            authority=f"https://login.microsoftonline.com/{self.config.tenant_id}",
        )
        token = self._acquire_access_token()
        self._default_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
        }
        self.session = self._new_session()
        self.batch = D365BatchRunner(
            requests_module=self._requests,
            session_factory=self._new_session,
            batch_url=self._batch_url(),
            entity_url=self._entity_url,
            record_url=self._record_url,
            config=self.config.batch,
            logger=self.logger,
            debug_http=debug_http,
            auth_refresh=self._refresh_access_token,
        )

    def _new_session(self) -> Any:
        session = self._requests.Session()
        session.headers.update(self._default_headers)
        return session

    def _acquire_access_token(self) -> str:
        result = self._msal_app.acquire_token_for_client(scopes=[f"{self.config.resource_url}/.default"])
        token = result.get("access_token")
        if not token:
            description = result.get("error_description") or result.get("error") or "unknown authentication error"
            raise ValueError(f"Failed to acquire D365 access token: {description}")
        return token

    def _refresh_access_token(self) -> str:
        token = self._acquire_access_token()
        self._default_headers["Authorization"] = f"Bearer {token}"
        if hasattr(self, "session"):
            self.session.headers.update(self._default_headers)
        return token

    def _retry_attempts(self) -> int:
        return min(max(1, self.config.batch.retry_attempts), 10)

    def _retry_delay_seconds(self, attempt: int) -> float:
        base_delay = min(30.0, 0.5 * (2 ** (attempt - 1)))
        jitter = random.uniform(0.0, 0.25)
        return base_delay + jitter

    def _is_retryable_http_status(self, status_code: int | None) -> bool:
        if status_code is None:
            return False
        if status_code == 401:
            return True
        if status_code in {408, 429}:
            return True
        return 500 <= status_code <= 599

    def _current_import_id(self) -> str:
        return f"T{date.today().strftime('%Y%m%d')}"

    def _merge_import_id(self, existing_import_id: str | None) -> str:
        current_import_id = self._current_import_id()
        if not existing_import_id:
            return current_import_id
        parts = [part.strip() for part in existing_import_id.split(",") if part.strip()]
        if current_import_id in parts:
            return ", ".join(parts)
        return f"{', '.join(parts)}, {current_import_id}"

    def _batch_flow_chunk_size(self) -> int:
        return max(1, self.config.batch.batch_size)

    def _lookup_field_names(self, table_name: str, table_config: D365TableConfig) -> tuple[str, ...]:
        lookup_fields = getattr(table_config, "lookup_fields", None)
        return lookup_fields or (table_config.match_field,)

    def _lookup_values_for_payload(
        self,
        table_name: str,
        table_config: D365TableConfig,
        record: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        lookup_values: dict[str, Any] = {}
        for field_name in self._lookup_field_names(table_name, table_config):
            if table_name == "payment" and field_name == "jh_name":
                value = payload.get(field_name)
            else:
                source_column = get_source_column_for_schema(table_name, field_name)
                value = record.get(source_column) if source_column else payload.get(field_name)
            if self._is_account_table(table_name) and field_name == table_config.match_field:
                value = _normalize_lookup_key(value)
            lookup_values[field_name] = value
        return lookup_values

    def _lookup_display(self, lookup_values: dict[str, Any]) -> str:
        parts: list[str] = []
        for field_name, value in lookup_values.items():
            parts.append(f"{field_name}={value}")
        return " and ".join(parts)

    def _is_account_table(self, table_name: str) -> bool:
        return table_name in _ACCOUNT_TABLE_NAMES

    def _log_record_upsert(
        self,
        *,
        table_name: str,
        record_index: int,
        outcome: str,
        mode: str,
        lookup_display: str | None = None,
        operation: str | None = None,
        entity_set: str | None = None,
        match_field: str | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        details: dict[str, Any] = {"mode": mode}
        if lookup_display is not None:
            details["match_value"] = lookup_display
        if operation is not None:
            details["operation"] = operation
        if entity_set is not None:
            details["entity_set"] = entity_set
        if match_field is not None:
            details["match_field"] = match_field
        if message is not None:
            details["message"] = message
        if error is not None:
            details["error"] = error
        self.logger.write(
            event_type="record_upsert",
            table_name=table_name,
            record_index=record_index,
            outcome=outcome,
            **details,
        )

    def _source_key_display(self, source_values: dict[str, Any]) -> str:
        parts: list[str] = []
        for field_name, value in source_values.items():
            parts.append(f"{field_name}={value}")
        return " and ".join(parts)

    def _payment_source_parts(self, record: dict[str, Any]) -> tuple[str, str, str]:
        order_id = str(record.get("orderhdr_id") or "").strip()
        sequence = str(record.get("order_item_seq") or "").strip()
        source_display = self._source_key_display({"orderhdr_id": order_id, "order_item_seq": sequence})
        return order_id, sequence, source_display

    def _payment_lookup_name(self, record: dict[str, Any]) -> str:
        try:
            return build_payment_name(record)
        except ValueError:
            return ""

    def _payment_item_source_parts(self, record: dict[str, Any]) -> tuple[str, str, str]:
        return self._payment_source_parts(record)

    def _payment_item_payload_record(self, record: dict[str, Any], entitlement_record_id: str) -> dict[str, Any]:
        payload_record = dict(record)
        payload_record["_jh_entitlement_record_id"] = entitlement_record_id
        return payload_record

    def _payment_item_sequence_lookup_value(self, sequence: Any) -> int | Any:
        if isinstance(sequence, bool):
            return sequence
        if isinstance(sequence, int):
            return sequence
        text = str(sequence).strip()
        if text.isdigit():
            return int(text)
        return sequence

    def _prepare_payment_batch_row(
        self,
        *,
        table_name: str,
        table_config: D365TableConfig,
        record_index: int,
        record: dict[str, Any],
        current_import_id: str,
        seen_source_keys: dict[tuple[str, str], int],
        mode: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        order_id, sequence, source_display = self._payment_source_parts(record)
        if not order_id or not sequence:
            error_text = f"missing orderhdr_id or order_item_seq for {source_display}"
            self._log_record_upsert(
                table_name=table_name,
                record_index=record_index,
                outcome="failure",
                mode=mode,
                error=error_text,
            )
            return None, None

        source_key = (order_id, sequence)
        first_seen_index = seen_source_keys.get(source_key)
        if first_seen_index is not None:
            message = f"[crm-json-transform] record {record_index}: skipped duplicate {source_display} (first seen at record {first_seen_index})"
            self._log_record_upsert(
                table_name=table_name,
                record_index=record_index,
                outcome="skipped",
                mode=mode,
                message=message.removeprefix("[crm-json-transform] "),
                match_field=table_config.match_field,
                lookup_display=source_display,
            )
            return None, message
        seen_source_keys[source_key] = record_index

        base_payload = build_d365_payload(table_name, record, import_id=current_import_id)
        if not base_payload:
            message = f"[crm-json-transform] record {record_index}: no D365 payload generated, skipped"
            self._log_record_upsert(
                table_name=table_name,
                record_index=record_index,
                outcome="skipped",
                mode=mode,
                message="no D365 payload generated",
            )
            return None, message

        lookup_values = self._lookup_values_for_payload(table_name, table_config, record, base_payload)
        lookup_display = self._lookup_display(lookup_values)
        return (
            {
                "record_index": record_index,
                "record": record,
                "match_value": base_payload.get(table_config.match_field),
                "lookup_values": lookup_values,
                "lookup_display": source_display or lookup_display,
            },
            None,
        )

    def _payment_account_lookup_record_id(self, customer_id: Any, record_index: int) -> str | None:
        account_id = _coerce_int_lookup_value(customer_id)
        if account_id is None:
            return None
        account_table_config = D365TableConfig(
            entity_set="accounts",
            match_field="jh_thinkidnbr",
            primary_id_field="accountid",
        )
        existing_id, _ = self._find_existing_record_compat(
            "customer",
            record_index,
            account_table_config,
            lookup_values={"jh_thinkidnbr": account_id},
        )
        if not existing_id:
            raise ValueError(f"account with account_id={account_id} does not exist")
        return existing_id

    def _payment_payload_record(
        self,
        record: dict[str, Any],
        *,
        record_index: int,
    ) -> dict[str, Any]:
        payload_record = dict(record)
        customer_id = payload_record.get("customer_id")
        if customer_id in {None, ""}:
            return payload_record
        self._payment_account_lookup_record_id(customer_id, record_index)
        return payload_record

    def _prepare_lookup_driven_batch_rows(
        self,
        *,
        table_name: str,
        table_config: D365TableConfig,
        records: list[dict[str, Any]],
        current_import_id: str,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        if self._is_account_table(table_name):
            return self._prepare_account_lookup_rows(
                table_name=table_name,
                table_config=table_config,
                records=records,
                current_import_id=current_import_id,
                mode="batch",
            )

        logs: list[str] = []
        seen_lookup_keys: dict[tuple[tuple[str, Any], ...], int] = {}
        payload_rows: list[dict[str, Any]] = []

        for record_index, record in enumerate(records, start=1):
            base_payload = build_d365_payload(table_name, record, import_id=current_import_id)
            if not base_payload:
                logs.append(f"[crm-json-transform] record {record_index}: no D365 payload generated, skipped")
                self.logger.write(
                    event_type="record_upsert",
                    table_name=table_name,
                    record_index=record_index,
                    outcome="skipped",
                    message="no D365 payload generated",
                )
                continue

            lookup_values = self._lookup_values_for_payload(table_name, table_config, record, base_payload)
            lookup_display = self._lookup_display(lookup_values)
            lookup_key = tuple(
                (field_name, lookup_values.get(field_name))
                for field_name in self._lookup_field_names(table_name, table_config)
            )
            if all(value not in {None, ""} for value in lookup_values.values()):
                first_seen_index = seen_lookup_keys.get(lookup_key)
                if first_seen_index is not None:
                    logs.append(
                        f"[crm-json-transform] record {record_index}: skipped duplicate {lookup_display} "
                        f"(first seen at record {first_seen_index})"
                    )
                    self.logger.write(
                        event_type="record_upsert",
                        table_name=table_name,
                        record_index=record_index,
                        outcome="skipped",
                        message=(
                            f"duplicate {lookup_display} "
                            f"(first seen at record {first_seen_index})"
                        ),
                        match_field=table_config.match_field,
                        match_value=lookup_display,
                        mode="batch",
                    )
                    continue
                seen_lookup_keys[lookup_key] = record_index

            payload_rows.append(
                {
                    "record_index": record_index,
                    "record": record,
                    "match_value": base_payload.get(table_config.match_field),
                    "lookup_values": lookup_values,
                    "lookup_display": lookup_display,
                }
            )

        return logs, payload_rows

    def _prepare_account_lookup_rows(
        self,
        *,
        table_name: str,
        table_config: D365TableConfig,
        records: list[dict[str, Any]],
        current_import_id: str,
        mode: str,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        logs: list[str] = []
        seen_lookup_keys: dict[str, int] = {}
        payload_rows_reversed: list[dict[str, Any]] = []

        for record_index in range(len(records), 0, -1):
            record = records[record_index - 1]
            base_payload = build_d365_payload(table_name, record, import_id=current_import_id)
            if not base_payload:
                logs.append(f"[crm-json-transform] record {record_index}: no D365 payload generated, skipped")
                self._log_record_upsert(
                    table_name=table_name,
                    record_index=record_index,
                    outcome="skipped",
                    mode=mode,
                    message="no D365 payload generated",
                )
                continue

            lookup_values = self._lookup_values_for_payload(table_name, table_config, record, base_payload)
            lookup_display = self._lookup_display(lookup_values)
            lookup_key = _normalize_lookup_key(lookup_values.get(table_config.match_field))
            first_kept_index = seen_lookup_keys.get(lookup_key)
            if first_kept_index is not None:
                logs.append(
                    f"[crm-json-transform] record {record_index}: skipped duplicate {lookup_display} "
                    f"(replaced by record {first_kept_index})"
                )
                self._log_record_upsert(
                    table_name=table_name,
                    record_index=record_index,
                    outcome="skipped",
                    mode=mode,
                    message=f"duplicate {lookup_display} (replaced by record {first_kept_index})",
                    match_field=table_config.match_field,
                    lookup_display=lookup_display,
                )
                continue

            seen_lookup_keys[lookup_key] = record_index
            payload_rows_reversed.append(
                {
                    "record_index": record_index,
                    "record": record,
                    "match_value": base_payload.get(table_config.match_field),
                    "lookup_values": lookup_values,
                    "lookup_display": lookup_display,
                }
            )

        payload_rows = list(reversed(payload_rows_reversed))
        return logs, payload_rows

    def _upsert_lookup_driven_row(
        self,
        *,
        table_name: str,
        table_config: D365TableConfig,
        record_index: int,
        record: dict[str, Any],
        current_import_id: str,
    ) -> tuple[str, str] | None:
        if table_name == "payment":
            return self._upsert_payment_lookup_driven_row(
                table_name=table_name,
                table_config=table_config,
                record_index=record_index,
                record=record,
                current_import_id=current_import_id,
            )
        base_payload = build_d365_payload(table_name, record, import_id=current_import_id)
        if not base_payload:
            return None

        lookup_values = self._lookup_values_for_payload(table_name, table_config, record, base_payload)
        lookup_display = self._lookup_display(lookup_values)
        existing_id, existing_import_id = self._find_existing_record_compat(
            table_name,
            record_index,
            table_config,
            lookup_values=lookup_values,
        )
        import_id = self._merge_import_id(existing_import_id) if existing_id else current_import_id
        payload = build_d365_payload(table_name, record, import_id=import_id)
        if existing_id:
            self._patch_record(table_name, record_index, table_config.entity_set, existing_id, payload)
            operation = "PATCH"
        else:
            self._post_record(table_name, record_index, table_config.entity_set, payload)
            operation = "POST"
        return operation, lookup_display

    def _upsert_payment_lookup_driven_row(
        self,
        *,
        table_name: str,
        table_config: D365TableConfig,
        record_index: int,
        record: dict[str, Any],
        current_import_id: str,
    ) -> tuple[str, str] | None:
        base_payload = build_d365_payload(table_name, self._payment_payload_record(record, record_index=record_index), import_id=current_import_id)
        if not base_payload:
            return None

        lookup_values = self._lookup_values_for_payload(table_name, table_config, record, base_payload)
        lookup_display = self._lookup_display(lookup_values)
        existing_id, existing_import_id = self._find_existing_record_compat(
            table_name,
            record_index,
            table_config,
            lookup_values=lookup_values,
        )
        import_id = self._merge_import_id(existing_import_id) if existing_id else current_import_id
        payload = build_d365_payload(table_name, self._payment_payload_record(record, record_index=record_index), import_id=import_id)
        if existing_id:
            self._patch_record(table_name, record_index, table_config.entity_set, existing_id, payload)
            operation = "PATCH"
        else:
            self._post_record(table_name, record_index, table_config.entity_set, payload)
            operation = "POST"
        return operation, lookup_display

    def _upsert_account_lookup_driven_row(
        self,
        *,
        table_name: str,
        table_config: D365TableConfig,
        record_index: int,
        record: dict[str, Any],
        current_import_id: str,
    ) -> tuple[str, str] | None:
        base_payload = build_d365_payload(table_name, record, import_id=current_import_id)
        if not base_payload:
            return None

        lookup_values = self._lookup_values_for_payload(table_name, table_config, record, base_payload)
        lookup_display = self._lookup_display(lookup_values)
        existing_id, existing_import_id = self._find_existing_record_compat(
            table_name,
            record_index,
            table_config,
            lookup_values=lookup_values,
        )
        import_id = self._merge_import_id(existing_import_id) if existing_id else current_import_id
        payload = build_d365_payload(table_name, record, import_id=import_id)
        if existing_id:
            try:
                self._patch_record(table_name, record_index, table_config.entity_set, existing_id, payload)
                operation = "PATCH"
            except Exception as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if not self._is_missing_record_http_error(status_code, str(exc)):
                    raise
                fallback_operation, _ = self._recover_missing_patch_write(
                    table_name=table_name,
                    table_config=table_config,
                    source_row={
                        "record_index": record_index,
                        "record": record,
                        "lookup_values": lookup_values,
                    },
                    current_import_id=current_import_id,
                )
                operation = fallback_operation
        else:
            self._post_record(table_name, record_index, table_config.entity_set, payload)
            operation = "POST"
        return operation, lookup_display

    def _run_lookup_driven_batch_chunk(
        self,
        *,
        table_name: str,
        table_config: D365TableConfig,
        source_chunk: list[dict[str, Any]],
        current_import_id: str,
    ) -> tuple[list[str], list[str]]:
        chunk_logs: list[str] = []
        chunk_failures: list[str] = []
        lookup_rows = [row for row in source_chunk if all(value not in {None, ""} for value in row["lookup_values"].values())]
        existing_ids: dict[int, str] = {}
        existing_import_ids: dict[int, str | None] = {}
        if lookup_rows:
            lookup_results = self.batch.lookup_existing_ids(
                table_name=table_name,
                table_config=table_config,
                chunk=lookup_rows,
            )
            for record_index, result in lookup_results.items():
                existing_ids[record_index] = result["record_id"]
                existing_import_ids[record_index] = result.get("import_id")

        write_rows: list[dict[str, Any]] = []
        for row in source_chunk:
            record_index = row["record_index"]
            record = row["record"]
            lookup_display = row["lookup_display"]
            record_id = existing_ids.get(record_index)
            import_id = (
                self._merge_import_id(existing_import_ids.get(record_index))
                if record_id
                else current_import_id
            )
            payload = build_d365_payload(table_name, record, import_id=import_id)
            write_rows.append(
                {
                    "record_index": record_index,
                    "operation": "PATCH" if record_id else "POST",
                    "record_id": record_id,
                    "record": record,
                    "payload": payload,
                    "match_value": row.get("match_value"),
                    "lookup_values": row.get("lookup_values"),
                    "lookup_display": lookup_display,
                }
            )

        write_row_by_index = {row["record_index"]: row for row in write_rows}
        write_chunks = self.batch.split_for_write(write_rows)
        write_results = self.batch.run_chunks(
            write_chunks,
            lambda chunk: self.batch.execute_write_chunk(
                table_name=table_name,
                table_config=table_config,
                chunk=chunk,
            ),
        )

        for result_chunk in write_results:
            for result in result_chunk:
                record_index = result["record_index"]
                operation = result["operation"]
                source_row = write_row_by_index.get(record_index)
                lookup_display = result.get("lookup_display") or (source_row.get("lookup_display") if source_row else None)
                if result["outcome"] == "success":
                    chunk_logs.append(
                        f"[crm-json-transform] record {record_index}: {operation} {table_config.entity_set} by {lookup_display}"
                    )
                    self.logger.write(
                        event_type="record_upsert",
                        table_name=table_name,
                        record_index=record_index,
                        outcome="success",
                        operation=operation,
                        entity_set=table_config.entity_set,
                        match_field=table_config.match_field,
                        match_value=lookup_display,
                        mode="batch",
                    )
                    continue

                error_text = result.get("error") or "batch operation failed"
                if operation == "PATCH" and source_row is not None:
                    status_code = result.get("status_code")
                    if self._is_missing_record_http_error(status_code, error_text):
                        try:
                            fallback_operation, _ = self._recover_missing_patch_write(
                                table_name=table_name,
                                table_config=table_config,
                                source_row=source_row,
                                current_import_id=current_import_id,
                            )
                        except Exception as fallback_exc:
                            error_text = f"{error_text}; fallback failed: {type(fallback_exc).__name__}: {fallback_exc}"
                        else:
                            chunk_logs.append(
                                f"[crm-json-transform] record {record_index}: {fallback_operation} {table_config.entity_set} by {lookup_display} (fallback)"
                            )
                            self.logger.write(
                                event_type="record_upsert",
                                table_name=table_name,
                                record_index=record_index,
                                outcome="success",
                                operation=fallback_operation,
                                entity_set=table_config.entity_set,
                                match_field=table_config.match_field,
                                match_value=lookup_display,
                                mode="batch",
                                message="fallback after missing PATCH target",
                            )
                            continue
                chunk_failures.append(f"record {record_index}: {error_text}")
                self.logger.write(
                    event_type="record_upsert",
                    table_name=table_name,
                    record_index=record_index,
                    outcome="failure",
                    operation=operation,
                    entity_set=table_config.entity_set,
                    match_field=table_config.match_field,
                    match_value=lookup_display,
                    mode="batch",
                    error=error_text,
                )

        return chunk_logs, chunk_failures

    def _resolve_payment_item_entitlement_ids(
        self,
        *,
        records: list[dict[str, Any]],
    ) -> dict[str, str]:
        config = getattr(self, "config", None)
        if config is None or "payment" not in getattr(config, "tables", {}):
            return {}
        payment_table = config.tables["payment"]
        unique_order_ids: list[str] = []
        seen_order_ids: set[str] = set()
        for record in records:
            order_id, _, _ = self._payment_item_source_parts(record)
            if order_id and order_id not in seen_order_ids:
                seen_order_ids.add(order_id)
                unique_order_ids.append(order_id)

        entitlement_ids: dict[str, str] = {}
        if not unique_order_ids:
            return entitlement_ids

        lookup_rows = [
            {"record_index": index, "match_value": order_id, "lookup_values": {"jh_orderid": order_id}}
            for index, order_id in enumerate(unique_order_ids, start=1)
        ]
        for chunk in chunked(lookup_rows, self._batch_flow_chunk_size()):
            lookup_results = self.batch.lookup_existing_ids(
                table_name="payment",
                table_config=payment_table,
                chunk=chunk,
            )
            for record_index, result in lookup_results.items():
                order_id = unique_order_ids[record_index - 1]
                record_id = result.get("record_id")
                if record_id:
                    entitlement_ids[order_id] = record_id
        return entitlement_ids

    def _upsert_payment_item_records_batch(
        self,
        table_name: str,
        table_config: D365TableConfig,
        records: list[dict[str, Any]],
    ) -> list[str]:
        logs: list[str] = []
        failures: list[str] = []
        current_import_id = self._current_import_id()
        seen_source_keys: dict[tuple[str, str], int] = {}
        payload_rows: list[dict[str, Any]] = []

        for record_index, record in enumerate(records, start=1):
            order_id, sequence, source_display = self._payment_item_source_parts(record)
            if not order_id or not sequence:
                error_text = f"missing orderhdr_id or order_item_seq for {source_display}"
                failures.append(f"record {record_index}: {error_text}")
                self.logger.write(
                    event_type="record_upsert",
                    table_name=table_name,
                    record_index=record_index,
                    outcome="failure",
                    error=error_text,
                    mode="batch",
                )
                continue

            source_key = (order_id, sequence)
            first_seen_index = seen_source_keys.get(source_key)
            if first_seen_index is not None:
                message = f"duplicate {source_display} (first seen at record {first_seen_index})"
                logs.append(f"[crm-json-transform] record {record_index}: skipped duplicate {source_display} (first seen at record {first_seen_index})")
                self.logger.write(
                    event_type="record_upsert",
                    table_name=table_name,
                    record_index=record_index,
                    outcome="skipped",
                    message=message,
                    match_field=table_config.match_field,
                    match_value=source_display,
                    mode="batch",
                )
                continue
            seen_source_keys[source_key] = record_index
            payload_rows.append(
                {
                    "record_index": record_index,
                    "record": record,
                    "order_id": order_id,
                    "sequence": sequence,
                    "source_display": source_display,
                    "match_value": sequence,
                }
            )

        flow_chunk_size = self._batch_flow_chunk_size()
        for source_chunk in chunked(payload_rows, flow_chunk_size):
            if source_chunk:
                first_index = source_chunk[0]["record_index"]
                last_index = source_chunk[-1]["record_index"]
                logs.append(f"[crm-json-transform] payment item entitlement lookup phase: records {first_index}-{last_index}")
            entitlement_ids = self._resolve_payment_item_entitlement_ids(records=[row["record"] for row in source_chunk])
            lookup_rows: list[dict[str, Any]] = []
            for row in source_chunk:
                order_id, _, _ = self._payment_item_source_parts(row["record"])
                entitlement_id = entitlement_ids.get(order_id)
                if entitlement_id:
                    row["entitlement_record_id"] = entitlement_id
                    row["lookup_values"] = {
                        "_jh_entitlementid_value": entitlement_id,
                        "jh_name": self._payment_lookup_name(row["record"]),
                    }
                    row["lookup_display"] = self._lookup_display(row["lookup_values"])
                    lookup_rows.append(row)
                else:
                    error_text = (
                        f"entitlement lookup returned no records for jh_orderid={order_id or '(empty)'} "
                        f"(orderhdr_id={row['order_id'] or '(empty)'} and order_item_seq={row['sequence'] or '(empty)'})"
                    )
                    failures.append(f"record {row['record_index']}: {error_text}")
                    self.logger.write(
                        event_type="record_upsert",
                        table_name=table_name,
                        record_index=row["record_index"],
                        outcome="failure",
                        error=error_text,
                        mode="batch",
                    )
                    row["skip_write"] = True
                    row["lookup_values"] = None
                    row["lookup_display"] = row["source_display"]
            existing_ids: dict[int, str] = {}
            existing_import_ids: dict[int, str | None] = {}
            if lookup_rows:
                lookup_results = self.batch.lookup_existing_ids(
                    table_name=table_name,
                    table_config=table_config,
                    chunk=lookup_rows,
                )
                for record_index, result in lookup_results.items():
                    existing_ids[record_index] = result["record_id"]
                    existing_import_ids[record_index] = result.get("import_id")

            write_rows: list[dict[str, Any]] = []
            for row in source_chunk:
                if row.get("skip_write"):
                    continue
                record_index = row["record_index"]
                record = row["record"]
                lookup_display = row["lookup_display"]
                record_id = existing_ids.get(record_index)
                import_id = self._merge_import_id(existing_import_ids.get(record_index)) if record_id else current_import_id
                entitlement_record_id = row.get("entitlement_record_id")
                payload_record = self._payment_item_payload_record(record, entitlement_record_id) if entitlement_record_id else record
                payload = build_d365_payload(table_name, payload_record, import_id=import_id)
                write_rows.append(
                    {
                        "record_index": record_index,
                        "operation": "PATCH" if record_id else "POST",
                        "record_id": record_id,
                        "record": record,
                        "payload": payload,
                        "match_value": row.get("match_value"),
                        "lookup_values": row.get("lookup_values"),
                        "lookup_display": lookup_display,
                    }
                )

            if write_rows:
                first_index = write_rows[0]["record_index"]
                last_index = write_rows[-1]["record_index"]
                logs.append(f"[crm-json-transform] payment item entitlement upsert phase: records {first_index}-{last_index}")
            write_row_by_index = {row["record_index"]: row for row in write_rows}
            write_chunks = self.batch.split_for_write(write_rows)
            write_results = self.batch.run_chunks(
                write_chunks,
                lambda chunk: self.batch.execute_write_chunk(
                    table_name=table_name,
                    table_config=table_config,
                    chunk=chunk,
                ),
            )

            for result_chunk in write_results:
                for result in result_chunk:
                    record_index = result["record_index"]
                    operation = result["operation"]
                    source_row = write_row_by_index.get(record_index)
                    lookup_display = result.get("lookup_display") or (source_row.get("lookup_display") if source_row else None)
                    if result["outcome"] == "success":
                        logs.append(f"[crm-json-transform] record {record_index}: {operation} {table_config.entity_set} by {lookup_display}")
                        self.logger.write(
                            event_type="record_upsert",
                            table_name=table_name,
                            record_index=record_index,
                            outcome="success",
                            operation=operation,
                            entity_set=table_config.entity_set,
                            match_field=table_config.match_field,
                            match_value=lookup_display,
                            mode="batch",
                        )
                    else:
                        error_text = result.get("error") or "batch operation failed"
                        if operation == "PATCH" and source_row is not None:
                            status_code = result.get("status_code")
                            if self._is_missing_record_http_error(status_code, error_text):
                                try:
                                    fallback_operation, _ = self._recover_missing_patch_write(
                                        table_name=table_name,
                                        table_config=table_config,
                                        source_row=source_row,
                                        current_import_id=current_import_id,
                                    )
                                except Exception as fallback_exc:
                                    error_text = f"{error_text}; fallback failed: {type(fallback_exc).__name__}: {fallback_exc}"
                                else:
                                    logs.append(f"[crm-json-transform] record {record_index}: {fallback_operation} {table_config.entity_set} by {lookup_display} (fallback)")
                                    self.logger.write(
                                        event_type="record_upsert",
                                        table_name=table_name,
                                        record_index=record_index,
                                        outcome="success",
                                        operation=fallback_operation,
                                        entity_set=table_config.entity_set,
                                        match_field=table_config.match_field,
                                        match_value=lookup_display,
                                        mode="batch",
                                        message="fallback after missing PATCH target",
                                    )
                                    continue
                        failures.append(f"record {record_index}: {error_text}")
                        self.logger.write(
                            event_type="record_upsert",
                            table_name=table_name,
                            record_index=record_index,
                            outcome="failure",
                            operation=operation,
                            entity_set=table_config.entity_set,
                            match_field=table_config.match_field,
                            match_value=lookup_display,
                            mode="batch",
                            error=error_text,
                        )

        if failures:
            raise ValueError("Batch upsert failed for " + "; ".join(failures))
        return logs

    def _is_missing_record_http_error(self, status_code: int | None, error_text: str) -> bool:
        if status_code == 404:
            return True
        normalized = error_text.lower()
        return "does not exist" in normalized or "not found" in normalized

    def _recover_missing_patch_write(
        self,
        *,
        table_name: str,
        table_config: D365TableConfig,
        source_row: dict[str, Any],
        current_import_id: str,
    ) -> tuple[str, dict[str, Any]]:
        record_index = source_row["record_index"]
        record = source_row["record"]
        lookup_values = source_row["lookup_values"]
        existing_id, existing_import_id = self._find_existing_record_compat(
            table_name,
            record_index,
            table_config,
            lookup_values=lookup_values,
        )
        if existing_id:
            payload = build_d365_payload(
                table_name,
                record,
                import_id=self._merge_import_id(existing_import_id),
            )
            self._patch_record(table_name, record_index, table_config.entity_set, existing_id, payload)
            return "PATCH", payload
        payload = build_d365_payload(table_name, record, import_id=current_import_id)
        self._post_record(table_name, record_index, table_config.entity_set, payload)
        return "POST", payload

    def upsert_table_records(self, table_name: str, sanitized_payload: Any) -> list[str]:
        get_table_mapping(table_name)
        if table_name not in self.config.tables:
            raise ValueError(f"Table '{table_name}' is not configured for D365 push.")
        table_config = self.config.tables[table_name]
        records = get_sanitized_records(sanitized_payload)
        mode = "batch" if self.config.batch.enabled else "row"
        self.logger.write(
            event_type="push_start",
            table_name=table_name,
            outcome="success",
            record_count=len(records),
            mode=mode,
            batch_size=self.config.batch.batch_size,
            batch_parallel=self.config.batch.parallel,
            batch_max_workers=self.config.batch.max_workers,
        )
        try:
            if table_name == "payment_item":
                logs = (
                    self._upsert_payment_item_records_batch(table_name, table_config, records)
                    if self.config.batch.enabled
                    else self._upsert_payment_item_records_rowwise(table_name, table_config, records)
                )
            else:
                logs = (
                    self._upsert_table_records_batch(table_name, table_config, records)
                    if self.config.batch.enabled
                    else self._upsert_table_records_rowwise(table_name, table_config, records)
                )
        except Exception as exc:
            self.logger.write(
                event_type="push_complete",
                table_name=table_name,
                outcome="failure",
                record_count=len(records),
                mode=mode,
                error=str(exc),
            )
            raise
        self.logger.write(
            event_type="push_complete",
            table_name=table_name,
            outcome="success",
            record_count=len(records),
            mode=mode,
        )
        return logs

    def _upsert_payment_item_records_rowwise(
        self,
        table_name: str,
        table_config: D365TableConfig,
        records: list[dict[str, Any]],
    ) -> list[str]:
        logs: list[str] = []
        failures: list[str] = []
        current_import_id = self._current_import_id()
        seen_source_keys: dict[tuple[str, str], int] = {}
        payment_table = self.config.tables.get("payment") if hasattr(self, "config") else None

        for record_index, record in enumerate(records, start=1):
            try:
                order_id, sequence, source_display = self._payment_item_source_parts(record)
                if not order_id or not sequence:
                    error_text = f"missing orderhdr_id or order_item_seq for {source_display}"
                    failures.append(f"record {record_index}: {error_text}")
                    self.logger.write(
                        event_type="record_upsert",
                        table_name=table_name,
                        record_index=record_index,
                        outcome="failure",
                        error=error_text,
                        mode="row",
                    )
                    continue

                source_key = (order_id, sequence)
                first_seen_index = seen_source_keys.get(source_key)
                if first_seen_index is not None:
                    logs.append(f"[crm-json-transform] record {record_index}: skipped duplicate {source_display} (first seen at record {first_seen_index})")
                    self.logger.write(
                        event_type="record_upsert",
                        table_name=table_name,
                        record_index=record_index,
                        outcome="skipped",
                        message=f"duplicate {source_display} (first seen at record {first_seen_index})",
                        match_field=table_config.match_field,
                        match_value=source_display,
                        mode="row",
                    )
                    continue
                seen_source_keys[source_key] = record_index

                entitlement_lookup_name = self._payment_lookup_name(record)
                entitlement_id = None
                if payment_table is not None:
                    entitlement_id, _ = self._find_existing_record_compat(
                        "payment",
                        record_index,
                        payment_table,
                        lookup_values={"jh_name": entitlement_lookup_name},
                    )
                if not entitlement_id:
                    error_text = (
                        f"entitlement lookup returned no records for jh_name={entitlement_lookup_name or '(empty)'} "
                        f"(orderhdr_id={order_id or '(empty)'} and order_item_seq={sequence or '(empty)'})"
                    )
                    failures.append(f"record {record_index}: {error_text}")
                    self.logger.write(
                        event_type="record_upsert",
                        table_name=table_name,
                        record_index=record_index,
                        outcome="failure",
                        error=error_text,
                        mode="row",
                    )
                    continue

                existing_id, existing_import_id = self._find_existing_record_compat(
                    table_name,
                    record_index,
                    table_config,
                    lookup_values={
                        "_jh_entitlementid_value": entitlement_id,
                        "jh_name": self._payment_lookup_name(record),
                    },
                )

                import_id = self._merge_import_id(existing_import_id) if existing_id else current_import_id
                payload = build_d365_payload(
                    table_name,
                    self._payment_item_payload_record(record, entitlement_id),
                    import_id=import_id,
                )
                if existing_id:
                    self._patch_record(table_name, record_index, table_config.entity_set, existing_id, payload)
                    operation = "PATCH"
                else:
                    self._post_record(table_name, record_index, table_config.entity_set, payload)
                    operation = "POST"
                logs.append(f"[crm-json-transform] record {record_index}: {operation} {table_config.entity_set} by {source_display}")
                self.logger.write(
                    event_type="record_upsert",
                    table_name=table_name,
                    record_index=record_index,
                    outcome="success",
                    operation=operation,
                    entity_set=table_config.entity_set,
                    match_field=table_config.match_field,
                    match_value=source_display,
                    mode="row",
                )
            except Exception as exc:
                error_text = str(exc)
                failures.append(f"record {record_index}: {error_text}")
                self.logger.write(
                    event_type="record_upsert",
                    table_name=table_name,
                    record_index=record_index,
                    outcome="failure",
                    error=error_text,
                    mode="row",
                )
                continue
        if failures:
            raise ValueError("Row-wise upsert failed for " + "; ".join(failures))
        return logs

    def _upsert_table_records_rowwise(
        self,
        table_name: str,
        table_config: D365TableConfig,
        records: list[dict[str, Any]],
    ) -> list[str]:
        if self._is_account_table(table_name):
            return self._upsert_account_table_records_rowwise(table_name, table_config, records)
        if table_name == "payment":
            return self._upsert_payment_table_records_rowwise(table_name, table_config, records)
        if table_name == "payment_item":
            return self._upsert_payment_item_records_rowwise(table_name, table_config, records)
        logs: list[str] = []
        failures: list[str] = []
        current_import_id = self._current_import_id()
        for record_index, record in enumerate(records, start=1):
            try:
                result = self._upsert_lookup_driven_row(
                    table_name=table_name,
                    table_config=table_config,
                    record_index=record_index,
                    record=record,
                    current_import_id=current_import_id,
                )
                if result is None:
                    logs.append(f"[crm-json-transform] record {record_index}: no D365 payload generated, skipped")
                    self._log_record_upsert(
                        table_name=table_name,
                        record_index=record_index,
                        outcome="skipped",
                        mode="row",
                        message="no D365 payload generated",
                    )
                    continue
                operation, lookup_display = result
                logs.append(
                    f"[crm-json-transform] record {record_index}: {operation} {table_config.entity_set} by {lookup_display}"
                )
                self._log_record_upsert(
                    table_name=table_name,
                    record_index=record_index,
                    outcome="success",
                    mode="row",
                    operation=operation,
                    entity_set=table_config.entity_set,
                    match_field=table_config.match_field,
                    lookup_display=lookup_display,
                )
            except Exception as exc:
                error_text = str(exc)
                failures.append(f"record {record_index}: {error_text}")
                self._log_record_upsert(
                    table_name=table_name,
                    record_index=record_index,
                    outcome="failure",
                    mode="row",
                    error=error_text,
                )
                continue
        if failures:
            raise ValueError("Row-wise upsert failed for " + "; ".join(failures))
        return logs

    def _upsert_account_table_records_rowwise(
        self,
        table_name: str,
        table_config: D365TableConfig,
        records: list[dict[str, Any]],
    ) -> list[str]:
        logs, deduped_rows = self._prepare_account_lookup_rows(
            table_name=table_name,
            table_config=table_config,
            records=records,
            current_import_id=self._current_import_id(),
            mode="row",
        )
        failures: list[str] = []
        current_import_id = self._current_import_id()
        for row in deduped_rows:
            record_index = row["record_index"]
            record = row["record"]
            lookup_display = row["lookup_display"]
            try:
                result = self._upsert_account_lookup_driven_row(
                    table_name=table_name,
                    table_config=table_config,
                    record_index=record_index,
                    record=record,
                    current_import_id=current_import_id,
                )
                if result is None:
                    logs.append(f"[crm-json-transform] record {record_index}: no D365 payload generated, skipped")
                    self._log_record_upsert(
                        table_name=table_name,
                        record_index=record_index,
                        outcome="skipped",
                        mode="row",
                        message="no D365 payload generated",
                    )
                    continue
                operation, _ = result
                logs.append(f"[crm-json-transform] record {record_index}: {operation} {table_config.entity_set} by {lookup_display}")
                self._log_record_upsert(
                    table_name=table_name,
                    record_index=record_index,
                    outcome="success",
                    mode="row",
                    operation=operation,
                    entity_set=table_config.entity_set,
                    match_field=table_config.match_field,
                    lookup_display=lookup_display,
                )
            except Exception as exc:
                error_text = str(exc)
                failures.append(f"record {record_index}: {error_text}")
                self._log_record_upsert(
                    table_name=table_name,
                    record_index=record_index,
                    outcome="failure",
                    mode="row",
                    error=error_text,
                )
        if failures:
            raise ValueError("Row-wise upsert failed for " + "; ".join(failures))
        return logs

    def _upsert_table_records_batch(
        self,
        table_name: str,
        table_config: D365TableConfig,
        records: list[dict[str, Any]],
    ) -> list[str]:
        if table_name == "payment":
            return self._upsert_payment_table_records_batch(table_name, table_config, records)
        if table_name == "payment_item":
            return self._upsert_payment_item_records_batch(table_name, table_config, records)
        current_import_id = self._current_import_id()
        logs, payload_rows = self._prepare_lookup_driven_batch_rows(
            table_name=table_name,
            table_config=table_config,
            records=records,
            current_import_id=current_import_id,
        )
        failures: list[str] = []
        flow_chunk_size = self._batch_flow_chunk_size()
        for source_chunk in chunked(payload_rows, flow_chunk_size):
            chunk_logs, chunk_failures = self._run_lookup_driven_batch_chunk(
                table_name=table_name,
                table_config=table_config,
                source_chunk=source_chunk,
                current_import_id=current_import_id,
            )
            logs.extend(chunk_logs)
            failures.extend(chunk_failures)
        if failures:
            raise ValueError("Batch upsert failed for " + "; ".join(failures))
        return logs

    def _upsert_payment_table_records_batch(
        self,
        table_name: str,
        table_config: D365TableConfig,
        records: list[dict[str, Any]],
    ) -> list[str]:
        logs: list[str] = []
        failures: list[str] = []
        current_import_id = self._current_import_id()
        payload_rows: list[dict[str, Any]] = []
        seen_source_keys: dict[tuple[str, str], int] = {}

        for record_index, record in enumerate(records, start=1):
            try:
                prepared_row, error_text = self._prepare_payment_batch_row(
                    table_name=table_name,
                    table_config=table_config,
                    record_index=record_index,
                    record=record,
                    current_import_id=current_import_id,
                    seen_source_keys=seen_source_keys,
                    mode="batch",
                )
                if prepared_row is not None:
                    payload_rows.append(prepared_row)
                elif error_text:
                    logs.append(error_text)
            except Exception as exc:
                error_text = str(exc)
                failures.append(f"record {record_index}: {error_text}")
                self._log_record_upsert(
                    table_name=table_name,
                    record_index=record_index,
                    outcome="failure",
                    mode="batch",
                    error=error_text,
                )

        flow_chunk_size = self._batch_flow_chunk_size()
        for source_chunk in chunked(payload_rows, flow_chunk_size):
            chunk_logs, chunk_failures = self._run_payment_batch_chunk(
                table_name=table_name,
                table_config=table_config,
                source_chunk=source_chunk,
                current_import_id=current_import_id,
            )
            logs.extend(chunk_logs)
            failures.extend(chunk_failures)

        if failures:
            raise ValueError("Batch upsert failed for " + "; ".join(failures))
        return logs

    def _run_payment_batch_chunk(
        self,
        *,
        table_name: str,
        table_config: D365TableConfig,
        source_chunk: list[dict[str, Any]],
        current_import_id: str,
    ) -> tuple[list[str], list[str]]:
        chunk_logs: list[str] = []
        chunk_failures: list[str] = []
        account_lookup_rows: list[dict[str, Any]] = []
        if source_chunk:
            first_index = source_chunk[0]["record_index"]
            last_index = source_chunk[-1]["record_index"]
            chunk_logs.append(f"[crm-json-transform] payment accounts lookup phase: records {first_index}-{last_index}")
        for row in source_chunk:
            record = row["record"]
            customer_id = _coerce_int_lookup_value(record.get("customer_id"))
            if customer_id is None:
                continue
            account_lookup_rows.append(
                {
                    "record_index": row["record_index"],
                    "match_value": customer_id,
                    "lookup_values": {"jh_thinkidnbr": customer_id},
                }
            )

        validated_chunk: list[dict[str, Any]] = []
        if account_lookup_rows:
            account_table_config = D365TableConfig(
                entity_set="accounts",
                match_field="jh_thinkidnbr",
                primary_id_field="accountid",
            )
            account_lookup_results = self.batch.lookup_existing_ids(
                table_name="customer",
                table_config=account_table_config,
                chunk=account_lookup_rows,
            )
            for row in source_chunk:
                record_index = row["record_index"]
                record = row["record"]
                customer_id = _coerce_int_lookup_value(record.get("customer_id"))
                if customer_id is not None:
                    if record_index not in account_lookup_results:
                        error_text = f"account with account_id={customer_id} does not exist"
                        chunk_failures.append(f"record {record_index}: {error_text}")
                        self._log_record_upsert(
                            table_name=table_name,
                            record_index=record_index,
                            outcome="failure",
                            mode="batch",
                            error=error_text,
                    )
                        continue
                validated_chunk.append(row)
        else:
            validated_chunk = list(source_chunk)

        if not validated_chunk:
            return chunk_logs, chunk_failures

        first_index = validated_chunk[0]["record_index"]
        last_index = validated_chunk[-1]["record_index"]
        chunk_logs.append(f"[crm-json-transform] payment entitlement lookup phase: records {first_index}-{last_index}")
        chunk_logs.append(f"[crm-json-transform] payment entitlement upsert phase: records {first_index}-{last_index}")
        lookup_chunk_logs, lookup_chunk_failures = self._run_lookup_driven_batch_chunk(
            table_name=table_name,
            table_config=table_config,
            source_chunk=validated_chunk,
            current_import_id=current_import_id,
        )
        chunk_logs.extend(lookup_chunk_logs)
        chunk_failures.extend(lookup_chunk_failures)
        return chunk_logs, chunk_failures

    def _upsert_payment_table_records_rowwise(
        self,
        table_name: str,
        table_config: D365TableConfig,
        records: list[dict[str, Any]],
    ) -> list[str]:
        logs: list[str] = []
        failures: list[str] = []
        current_import_id = self._current_import_id()
        seen_source_keys: dict[tuple[str, str], int] = {}

        for record_index, record in enumerate(records, start=1):
            try:
                order_id, sequence, source_display = self._payment_source_parts(record)
                if not order_id or not sequence:
                    error_text = f"missing orderhdr_id or order_item_seq for {source_display}"
                    failures.append(f"record {record_index}: {error_text}")
                    self._log_record_upsert(
                        table_name=table_name,
                        record_index=record_index,
                        outcome="failure",
                        mode="row",
                        error=error_text,
                    )
                    continue

                source_key = (order_id, sequence)
                first_seen_index = seen_source_keys.get(source_key)
                if first_seen_index is not None:
                    message = f"duplicate {source_display} (first seen at record {first_seen_index})"
                    logs.append(
                        f"[crm-json-transform] record {record_index}: skipped duplicate {source_display} "
                        f"(first seen at record {first_seen_index})"
                    )
                    self._log_record_upsert(
                        table_name=table_name,
                        record_index=record_index,
                        outcome="skipped",
                        mode="row",
                        message=message,
                        match_field=table_config.match_field,
                        lookup_display=source_display,
                    )
                    continue
                seen_source_keys[source_key] = record_index

                customer_id = record.get("customer_id")
                if customer_id not in {None, ""}:
                    self._payment_account_lookup_record_id(customer_id, record_index)

                base_payload = build_d365_payload(table_name, record, import_id=current_import_id)
                if not base_payload:
                    logs.append(f"[crm-json-transform] record {record_index}: no D365 payload generated, skipped")
                    self._log_record_upsert(
                        table_name=table_name,
                        record_index=record_index,
                        outcome="skipped",
                        mode="row",
                        message="no D365 payload generated",
                    )
                    continue

                lookup_values = self._lookup_values_for_payload(table_name, table_config, record, base_payload)
                lookup_display = self._lookup_display(lookup_values)
                existing_id, existing_import_id = self._find_existing_record_compat(
                    table_name,
                    record_index,
                    table_config,
                    lookup_values=lookup_values,
                )
                import_id = self._merge_import_id(existing_import_id) if existing_id else current_import_id
                payload = build_d365_payload(table_name, record, import_id=import_id)
                if existing_id:
                    try:
                        self._patch_record(table_name, record_index, table_config.entity_set, existing_id, payload)
                        operation = "PATCH"
                    except Exception as exc:
                        status_code = getattr(getattr(exc, "response", None), "status_code", None)
                        if not self._is_missing_record_http_error(status_code, str(exc)):
                            raise
                        fallback_operation, _ = self._recover_missing_patch_write(
                            table_name=table_name,
                            table_config=table_config,
                            source_row={
                                "record_index": record_index,
                                "record": record,
                                "lookup_values": lookup_values,
                            },
                            current_import_id=current_import_id,
                        )
                        operation = fallback_operation
                else:
                    self._post_record(table_name, record_index, table_config.entity_set, payload)
                    operation = "POST"
                logs.append(f"[crm-json-transform] record {record_index}: {operation} {table_config.entity_set} by {lookup_display}")
                self._log_record_upsert(
                    table_name=table_name,
                    record_index=record_index,
                    outcome="success",
                    mode="row",
                    operation=operation,
                    entity_set=table_config.entity_set,
                    match_field=table_config.match_field,
                    lookup_display=lookup_display,
                )
            except Exception as exc:
                error_text = str(exc)
                failures.append(f"record {record_index}: {error_text}")
                self._log_record_upsert(
                    table_name=table_name,
                    record_index=record_index,
                    outcome="failure",
                    mode="row",
                    error=error_text,
                )
        if failures:
            raise ValueError("Row-wise upsert failed for " + "; ".join(failures))
        return logs

    def _find_existing_record_compat(
        self,
        table_name: str,
        record_index: int,
        table_config: D365TableConfig,
        *,
        match_value: Any | None = None,
        lookup_values: dict[str, Any] | None = None,
    ) -> tuple[str | None, str | None]:
        try:
            return self._find_existing_record(
                table_name,
                record_index,
                table_config,
                match_value=match_value,
                lookup_values=lookup_values,
            )
        except TypeError as exc:
            if lookup_values is None or "lookup_values" not in str(exc):
                raise
            fallback_match_value = match_value
            if fallback_match_value in {None, ""} and lookup_values:
                fallback_match_value = next(iter(lookup_values.values()))
            return self._find_existing_record(table_name, record_index, table_config, fallback_match_value)

    def _find_existing_record(
        self,
        table_name: str,
        record_index: int,
        table_config: D365TableConfig,
        match_value: Any | None = None,
        lookup_values: dict[str, Any] | None = None,
    ) -> tuple[str | None, str | None]:
        lookup_values = lookup_values or {table_config.match_field: match_value}
        if any(value in {None, ""} for value in lookup_values.values()):
            return None, None
        lookup_fields = self._lookup_field_names(table_name, table_config)
        select_fields = [table_config.primary_id_field, *lookup_fields, "jh_importid"]
        response = self._request_with_retry(
            "get",
            self._entity_url(table_config.entity_set),
            request_label="GET existing record lookup",
            table_name=table_name,
            record_index=record_index,
            params={
                "$top": 2,
                "$select": ",".join(dict.fromkeys(select_fields)),
                "$filter": " and ".join(build_odata_filter(field_name, lookup_values[field_name]) for field_name in lookup_fields),
            },
            timeout=30,
        )
        items = response.json().get("value", [])
        if self._is_account_table(table_name):
            if len(items) > 1:
                lookup_display = self._lookup_display(lookup_values)
                raise ValueError(
                    f"record {record_index}: lookup for {table_config.entity_set} by {lookup_display} returned "
                    f"{len(items)} matches"
                )
            if len(items) == 0:
                return None, None
            item = items[0]
            if any(
                _normalize_lookup_key(item.get(field_name)) != _normalize_lookup_key(lookup_values.get(field_name))
                for field_name in lookup_fields
            ):
                lookup_display = self._lookup_display(lookup_values)
                raise ValueError(
                    f"record {record_index}: lookup for {table_config.entity_set} by {lookup_display} returned "
                    "a non-matching record"
                )
            lookup_result = _lookup_result_from_item(item, table_config.primary_id_field)
            if lookup_result is None:
                lookup_display = self._lookup_display(lookup_values)
                raise ValueError(
                    f"record {record_index}: lookup for {table_config.entity_set} by {lookup_display} "
                    "returned a record without a primary id"
                )
            return lookup_result["record_id"], lookup_result["import_id"]

        if not items:
            return None, None
        lookup_result = _lookup_result_from_item(items[0], table_config.primary_id_field)
        if lookup_result is None:
            return None, None
        return lookup_result["record_id"], lookup_result["import_id"]

    def _post_record(self, table_name: str, record_index: int, entity_set: str, payload: dict[str, Any]) -> None:
        self._request_with_retry(
            "post",
            self._entity_url(entity_set),
            request_label="POST create record",
            payload=payload,
            table_name=table_name,
            record_index=record_index,
            json=payload,
            timeout=30,
        )

    def _patch_record(
        self,
        table_name: str,
        record_index: int,
        entity_set: str,
        record_id: str,
        payload: dict[str, Any],
    ) -> None:
        self._request_with_retry(
            "patch",
            self._record_url(entity_set, record_id),
            request_label="PATCH update record",
            payload=payload,
            table_name=table_name,
            record_index=record_index,
            json=payload,
            timeout=30,
        )

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        request_label: str,
        payload: dict[str, Any] | None = None,
        table_name: str | None = None,
        record_index: int | None = None,
        **kwargs: Any,
    ) -> Any:
        retryable_exceptions = (
            self._requests.exceptions.ConnectionError,
            self._requests.exceptions.Timeout,
        )
        attempts = self._retry_attempts()
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            request = getattr(self.session, method)
            try:
                response = request(url, **kwargs)
                try:
                    response.raise_for_status()
                except self._requests.exceptions.HTTPError as exc:
                    body = response.text.strip()
                    status_code = getattr(response, "status_code", None)
                    retryable_status = self._is_retryable_http_status(status_code)
                    if not retryable_status or attempt >= attempts:
                        self.logger.write(
                            event_type="http_result",
                            table_name=table_name,
                            record_index=record_index,
                            outcome="failure",
                            request_label=request_label,
                            status_code=status_code,
                            method=method.upper(),
                            url=url,
                            payload=payload,
                            response_body=body if body else None,
                            error=str(exc),
                        )
                        if self.debug_http:
                            print(f"[crm-json-transform][debug] {request_label}", file=os.sys.stderr)
                            print(f"[crm-json-transform][debug] status={status_code}", file=os.sys.stderr)
                            print(f"[crm-json-transform][debug] method={method.upper()}", file=os.sys.stderr)
                            print(f"[crm-json-transform][debug] url={url}", file=os.sys.stderr)
                            if payload is not None:
                                print(f"[crm-json-transform][debug] payload={json.dumps(payload, ensure_ascii=True)}", file=os.sys.stderr)
                            if body:
                                print(f"[crm-json-transform][debug] response={body}", file=os.sys.stderr)
                        raise
                    if response.status_code == 401:
                        self._refresh_access_token()
                    delay_seconds = self._retry_delay_seconds(attempt)
                    self.logger.write(
                        event_type="http_retry",
                        table_name=table_name,
                        record_index=record_index,
                        outcome="retrying",
                        request_label=request_label,
                        attempt=attempt,
                        max_attempts=attempts,
                        method=method.upper(),
                        url=url,
                        retry_after_seconds=delay_seconds,
                        status_code=status_code,
                        reason=f"HTTP {status_code}",
                    )
                    if self.debug_http:
                        print(f"[crm-json-transform][debug] {request_label}", file=os.sys.stderr)
                        print(f"[crm-json-transform][debug] status={status_code}", file=os.sys.stderr)
                        if response.status_code == 401:
                            print("[crm-json-transform][debug] refreshing auth token and retrying", file=os.sys.stderr)
                        print(f"[crm-json-transform][debug] retry_after_seconds={delay_seconds:.3f}", file=os.sys.stderr)
                        print(f"[crm-json-transform][debug] method={method.upper()}", file=os.sys.stderr)
                        print(f"[crm-json-transform][debug] url={url}", file=os.sys.stderr)
                        if payload is not None:
                            print(f"[crm-json-transform][debug] payload={json.dumps(payload, ensure_ascii=True)}", file=os.sys.stderr)
                        if body:
                            print(f"[crm-json-transform][debug] response={body}", file=os.sys.stderr)
                    time.sleep(delay_seconds)
                    continue
                self.logger.write(
                    event_type="http_result",
                    table_name=table_name,
                    record_index=record_index,
                    outcome="success",
                    request_label=request_label,
                    status_code=response.status_code,
                    method=getattr(response.request, "method", None),
                    url=getattr(response.request, "url", None),
                )
                return response
            except retryable_exceptions as exc:
                last_exc = exc
                if attempt >= attempts:
                    self.logger.write(
                        event_type="http_result",
                        table_name=table_name,
                        record_index=record_index,
                        outcome="failure",
                        request_label=request_label,
                        method=method.upper(),
                        url=url,
                        exception_type=type(exc).__name__,
                        error=str(exc),
                    )
                    raise
                delay_seconds = self._retry_delay_seconds(attempt)
                self.logger.write(
                    event_type="http_retry",
                    table_name=table_name,
                    record_index=record_index,
                    outcome="retrying",
                    request_label=request_label,
                    attempt=attempt,
                    max_attempts=attempts,
                    method=method.upper(),
                    url=url,
                    retry_after_seconds=delay_seconds,
                    exception_type=type(exc).__name__,
                    error=str(exc),
                )
                time.sleep(delay_seconds)
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Request failed without an exception")

    def _raise_for_status(
        self,
        response: Any,
        *,
        request_label: str,
        payload: dict[str, Any] | None = None,
        table_name: str | None = None,
        record_index: int | None = None,
    ) -> None:
        try:
            response.raise_for_status()
            self.logger.write(
                event_type="http_result",
                table_name=table_name,
                record_index=record_index,
                outcome="success",
                request_label=request_label,
                status_code=response.status_code,
                method=getattr(response.request, "method", None),
                url=getattr(response.request, "url", None),
            )
        except self._requests.exceptions.HTTPError as exc:
            body = response.text.strip()
            self.logger.write(
                event_type="http_result",
                table_name=table_name,
                record_index=record_index,
                outcome="failure",
                request_label=request_label,
                status_code=response.status_code,
                method=getattr(response.request, "method", None),
                url=getattr(response.request, "url", None),
                payload=payload,
                response_body=body if body else None,
                error=str(exc),
            )
            if self.debug_http:
                print(f"[crm-json-transform][debug] {request_label}", file=os.sys.stderr)
                print(f"[crm-json-transform][debug] status={response.status_code}", file=os.sys.stderr)
                print(f"[crm-json-transform][debug] method={response.request.method}", file=os.sys.stderr)
                print(f"[crm-json-transform][debug] url={response.request.url}", file=os.sys.stderr)
                if payload is not None:
                    print(f"[crm-json-transform][debug] payload={json.dumps(payload, ensure_ascii=True)}", file=os.sys.stderr)
                if body:
                    print(f"[crm-json-transform][debug] response={body}", file=os.sys.stderr)
            raise

    def _entity_url(self, entity_set: str) -> str:
        return f"{self.config.resource_url}/api/data/{API_VERSION}/{entity_set}"

    def _record_url(self, entity_set: str, record_id: str) -> str:
        return f"{self._entity_url(entity_set)}({record_id})"

    def _batch_url(self) -> str:
        return f"{self.config.resource_url}/api/data/{API_VERSION}/$batch"
