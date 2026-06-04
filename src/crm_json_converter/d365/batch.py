from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import random
import time
from typing import Any, Callable
from urllib.parse import quote, urlencode
import uuid

from .helpers import build_odata_filter, chunked, parse_batch_http_parts
from .models import D365BatchConfig, D365TableConfig
from .structured_log import StructuredLogger

_ACCOUNT_TABLE_NAMES = {"customer", "agency"}


def _lookup_result_from_item(item: dict[str, Any], primary_id_field: str) -> dict[str, str | None] | None:
    primary_id = item.get(primary_id_field)
    if not primary_id:
        return None
    return {
        "record_id": str(primary_id),
        "import_id": item.get("jh_importid"),
    }


def _normalize_lookup_key(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


class D365BatchRunner:
    def __init__(
        self,
        *,
        requests_module: Any,
        session_factory: Callable[[], Any],
        batch_url: str,
        entity_url: Callable[[str], str],
        record_url: Callable[[str, str], str],
        config: D365BatchConfig,
        logger: StructuredLogger,
        debug_http: bool,
        auth_refresh: Callable[[], None] | None = None,
    ) -> None:
        self._requests = requests_module
        self._session_factory = session_factory
        self._batch_url = batch_url
        self._entity_url = entity_url
        self._record_url = record_url
        self._config = config
        self._logger = logger
        self._debug_http = debug_http
        self._auth_refresh = auth_refresh

    def run_chunks(
        self,
        chunks: list[list[dict[str, Any]]],
        fn: Callable[[list[dict[str, Any]]], Any],
    ) -> list[Any]:
        if not chunks:
            return []
        if (not self._config.parallel) or self._config.max_workers <= 1 or len(chunks) == 1:
            return [fn(chunk) for chunk in chunks]
        results: list[Any] = [None] * len(chunks)
        worker_count = min(self._config.max_workers, len(chunks))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(fn, chunk): idx for idx, chunk in enumerate(chunks)}
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return results

    def split_for_lookup(self, rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        return chunked(rows, self._config.batch_size)

    def split_for_write(self, rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        return chunked(rows, self._config.batch_size)

    def _lookup_fields(self, table_config: D365TableConfig) -> tuple[str, ...]:
        lookup_fields = getattr(table_config, "lookup_fields", None)
        return lookup_fields or (table_config.match_field,)

    def _lookup_filter_expr(self, lookup_values: dict[str, Any]) -> str:
        parts: list[str] = []
        for field_name, value in lookup_values.items():
            parts.append(build_odata_filter(field_name, value))
        return " and ".join(parts)

    def lookup_existing_ids(
        self,
        *,
        table_name: str,
        table_config: D365TableConfig,
        chunk: list[dict[str, Any]],
    ) -> dict[int, dict[str, str | None]]:
        if not chunk:
            return {}
        batch_boundary = f"batch_{uuid.uuid4().hex}"
        lines: list[str] = []
        lookup_fields = self._lookup_fields(table_config)
        for content_id, row in enumerate(chunk, start=1):
            lookup_values = row.get("lookup_values") or {table_config.match_field: row["match_value"]}
            filter_expr = self._lookup_filter_expr(lookup_values)
            select_fields = [table_config.primary_id_field, *lookup_fields, "jh_importid"]
            select_expr = ",".join(dict.fromkeys(select_fields))
            query = urlencode(
                {"$top": 2, "$select": select_expr, "$filter": filter_expr},
                quote_via=quote,
                safe="(),'$",
            )
            lines.extend(
                [
                    f"--{batch_boundary}",
                    "Content-Type: application/http",
                    "Content-Transfer-Encoding: binary",
                    f"Content-ID: {content_id}",
                    "",
                    f"GET {self._entity_url(table_config.entity_set)}?{query} HTTP/1.1",
                    "Accept: application/json",
                    "",
                ]
            )
        first_record_index = chunk[0]["record_index"]
        last_record_index = chunk[-1]["record_index"]
        self._logger.write(
            event_type="lookup_queued",
            table_name=table_name,
            outcome="success",
            match_field=table_config.match_field,
            match_value=None,
            mode="batch",
            lookup_count=len(chunk),
            first_record_index=first_record_index,
            last_record_index=last_record_index,
        )
        lines.extend([f"--{batch_boundary}--", ""])
        response = self._send_batch(batch_boundary, "\r\n".join(lines), table_name, "BATCH lookup records")
        parts = parse_batch_http_parts(response.headers.get("Content-Type", ""), response.content)
        if len(parts) != len(chunk):
            raise ValueError(f"Unexpected lookup batch response count: expected {len(chunk)}, got {len(parts)}")
        results: dict[int, dict[str, str | None]] = {}
        for row, part in zip(chunk, parts):
            status_code = part["status_code"]
            body = part.get("body", "")
            record_index = row["record_index"]
            if status_code < 200 or status_code >= 300:
                raise ValueError(f"record {record_index}: lookup failed with status {status_code}: {body}")
            if not body.strip():
                continue
            data = json.loads(body)
            items = data.get("value", [])
            if table_name in _ACCOUNT_TABLE_NAMES:
                lookup_values = row.get("lookup_values") or {table_config.match_field: row["match_value"]}
                if len(items) > 1:
                    lookup_display = " and ".join(f"{field_name}={value}" for field_name, value in lookup_values.items())
                    raise ValueError(
                        f"record {record_index}: lookup for {table_config.entity_set} by {lookup_display} returned "
                        f"{len(items)} matches"
                    )
                if len(items) == 0:
                    continue
                item = items[0]
                if any(
                    _normalize_lookup_key(item.get(field_name)) != _normalize_lookup_key(lookup_values.get(field_name))
                    for field_name in lookup_fields
                ):
                    lookup_display = " and ".join(f"{field_name}={value}" for field_name, value in lookup_values.items())
                    raise ValueError(
                        f"record {record_index}: lookup for {table_config.entity_set} by {lookup_display} "
                        "returned a non-matching record"
                    )
                lookup_result = _lookup_result_from_item(item, table_config.primary_id_field)
                if lookup_result is None:
                    lookup_display = " and ".join(f"{field_name}={value}" for field_name, value in lookup_values.items())
                    raise ValueError(
                        f"record {record_index}: lookup for {table_config.entity_set} by {lookup_display} "
                        "returned a record without a primary id"
                    )
                results[record_index] = lookup_result
                continue
            if items:
                lookup_result = _lookup_result_from_item(items[0], table_config.primary_id_field)
                if lookup_result is not None:
                    results[record_index] = lookup_result
        return results

    def execute_write_chunk(
        self,
        *,
        table_name: str,
        table_config: D365TableConfig,
        chunk: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not chunk:
            return []
        batch_boundary = f"batch_{uuid.uuid4().hex}"
        changeset_boundary = f"changeset_{uuid.uuid4().hex}"
        lines: list[str] = [f"--{batch_boundary}", f"Content-Type: multipart/mixed; boundary={changeset_boundary}", ""]
        for content_id, row in enumerate(chunk, start=1):
            target_url = (
                self._record_url(table_config.entity_set, row["record_id"])
                if row["operation"] == "PATCH"
                else self._entity_url(table_config.entity_set)
            )
            lines.extend(
                [
                    f"--{changeset_boundary}",
                    "Content-Type: application/http",
                    "Content-Transfer-Encoding: binary",
                    f"Content-ID: {content_id}",
                    "",
                    f"{row['operation']} {target_url} HTTP/1.1",
                    "Content-Type: application/json",
                    "Accept: application/json",
                    "",
                    json.dumps(row["payload"], ensure_ascii=True),
                    "",
                ]
            )
        lines.extend([f"--{changeset_boundary}--", f"--{batch_boundary}--", ""])
        try:
            response = self._send_batch(batch_boundary, "\r\n".join(lines), table_name, "BATCH upsert records")
        except Exception as exc:
            return [
                {
                    "record_index": row["record_index"],
                    "operation": row["operation"],
                    "match_value": row.get("match_value"),
                    "outcome": "failure",
                    "status_code": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                for row in chunk
            ]
        parts = parse_batch_http_parts(response.headers.get("Content-Type", ""), response.content)
        if len(parts) != len(chunk):
            raise ValueError(f"Unexpected write batch response count: expected {len(chunk)}, got {len(parts)}")
        results: list[dict[str, Any]] = []
        for row, part in zip(chunk, parts):
            success = 200 <= part["status_code"] < 300
            results.append(
                {
                    "record_index": row["record_index"],
                    "operation": row["operation"],
                    "match_value": row.get("match_value"),
                    "lookup_display": row.get("lookup_display"),
                    "outcome": "success" if success else "failure",
                    "status_code": part["status_code"],
                    "error": None if success else part.get("body", "").strip(),
                }
            )
        return results

    def _send_batch(self, boundary: str, body: str, table_name: str, request_label: str) -> Any:
        attempts = min(max(1, self._config.retry_attempts), 10)
        retryable_exceptions = (
            self._requests.exceptions.ConnectionError,
            self._requests.exceptions.Timeout,
        )

        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            session = self._session_factory()
            try:
                response = session.post(
                    self._batch_url,
                    data=body.encode("utf-8"),
                    headers={"Content-Type": f"multipart/mixed; boundary={boundary}", "Accept": "application/json"},
                    timeout=self._config.request_timeout_seconds,
                )
                try:
                    response.raise_for_status()
                except self._requests.exceptions.HTTPError as exc:
                    body_text = response.text.strip() if response is not None else None
                    status_code = getattr(response, "status_code", None)
                    retryable_status = self._is_retryable_http_status(status_code)
                    if not retryable_status or attempt >= attempts:
                        self._logger.write(
                            event_type="http_result",
                            table_name=table_name,
                            outcome="failure",
                            request_label=request_label,
                            status_code=status_code,
                            method="POST",
                            url=self._batch_url,
                            mode="batch",
                            response_body=body_text if body_text else None,
                            error=str(exc),
                        )
                        if self._debug_http:
                            print(f"[crm-json-transform][debug] {request_label}", file=os.sys.stderr)
                            if response is not None:
                                print(f"[crm-json-transform][debug] status={response.status_code}", file=os.sys.stderr)
                            print("[crm-json-transform][debug] method=POST", file=os.sys.stderr)
                            print(f"[crm-json-transform][debug] url={self._batch_url}", file=os.sys.stderr)
                            if body_text:
                                print(f"[crm-json-transform][debug] response={body_text}", file=os.sys.stderr)
                        raise
                    if (
                        response is not None
                        and response.status_code == 401
                        and self._auth_refresh is not None
                    ):
                        self._auth_refresh()
                    delay_seconds = self._retry_delay_seconds(attempt)
                    self._logger.write(
                        event_type="http_retry",
                        table_name=table_name,
                        outcome="retrying",
                        request_label=request_label,
                        attempt=attempt,
                        max_attempts=attempts,
                        method="POST",
                        url=self._batch_url,
                        mode="batch",
                        retry_after_seconds=delay_seconds,
                        status_code=status_code,
                        reason=f"HTTP {status_code}",
                    )
                    if self._debug_http:
                        print(f"[crm-json-transform][debug] {request_label}", file=os.sys.stderr)
                        if response is not None:
                            print(f"[crm-json-transform][debug] status={response.status_code}", file=os.sys.stderr)
                        print("[crm-json-transform][debug] method=POST", file=os.sys.stderr)
                        print(f"[crm-json-transform][debug] url={self._batch_url}", file=os.sys.stderr)
                        if body_text:
                            print(f"[crm-json-transform][debug] response={body_text}", file=os.sys.stderr)
                        print(f"[crm-json-transform][debug] retry_after_seconds={delay_seconds:.3f}", file=os.sys.stderr)
                    time.sleep(delay_seconds)
                    continue
                self._logger.write(
                    event_type="http_result",
                    table_name=table_name,
                    outcome="success",
                    request_label=request_label,
                    status_code=response.status_code,
                    method="POST",
                    url=self._batch_url,
                    mode="batch",
                )
                return response
            except retryable_exceptions as exc:
                last_exc = exc
                if attempt >= attempts:
                    self._logger.write(
                        event_type="http_result",
                        table_name=table_name,
                        outcome="failure",
                        request_label=request_label,
                        method="POST",
                        url=self._batch_url,
                        mode="batch",
                        exception_type=type(exc).__name__,
                        error=str(exc),
                    )
                    if self._debug_http:
                        print(f"[crm-json-transform][debug] {request_label}", file=os.sys.stderr)
                        print(f"[crm-json-transform][debug] exception={type(exc).__name__}", file=os.sys.stderr)
                        print(f"[crm-json-transform][debug] attempts={attempts}", file=os.sys.stderr)
                        print("[crm-json-transform][debug] method=POST", file=os.sys.stderr)
                        print(f"[crm-json-transform][debug] url={self._batch_url}", file=os.sys.stderr)
                    raise
                delay_seconds = self._retry_delay_seconds(attempt)
                self._logger.write(
                    event_type="http_retry",
                    table_name=table_name,
                    outcome="retrying",
                    request_label=request_label,
                    attempt=attempt,
                    max_attempts=attempts,
                    method="POST",
                    url=self._batch_url,
                    mode="batch",
                    retry_after_seconds=delay_seconds,
                    exception_type=type(exc).__name__,
                    error=str(exc),
                )
                if self._debug_http:
                    print(f"[crm-json-transform][debug] {request_label}", file=os.sys.stderr)
                    print(f"[crm-json-transform][debug] attempt={attempt}/{attempts}", file=os.sys.stderr)
                    print(f"[crm-json-transform][debug] exception={type(exc).__name__}", file=os.sys.stderr)
                    print(f"[crm-json-transform][debug] method=POST", file=os.sys.stderr)
                    print(f"[crm-json-transform][debug] url={self._batch_url}", file=os.sys.stderr)
                    print(f"[crm-json-transform][debug] retry_after_seconds={delay_seconds:.3f}", file=os.sys.stderr)
                time.sleep(delay_seconds)
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Batch request failed without an exception")

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
