from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crm_json_converter.d365.batch import D365BatchRunner
from crm_json_converter.d365.client import D365Client
from crm_json_converter.d365.models import D365BatchConfig, D365Config, D365LogConfig, D365TableConfig
from crm_json_converter.d365.structured_log import StructuredLogger


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        content: bytes = b"",
        body: dict[str, object] | None = None,
        method: str = "POST",
        url: str = "https://example.test/$batch",
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = content
        self.headers = {"Content-Type": "multipart/mixed; boundary=batch_test"}
        self.request = type("Request", (), {"method": method, "url": url})()
        self._body = body or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            response._content = self.text.encode("utf-8")
            response.url = self.request.url
            response.request = requests.Request(method=self.request.method, url=self.request.url).prepare()
            raise requests.exceptions.HTTPError(f"{self.status_code} Error", response=response)

    def json(self) -> dict[str, object]:
        return self._body


class _SequenceSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls = 0

    def get(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls += 1
        value = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        return value


class _RefreshingSession:
    def __init__(self, responses: list[object]) -> None:
        self._responses = responses
        self.calls = 0

    def post(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls += 1
        value = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(value, Exception):
            raise value
        return value


class _FlakySession:
    def __init__(self, responses: list[object]) -> None:
        self._responses = responses
        self.calls = 0

    def post(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls += 1
        value = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(value, Exception):
            raise value
        return value


class _CollectorLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def write(self, **kwargs) -> None:  # noqa: ANN003
        self.events.append(kwargs)


def _build_runner(session: _FlakySession, retry_attempts: int = 3) -> tuple[D365BatchRunner, Path, tempfile.TemporaryDirectory]:
    temp_dir = tempfile.TemporaryDirectory()
    log_path = Path(temp_dir.name) / "batch.log"
    logger = StructuredLogger(D365LogConfig(log_path=str(log_path), log_dir=temp_dir.name))
    runner = D365BatchRunner(
        requests_module=requests,
        session_factory=lambda: session,
        batch_url="https://example.test/api/data/v9.2/$batch",
        entity_url=lambda entity_set: f"https://example.test/api/data/v9.2/{entity_set}",
        record_url=lambda entity_set, record_id: f"https://example.test/api/data/v9.2/{entity_set}({record_id})",
        config=D365BatchConfig(
            enabled=True,
            parallel=False,
            batch_size=50,
            max_workers=1,
            retry_attempts=retry_attempts,
        ),
        logger=logger,
        debug_http=False,
        auth_refresh=None,
    )
    return runner, log_path, temp_dir


class D365BatchRetryTests(unittest.TestCase):
    def test_batch_upsert_processes_lookup_and_write_per_chunk(self) -> None:
        class _Batch:
            def __init__(self) -> None:
                self.calls: list[tuple[str, list[int]]] = []

            def lookup_existing_ids(self, *, table_name, table_config, chunk):  # noqa: ANN001, ANN201
                self.calls.append(("lookup", [row["record_index"] for row in chunk]))
                if not chunk:
                    return {}
                first_row = chunk[0]
                if first_row["record_index"] == 1:
                    return {1: {"record_id": "abc", "import_id": "T20260529"}}
                return {}

            def split_for_write(self, rows):  # noqa: ANN001, ANN201
                self.calls.append(("split_for_write", [row["record_index"] for row in rows]))
                return [rows]

            def run_chunks(self, chunks, fn):  # noqa: ANN001, ANN201
                self.calls.append(("run_chunks", [row["record_index"] for row in chunks[0]] if chunks else []))
                return [fn(chunk) for chunk in chunks]

            def execute_write_chunk(self, *, table_name, table_config, chunk):  # noqa: ANN001, ANN201
                self.calls.append(("write", [row["record_index"] for row in chunk]))
                return [
                    {
                        "record_index": row["record_index"],
                        "operation": row["operation"],
                        "match_value": row.get("match_value"),
                        "outcome": "success",
                    }
                    for row in chunk
                ]

        client = D365Client.__new__(D365Client)
        client.batch = _Batch()  # type: ignore[attr-defined]
        client.logger = _CollectorLogger()  # type: ignore[attr-defined]
        client.config = D365Config(  # type: ignore[attr-defined]
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            resource_url="https://example.test",
            tables={
                "payment": D365TableConfig(
                    entity_set="jh_entitlements",
                    match_field="jh_name",
                    primary_id_field="jh_entitlementid",
                ),
                "payment_item": D365TableConfig(
                    entity_set="jh_entitlementitems",
                    match_field="jh_name",
                    primary_id_field="jh_entitlementitemid",
                    lookup_fields=("_jh_entitlementid_value", "jh_name"),
                ),
            },
            batch=D365BatchConfig(
                enabled=True,
                parallel=False,
                batch_size=50,
                max_workers=1,
                retry_attempts=3,
            ),
            logging=D365LogConfig(log_path=None, log_dir="logs"),
        )  # type: ignore[attr-defined]
        client._current_import_id = lambda: "T20260529"  # type: ignore[attr-defined]
        client._merge_import_id = lambda existing_import_id: "T20260529"  # type: ignore[attr-defined]
        client._batch_flow_chunk_size = lambda: 1  # type: ignore[attr-defined]

        table_config = type(
            "TableConfig",
            (),
            {"entity_set": "accounts", "match_field": "jh_thinkidnbr", "primary_id_field": "accountid"},
        )()
        records = [
            {"customer_id": 1001, "company": "First"},
            {"customer_id": "", "company": "No Lookup"},
            {"customer_id": 1002, "company": "Second"},
        ]

        with patch("crm_json_converter.d365.client.build_d365_payload") as build_payload:
            build_payload.side_effect = lambda table_name, record, import_id=None: {
                "customer_id": record.get("customer_id"),
                "name": record.get("company"),
                "import_id": import_id,
            }
            logs = client._upsert_table_records_batch("customer", table_config, records)

        self.assertEqual(
            client.batch.calls,  # type: ignore[attr-defined]
            [
                ("lookup", [1]),
                ("split_for_write", [1]),
                ("run_chunks", [1]),
                ("write", [1]),
                ("split_for_write", [2]),
                ("run_chunks", [2]),
                ("write", [2]),
                ("lookup", [3]),
                ("split_for_write", [3]),
                ("run_chunks", [3]),
                ("write", [3]),
            ],
        )
        self.assertEqual(logs, [
            "[crm-json-transform] record 1: PATCH accounts by jh_thinkidnbr=1001",
            "[crm-json-transform] record 2: POST accounts by jh_thinkidnbr=",
            "[crm-json-transform] record 3: POST accounts by jh_thinkidnbr=1002",
        ])
        self.assertTrue(
            all(
                event.get("mode") == "batch"
                for event in client.logger.events
                if event.get("event_type") == "record_upsert"
            )
        )

    def test_account_batch_upsert_keeps_latest_duplicate_row(self) -> None:
        class _Batch:
            def __init__(self) -> None:
                self.write_rows: list[dict[str, object]] = []

            def lookup_existing_ids(self, *, table_name, table_config, chunk):  # noqa: ANN001, ANN201
                return {}

            def split_for_write(self, rows):  # noqa: ANN001, ANN201
                return [rows]

            def run_chunks(self, chunks, fn):  # noqa: ANN001, ANN201
                return [fn(chunk) for chunk in chunks]

            def execute_write_chunk(self, *, table_name, table_config, chunk):  # noqa: ANN001, ANN201
                self.write_rows.extend(chunk)
                return [
                    {
                        "record_index": row["record_index"],
                        "operation": row["operation"],
                        "match_value": row.get("match_value"),
                        "outcome": "success",
                    }
                    for row in chunk
                ]

        client = D365Client.__new__(D365Client)
        client.batch = _Batch()  # type: ignore[attr-defined]
        client.logger = _CollectorLogger()  # type: ignore[attr-defined]
        client.config = D365Config(  # type: ignore[attr-defined]
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            resource_url="https://example.test",
            tables={
                "customer": D365TableConfig(
                    entity_set="accounts",
                    match_field="customer_id",
                    primary_id_field="accountid",
                ),
            },
            batch=D365BatchConfig(
                enabled=True,
                parallel=False,
                batch_size=50,
                max_workers=1,
                retry_attempts=3,
            ),
            logging=D365LogConfig(log_path=None, log_dir="logs"),
        )  # type: ignore[attr-defined]
        client._current_import_id = lambda: "T20260529"  # type: ignore[attr-defined]
        client._merge_import_id = lambda existing_import_id: "T20260529"  # type: ignore[attr-defined]
        client._batch_flow_chunk_size = lambda: 1  # type: ignore[attr-defined]

        table_config = type(
            "TableConfig",
            (),
            {"entity_set": "accounts", "match_field": "jh_thinkidnbr", "primary_id_field": "accountid"},
        )()
        records = [
            {"customer_id": 1001, "company": "Old First"},
            {"customer_id": 1002, "company": "Second"},
            {"customer_id": 1001, "company": "New First"},
        ]

        with patch("crm_json_converter.d365.client.build_d365_payload") as build_payload:
            build_payload.side_effect = lambda table_name, record, import_id=None: {
                "customer_id": record.get("customer_id"),
                "name": record.get("company"),
                "import_id": import_id,
            }
            logs = client._upsert_table_records_batch("customer", table_config, records)

        self.assertEqual([row["record_index"] for row in client.batch.write_rows], [2, 3])  # type: ignore[attr-defined]
        self.assertEqual([row["payload"]["name"] for row in client.batch.write_rows], ["Second", "New First"])  # type: ignore[attr-defined]
        self.assertIn(
            "[crm-json-transform] record 1: skipped duplicate jh_thinkidnbr=1001 (replaced by record 3)",
            logs,
        )
        self.assertNotIn(1, [row["record_index"] for row in client.batch.write_rows])  # type: ignore[attr-defined]

    def test_payment_item_batch_upsert_dedupes_by_composite_lookup_key(self) -> None:
        class _Batch:
            def __init__(self) -> None:
                self.calls: list[tuple[str, list[int]]] = []

            def lookup_existing_ids(self, *, table_name, table_config, chunk):  # noqa: ANN001, ANN201
                self.calls.append((f"lookup:{table_name}", [row["record_index"] for row in chunk]))
                if table_name == "entitlement":
                    results: dict[int, dict[str, str | None]] = {}
                    for row in chunk:
                        results[row["record_index"]] = {
                            "record_id": "ent-guid-1" if row["match_value"] == "ENT-1" else "ent-guid-2",
                            "import_id": "T20260529",
                        }
                    return results
                if table_name == "payment_item":
                    lookup_values = chunk[0]["lookup_values"]
                    entitlement_id = lookup_values["_jh_entitlementid_value"]
                    item_name = lookup_values["jh_name"]
                    record_id = f"item-{entitlement_id}-{item_name}"
                    return {chunk[0]["record_index"]: {"record_id": record_id, "import_id": "T20260529"}}
                return {}

            def split_for_write(self, rows):  # noqa: ANN001, ANN201
                self.calls.append(("split_for_write", [row["record_index"] for row in rows]))
                return [rows]

            def run_chunks(self, chunks, fn):  # noqa: ANN001, ANN201
                self.calls.append(("run_chunks", [row["record_index"] for row in chunks[0]] if chunks else []))
                return [fn(chunk) for chunk in chunks]

            def execute_write_chunk(self, *, table_name, table_config, chunk):  # noqa: ANN001, ANN201
                self.calls.append(("write", [row["record_index"] for row in chunk]))
                return [
                    {
                        "record_index": row["record_index"],
                        "operation": row["operation"],
                        "match_value": row.get("lookup_display"),
                        "lookup_display": row.get("lookup_display"),
                        "outcome": "success",
                    }
                    for row in chunk
                ]

        client = D365Client.__new__(D365Client)
        client.batch = _Batch()  # type: ignore[attr-defined]
        client.logger = _CollectorLogger()  # type: ignore[attr-defined]
        client.config = D365Config(  # type: ignore[attr-defined]
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            resource_url="https://example.test",
            tables={
                "entitlement": D365TableConfig(
                    entity_set="jh_entitlements",
                    match_field="jh_entitlementid",
                    primary_id_field="jh_entitlementid",
                ),
                "payment": D365TableConfig(
                    entity_set="jh_entitlements",
                    match_field="jh_name",
                    primary_id_field="jh_entitlementid",
                ),
                "payment_item": D365TableConfig(
                    entity_set="jh_entitlementitems",
                    match_field="jh_name",
                    primary_id_field="jh_entitlementitemid",
                    lookup_fields=("_jh_entitlementid_value", "jh_name"),
                ),
            },
            batch=D365BatchConfig(
                enabled=True,
                parallel=False,
                batch_size=50,
                max_workers=1,
                retry_attempts=3,
            ),
            logging=D365LogConfig(log_path=None, log_dir="logs"),
        )  # type: ignore[attr-defined]
        client._current_import_id = lambda: "T20260529"  # type: ignore[attr-defined]
        client._merge_import_id = lambda existing_import_id: "T20260529"  # type: ignore[attr-defined]
        client._batch_flow_chunk_size = lambda: 1  # type: ignore[attr-defined]

        table_config = type(
            "TableConfig",
            (),
            {
                "entity_set": "jh_entitlementitems",
                "match_field": "jh_name",
                "primary_id_field": "jh_entitlementitemid",
                "lookup_fields": ("_jh_entitlementid_value", "jh_name"),
            },
        )()
        records = [
            {"orderhdr_id": "ENT-1", "order_item_seq": 1, "start_date": "2026-06-01T00:00:00", "expire_date": "2026-12-31T00:00:00", "description": "  Alpha  "},
            {"orderhdr_id": "ENT-1", "order_item_seq": 1, "start_date": "2026-06-01T00:00:00", "expire_date": "2026-12-31T00:00:00", "description": "Alpha"},
            {"orderhdr_id": "ENT-2", "order_item_seq": 2, "start_date": "2026-06-01T00:00:00", "expire_date": "2026-12-31T00:00:00", "description": "Alpha"},
            {"orderhdr_id": "ENT-1", "order_item_seq": 3, "start_date": "2026-06-01T00:00:00", "expire_date": "2026-12-31T00:00:00", "description": "Beta"},
        ]

        with patch("crm_json_converter.d365.client.build_d365_payload") as build_payload:
            def _build_payload(table_name, record, import_id=None):  # noqa: ANN001, ANN201
                if table_name == "entitlement":
                    return {
                        "jh_entitlementid": record.get("orderhdr_id"),
                        "jh_starton": record.get("start_date"),
                        "jh_endon": record.get("expire_date"),
                        "jh_importid": import_id,
                    }
                payload = {
                    "jh_name": " ".join(str(record.get("description", "")).split()),
                    "jh_importid": import_id,
                }
                if table_name == "payment_item":
                    entitlement_record_id = record.get("_jh_entitlement_record_id")
                    if not entitlement_record_id:
                        raise AssertionError("missing entitlement record id")
                    payload["jh_entitlementid@odata.bind"] = f"/jh_entitlements({entitlement_record_id})"
                    payload["jh_sequence"] = record.get("order_item_seq")
                return payload

            build_payload.side_effect = _build_payload
            logs = client._upsert_table_records_batch("payment_item", table_config, records)

        self.assertEqual(
            client.batch.calls,  # type: ignore[attr-defined]
            [
                ("lookup:entitlement", [1]),
                ("split_for_write", [1]),
                ("run_chunks", [1]),
                ("write", [1]),
                ("lookup:entitlement", [2]),
                ("split_for_write", [2]),
                ("run_chunks", [2]),
                ("write", [2]),
                ("lookup:entitlement", [1]),
                ("lookup:entitlement", [2]),
                ("lookup:payment_item", [1]),
                ("split_for_write", [1]),
                ("run_chunks", [1]),
                ("write", [1]),
                ("lookup:payment_item", [3]),
                ("split_for_write", [3]),
                ("run_chunks", [3]),
                ("write", [3]),
                ("lookup:payment_item", [4]),
                ("split_for_write", [4]),
                ("run_chunks", [4]),
                ("write", [4]),
            ],
        )
        self.assertIn(
            "[crm-json-transform] record 2: skipped duplicate orderhdr_id=ENT-1 and order_item_seq=1 (first seen at record 1)",
            logs,
        )
        self.assertEqual(sum(1 for line in logs if "jh_entitlementitems by" in line), 3)
        self.assertTrue(any("record 1:" in line and "jh_entitlementitems by" in line for line in logs))
        self.assertTrue(any("record 3:" in line and "jh_entitlementitems by" in line for line in logs))
        self.assertTrue(any("record 4:" in line and "jh_entitlementitems by" in line for line in logs))

    def test_entitlement_batch_upsert_processes_lookup_and_chunk_writes(self) -> None:
        class _Batch:
            def __init__(self, events: list[tuple[str, list[int]]]) -> None:
                self.calls = events

            def lookup_existing_ids(self, *, table_name, table_config, chunk):  # noqa: ANN001, ANN201
                self.calls.append((f"lookup:{table_name}", [row["record_index"] for row in chunk]))
                if table_name == "entitlement" and chunk:
                    first_row = chunk[0]
                    return {
                        first_row["record_index"]: {
                            "record_id": f"ent-{first_row['match_value']}",
                            "import_id": "T20260529",
                        }
                    }
                return {}

            def split_for_write(self, rows):  # noqa: ANN001, ANN201
                self.calls.append(("split_for_write", [row["record_index"] for row in rows]))
                return [rows]

            def run_chunks(self, chunks, fn):  # noqa: ANN001, ANN201
                self.calls.append(("run_chunks", [row["record_index"] for row in chunks[0]] if chunks else []))
                return [fn(chunk) for chunk in chunks]

            def execute_write_chunk(self, *, table_name, table_config, chunk):  # noqa: ANN001, ANN201
                self.calls.append(("write", [row["record_index"] for row in chunk]))
                return [
                    {
                        "record_index": row["record_index"],
                        "operation": row["operation"],
                        "match_value": row.get("lookup_display"),
                        "lookup_display": row.get("lookup_display"),
                        "outcome": "success",
                    }
                    for row in chunk
                ]

        events: list[tuple[str, list[int]]] = []
        client = D365Client.__new__(D365Client)
        client.batch = _Batch(events)  # type: ignore[attr-defined]
        client.logger = _CollectorLogger()  # type: ignore[attr-defined]
        client.config = D365Config(  # type: ignore[attr-defined]
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            resource_url="https://example.test",
            tables={
                "entitlement": D365TableConfig(
                    entity_set="jh_entitlements",
                    match_field="jh_entitlementid",
                    primary_id_field="jh_entitlementid",
                ),
            },
            batch=D365BatchConfig(
                enabled=True,
                parallel=False,
                batch_size=50,
                max_workers=1,
                retry_attempts=3,
            ),
            logging=D365LogConfig(log_path=None, log_dir="logs"),
        )  # type: ignore[attr-defined]
        client._current_import_id = lambda: "T20260529"  # type: ignore[attr-defined]
        client._merge_import_id = lambda existing_import_id: "T20260529"  # type: ignore[attr-defined]
        client._batch_flow_chunk_size = lambda: 1  # type: ignore[attr-defined]

        table_config = D365TableConfig(
            entity_set="jh_entitlements",
            match_field="jh_entitlementid",
            primary_id_field="jh_entitlementid",
        )
        records = [
            {"orderhdr_id": "ENT-1", "start_date": "2026-06-01T00:00:00", "expire_date": "2026-12-31T00:00:00"},
            {"orderhdr_id": "ENT-2", "start_date": "2026-06-01T00:00:00", "expire_date": "2026-12-31T00:00:00"},
        ]

        with patch("crm_json_converter.d365.client.build_d365_payload") as build_payload:
            build_payload.side_effect = lambda table_name, record, import_id=None: {
                "jh_entitlementid": record.get("orderhdr_id"),
                "jh_starton": record.get("start_date"),
                "jh_endon": record.get("expire_date"),
                "jh_importid": import_id,
            }
            logs = client._upsert_table_records_batch("entitlement", table_config, records)

        self.assertEqual(
            events,
            [
                ("lookup:entitlement", [1]),
                ("split_for_write", [1]),
                ("run_chunks", [1]),
                ("write", [1]),
                ("lookup:entitlement", [2]),
                ("split_for_write", [2]),
                ("run_chunks", [2]),
                ("write", [2]),
            ],
        )
        self.assertTrue(any("record 1:" in line and "jh_entitlements by" in line for line in logs))
        self.assertTrue(any("record 2:" in line and "jh_entitlements by" in line for line in logs))

    def test_payment_item_rowwise_upsert_writes_and_skips_duplicates(self) -> None:
        client = D365Client.__new__(D365Client)
        class _Batch:
            def lookup_existing_ids(self, *, table_name, table_config, chunk):  # noqa: ANN001, ANN201
                if table_name != "entitlement":
                    return {}
                results: dict[int, dict[str, str | None]] = {}
                for row in chunk:
                    order_id = row["lookup_values"]["jh_entitlementid"]
                    results[row["record_index"]] = {
                        "record_id": "ent-guid-1" if order_id == "ENT-1" else "ent-guid-2",
                        "import_id": "T20260529",
                    }
                return results

        client.batch = _Batch()  # type: ignore[attr-defined]
        client.logger = _CollectorLogger()  # type: ignore[attr-defined]
        client.config = D365Config(  # type: ignore[attr-defined]
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            resource_url="https://example.test",
            tables={
                "entitlement": D365TableConfig(
                    entity_set="jh_entitlements",
                    match_field="jh_entitlementid",
                    primary_id_field="jh_entitlementid",
                ),
                "payment": D365TableConfig(
                    entity_set="jh_entitlements",
                    match_field="jh_name",
                    primary_id_field="jh_entitlementid",
                ),
                "payment_item": D365TableConfig(
                    entity_set="jh_entitlementitems",
                    match_field="jh_name",
                    primary_id_field="jh_entitlementitemid",
                    lookup_fields=("_jh_entitlementid_value", "jh_name"),
                ),
            },
            batch=D365BatchConfig(
                enabled=False,
                parallel=False,
                batch_size=50,
                max_workers=1,
                retry_attempts=3,
            ),
            logging=D365LogConfig(log_path=None, log_dir="logs"),
        )  # type: ignore[attr-defined]
        client._current_import_id = lambda: "T20260529"  # type: ignore[attr-defined]
        client._merge_import_id = lambda existing_import_id: "T20260529"  # type: ignore[attr-defined]

        table_config = D365TableConfig(
            entity_set="jh_entitlementitems",
            match_field="jh_name",
            primary_id_field="jh_entitlementitemid",
            lookup_fields=("_jh_entitlementid_value", "jh_name"),
        )
        records = [
            {"orderhdr_id": "ENT-1", "order_item_seq": 1, "start_date": "2026-06-01T00:00:00", "expire_date": "2026-12-31T00:00:00", "description": "  Alpha  "},
            {"orderhdr_id": "ENT-1", "order_item_seq": 1, "start_date": "2026-06-01T00:00:00", "expire_date": "2026-12-31T00:00:00", "description": "Alpha"},
            {"orderhdr_id": "ENT-2", "order_item_seq": 2, "start_date": "2026-06-01T00:00:00", "expire_date": "2026-12-31T00:00:00", "description": "Beta"},
        ]

        post_calls: list[tuple[str, int, dict[str, object]]] = []
        patch_calls: list[tuple[str, int, str, dict[str, object]]] = []
        lookup_calls: list[tuple[str, int, dict[str, object] | None]] = []

        def _find_existing_record_compat(table_name, record_index, table_config, *, match_value=None, lookup_values=None):  # noqa: ANN001, ANN201
            lookup_calls.append((table_name, record_index, lookup_values))
            if table_name == "entitlement":
                order_id = lookup_values["jh_entitlementid"] if lookup_values else match_value
                if order_id == "ENT-1":
                    return None, None
                if order_id == "ENT-2":
                    return None, None
            if table_name == "payment_item" and lookup_values:
                entitlement_id = lookup_values["_jh_entitlementid_value"]
                item_name = lookup_values["jh_name"]
                if entitlement_id == "ent-guid-1" and item_name == "ENT-1:1":
                    return "item-guid-1", "T20260529"
                if entitlement_id == "ent-guid-2" and item_name == "ENT-2:2":
                    return None, None
            return None, None

        client._find_existing_record_compat = _find_existing_record_compat  # type: ignore[attr-defined]
        client._patch_record = lambda table_name, record_index, entity_set, record_id, payload: patch_calls.append((table_name, record_index, record_id, payload))  # type: ignore[attr-defined]
        client._post_record = lambda table_name, record_index, entity_set, payload: post_calls.append((table_name, record_index, payload))  # type: ignore[attr-defined]

        with patch("crm_json_converter.d365.client.build_d365_payload") as build_payload:
            def _build_payload(table_name, record, import_id=None):  # noqa: ANN001, ANN201
                if table_name == "entitlement":
                    return {
                        "jh_entitlementid": record.get("orderhdr_id"),
                        "jh_starton": record.get("start_date"),
                        "jh_endon": record.get("expire_date"),
                        "jh_importid": import_id,
                    }
                payload = {
                    "jh_name": f"{record.get('orderhdr_id')}:{record.get('order_item_seq')}",
                    "jh_importid": import_id,
                }
                if table_name == "payment_item":
                    entitlement_record_id = record.get("_jh_entitlement_record_id")
                    if not entitlement_record_id:
                        raise AssertionError("missing entitlement record id")
                    payload["jh_entitlementid@odata.bind"] = f"/jh_entitlements({entitlement_record_id})"
                    payload["jh_sequence"] = record.get("order_item_seq")
                return payload

            build_payload.side_effect = _build_payload
            logs = client._upsert_payment_item_records_rowwise("payment_item", table_config, records)

        self.assertEqual(len(lookup_calls), 4)
        self.assertTrue(any(call[0] == "payment_item" and call[1] == 3 for call in post_calls))
        self.assertEqual(len([call for call in post_calls if call[0] == "payment_item"]), 1)
        self.assertEqual(len([call for call in patch_calls if call[0] == "payment_item"]), 1)
        self.assertIn("[crm-json-transform] record 2: skipped duplicate orderhdr_id=ENT-1 and order_item_seq=1 (first seen at record 1)", logs)
        self.assertTrue(any("record 1: PATCH jh_entitlementitems" in line for line in logs))
        self.assertTrue(any("record 3: POST jh_entitlementitems" in line for line in logs))

    def test_batch_upsert_falls_back_to_post_after_missing_patch_target(self) -> None:
        class _Batch:
            def lookup_existing_ids(self, *, table_name, table_config, chunk):  # noqa: ANN001, ANN201
                return {1: {"record_id": "stale-id", "import_id": "T20260529"}}

            def split_for_write(self, rows):  # noqa: ANN001, ANN201
                return [rows]

            def run_chunks(self, chunks, fn):  # noqa: ANN001, ANN201
                return [fn(chunk) for chunk in chunks]

            def execute_write_chunk(self, *, table_name, table_config, chunk):  # noqa: ANN001, ANN201
                return [
                    {
                        "record_index": 1,
                        "operation": "PATCH",
                        "match_value": 1001,
                        "outcome": "failure",
                        "status_code": 404,
                        "error": '{"error":{"code":"0x80060891","message":"A record with the specified key values does not exist in account entity"}}',
                    }
                ]

        client = D365Client.__new__(D365Client)
        client.batch = _Batch()  # type: ignore[attr-defined]
        client.logger = _CollectorLogger()  # type: ignore[attr-defined]
        client._current_import_id = lambda: "T20260529"  # type: ignore[attr-defined]
        client._merge_import_id = lambda existing_import_id: "T20260529"  # type: ignore[attr-defined]
        client._batch_flow_chunk_size = lambda: 1  # type: ignore[attr-defined]
        client._find_existing_record = lambda *args, **kwargs: (None, None)  # type: ignore[attr-defined]
        post_calls: list[tuple[int, dict[str, object]]] = []
        client._post_record = lambda table_name, record_index, entity_set, payload: post_calls.append((record_index, payload))  # type: ignore[attr-defined]
        client._patch_record = lambda *args, **kwargs: None  # type: ignore[attr-defined]

        table_config = type(
            "TableConfig",
            (),
            {"entity_set": "accounts", "match_field": "jh_thinkidnbr", "primary_id_field": "accountid"},
        )()
        records = [{"customer_id": 1001, "company": "First"}]

        with patch("crm_json_converter.d365.client.build_d365_payload") as build_payload:
            build_payload.side_effect = lambda table_name, record, import_id=None: {
                "customer_id": record.get("customer_id"),
                "name": record.get("company"),
                "import_id": import_id,
            }
            logs = client._upsert_table_records_batch("customer", table_config, records)

        self.assertEqual(post_calls, [(1, {"customer_id": 1001, "name": "First", "import_id": "T20260529"})])
        self.assertEqual(logs, ["[crm-json-transform] record 1: POST accounts by jh_thinkidnbr=1001 (fallback)"])
        self.assertTrue(any(event.get("outcome") == "success" and event.get("operation") == "POST" for event in client.logger.events))

    def test_entitlement_lookup_fails_when_identifier_is_missing(self) -> None:
        client = D365Client.__new__(D365Client)
        client._current_import_id = lambda: "T20260529"  # type: ignore[attr-defined]
        client._merge_import_id = lambda existing_import_id: "T20260529"  # type: ignore[attr-defined]
        client.logger = _CollectorLogger()  # type: ignore[attr-defined]
        client._post_record = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("post should not be called"))  # type: ignore[attr-defined]
        client._patch_record = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("patch should not be called"))  # type: ignore[attr-defined]

        table_config = D365TableConfig(
            entity_set="jh_entitlements",
            match_field="jh_entitlementid",
            primary_id_field="jh_entitlementid",
        )

        with self.assertRaises(ValueError) as exc:
            client._upsert_lookup_driven_row(  # type: ignore[attr-defined]
                table_name="entitlement",
                table_config=table_config,
                record_index=1,
                record={
                    "start_date": "2026-06-03T00:00:00",
                    "expire_date": "2026-12-31T00:00:00",
                },
                current_import_id="T20260529",
            )

        self.assertIn("entitlement requires orderhdr_id", str(exc.exception))

    def test_lookup_existing_ids_logs_once_per_chunk(self) -> None:
        multipart_body = (
            "--batch_test\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n"
            "Content-ID: 1\r\n"
            "\r\n"
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            "\r\n"
            '{"value":[{"accountid":"abc","jh_thinkidnbr":1001,"jh_importid":"T20260529"}]}\r\n'
            "--batch_test\r\n"
            "Content-Type: application/http\r\n"
            "Content-Transfer-Encoding: binary\r\n"
            "Content-ID: 2\r\n"
            "\r\n"
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            "\r\n"
            '{"value":[]}\r\n'
            "--batch_test--\r\n"
        ).encode("utf-8")

        session = _RefreshingSession(
            [
                _FakeResponse(
                    status_code=200,
                    content=multipart_body,
                    method="POST",
                    url="https://example.test/api/data/v9.2/$batch",
                )
            ]
        )
        logger = _CollectorLogger()
        runner = D365BatchRunner(
            requests_module=requests,
            session_factory=lambda: session,
            batch_url="https://example.test/api/data/v9.2/$batch",
            entity_url=lambda entity_set: f"https://example.test/api/data/v9.2/{entity_set}",
            record_url=lambda entity_set, record_id: f"https://example.test/api/data/v9.2/{entity_set}({record_id})",
            config=D365BatchConfig(
                enabled=True,
                parallel=False,
                batch_size=50,
                max_workers=1,
                retry_attempts=3,
            ),
            logger=logger,
            debug_http=False,
            auth_refresh=None,
        )

        results = runner.lookup_existing_ids(
            table_name="customer",
            table_config=type(
                "TableConfig",
                (),
                {"entity_set": "accounts", "match_field": "jh_thinkidnbr", "primary_id_field": "accountid"},
            )(),
            chunk=[
                {"record_index": 1, "match_value": 1001},
                {"record_index": 2, "match_value": 1002},
            ],
        )

        lookup_events = [event for event in logger.events if event.get("event_type") == "lookup_queued"]
        self.assertEqual(results, {1: {"record_id": "abc", "import_id": "T20260529"}})
        self.assertEqual(len(lookup_events), 1)
        self.assertEqual(lookup_events[0]["lookup_count"], 2)
        self.assertNotIn("record_index", lookup_events[0])

    def test_send_batch_retries_transient_connection_errors(self) -> None:
        session = _FlakySession(
            [
                requests.exceptions.ConnectionError("temporary dns failure"),
                requests.exceptions.ConnectionError("temporary dns failure"),
                _FakeResponse(),
            ]
        )
        runner, _, temp_dir = _build_runner(session, retry_attempts=3)
        self.addCleanup(temp_dir.cleanup)

        response = runner._send_batch(
            "batch_test",
            "payload",
            "customer",
            "BATCH upsert records",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(session.calls, 3)

    def test_execute_write_chunk_turns_exhausted_transport_failure_into_row_failures(self) -> None:
        session = _FlakySession(
            [
                requests.exceptions.ConnectionError("temporary dns failure"),
                requests.exceptions.ConnectionError("temporary dns failure"),
                requests.exceptions.ConnectionError("temporary dns failure"),
            ]
        )
        runner, _, temp_dir = _build_runner(session, retry_attempts=3)
        self.addCleanup(temp_dir.cleanup)

        results = runner.execute_write_chunk(
            table_name="customer",
            table_config=type(
                "TableConfig",
                (),
                {"entity_set": "accounts", "match_field": "customer_id", "primary_id_field": "accountid"},
            )(),
            chunk=[
                {"record_index": 1, "operation": "POST", "payload": {"name": "A"}, "match_value": 1001},
                {
                    "record_index": 2,
                    "operation": "PATCH",
                    "record_id": "00000000-0000-0000-0000-000000000002",
                    "payload": {"name": "B"},
                    "match_value": 1002,
                },
            ],
        )

        self.assertEqual(session.calls, 3)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["outcome"] == "failure" for result in results))
        self.assertTrue(all("ConnectionError" in result["error"] for result in results))

    def test_rowwise_upsert_continues_after_a_failed_record(self) -> None:
        client = D365Client.__new__(D365Client)
        client._current_import_id = lambda: "T20260526"  # type: ignore[attr-defined]
        client._merge_import_id = lambda existing_import_id: "T20260526"  # type: ignore[attr-defined]
        client.logger = _CollectorLogger()  # type: ignore[attr-defined]

        table_config = type(
            "TableConfig",
            (),
            {"entity_set": "accounts", "match_field": "jh_thinkidnbr", "primary_id_field": "accountid"},
        )()
        records = [
            {"customer_id": 1001, "company": "Broken Row"},
            {"customer_id": 1002, "company": "Good Row"},
        ]
        calls: list[str] = []

        def _find_existing_record(table_name, record_index, table_config, match_value):  # noqa: ANN001, ANN201
            if record_index == 1:
                raise ValueError("temporary lookup failure")
            return None, None

        def _post_record(table_name, record_index, entity_set, payload):  # noqa: ANN001, ANN201
            calls.append(f"post:{record_index}:{payload['name']}")

        client._find_existing_record = _find_existing_record  # type: ignore[attr-defined]
        client._post_record = _post_record  # type: ignore[attr-defined]
        client._patch_record = lambda *args, **kwargs: None  # type: ignore[attr-defined]

        with self.assertRaises(ValueError) as exc:
            client._upsert_table_records_rowwise("customer", table_config, records)

        self.assertIn("record 1: temporary lookup failure", str(exc.exception))
        self.assertEqual(calls, ["post:2:Good Row"])
        self.assertTrue(any(event.get("record_index") == 1 and event.get("outcome") == "failure" for event in client.logger.events))
        self.assertTrue(any(event.get("record_index") == 2 and event.get("outcome") == "success" for event in client.logger.events))
        self.assertTrue(
            all(
                event.get("mode") == "row"
                for event in client.logger.events
                if event.get("event_type") == "record_upsert"
            )
        )

    def test_find_existing_record_retries_after_unauthorized(self) -> None:
        class _Session:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
                self.calls += 1
                if self.calls == 1:
                    return _FakeResponse(
                        status_code=401,
                        text='{"error":"unauthorized"}',
                        method="GET",
                        url="https://example.test/api/data/v9.2/accounts",
                    )
                return _FakeResponse(
                    status_code=200,
                    body={"value": [{"accountid": "abc", "jh_thinkidnbr": 1001, "jh_importid": "T20260529"}]},
                    method="GET",
                    url="https://example.test/api/data/v9.2/accounts",
                )

        client = D365Client.__new__(D365Client)
        client.session = _Session()  # type: ignore[attr-defined]
        client._requests = requests  # type: ignore[attr-defined]
        client.config = type(  # type: ignore[attr-defined]
            "Config",
            (),
            {
                "batch": type(
                    "Batch",
                    (),
                    {
                        "retry_attempts": 10,
                        "request_timeout_seconds": 30,
                    },
                )(),
            },
        )()
        client._default_headers = {"Authorization": "Bearer old"}  # type: ignore[attr-defined]
        client._refresh_access_token = lambda: "new-token"  # type: ignore[attr-defined]
        client.logger = _CollectorLogger()  # type: ignore[attr-defined]
        client.debug_http = False  # type: ignore[attr-defined]
        client._entity_url = lambda entity_set: "https://example.test/api/data/v9.2/accounts"  # type: ignore[attr-defined]

        table_config = type(
            "TableConfig",
            (),
            {"entity_set": "accounts", "match_field": "jh_thinkidnbr", "primary_id_field": "accountid"},
        )()

        record_id, import_id = client._find_existing_record("customer", 1, table_config, 1001)

        self.assertEqual(record_id, "abc")
        self.assertEqual(import_id, "T20260529")
        self.assertEqual(client.session.calls, 2)

    def test_find_existing_record_does_not_retry_on_not_found(self) -> None:
        class _Session:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
                self.calls += 1
                return _FakeResponse(
                    status_code=404,
                    text='{"error":"not found"}',
                    method="GET",
                    url="https://example.test/api/data/v9.2/accounts",
                )

        client = D365Client.__new__(D365Client)
        session = _Session()
        client.session = session  # type: ignore[attr-defined]
        client._requests = requests  # type: ignore[attr-defined]
        client.config = type(  # type: ignore[attr-defined]
            "Config",
            (),
            {
                "batch": type(
                    "Batch",
                    (),
                    {
                        "retry_attempts": 10,
                        "request_timeout_seconds": 30,
                    },
                )(),
            },
        )()
        client._default_headers = {"Authorization": "Bearer old"}  # type: ignore[attr-defined]
        refresh_calls: list[str] = []
        client._refresh_access_token = lambda: refresh_calls.append("refresh") or "new-token"  # type: ignore[attr-defined]
        client.logger = _CollectorLogger()  # type: ignore[attr-defined]
        client.debug_http = False  # type: ignore[attr-defined]
        client._entity_url = lambda entity_set: "https://example.test/api/data/v9.2/accounts"  # type: ignore[attr-defined]

        table_config = type(
            "TableConfig",
            (),
            {"entity_set": "accounts", "match_field": "jh_thinkidnbr", "primary_id_field": "accountid"},
        )()

        with self.assertRaises(requests.exceptions.HTTPError):
            client._find_existing_record("customer", 1, table_config, 1001)

        self.assertEqual(session.calls, 1)
        self.assertEqual(refresh_calls, [])

    def test_find_existing_record_treats_empty_value_as_not_found(self) -> None:
        class _Session:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
                self.calls += 1
                return _FakeResponse(
                    status_code=200,
                    body={"value": []},
                    method="GET",
                    url="https://example.test/api/data/v9.2/accounts",
                )

        client = D365Client.__new__(D365Client)
        session = _Session()
        client.session = session  # type: ignore[attr-defined]
        client._requests = requests  # type: ignore[attr-defined]
        client.config = type(  # type: ignore[attr-defined]
            "Config",
            (),
            {
                "batch": type(
                    "Batch",
                    (),
                    {
                        "retry_attempts": 10,
                        "request_timeout_seconds": 30,
                    },
                )(),
            },
        )()
        client._default_headers = {"Authorization": "Bearer old"}  # type: ignore[attr-defined]
        refresh_calls: list[str] = []
        client._refresh_access_token = lambda: refresh_calls.append("refresh") or "new-token"  # type: ignore[attr-defined]
        client.logger = _CollectorLogger()  # type: ignore[attr-defined]
        client.debug_http = False  # type: ignore[attr-defined]
        client._entity_url = lambda entity_set: "https://example.test/api/data/v9.2/accounts"  # type: ignore[attr-defined]

        table_config = type(
            "TableConfig",
            (),
                {"entity_set": "accounts", "match_field": "jh_thinkidnbr", "primary_id_field": "accountid"},
        )()

        record_id, import_id = client._find_existing_record("customer", 1, table_config, 1001)

        self.assertIsNone(record_id)
        self.assertIsNone(import_id)
        self.assertEqual(session.calls, 1)
        self.assertEqual(refresh_calls, [])

    def test_find_existing_record_rejects_multiple_account_matches(self) -> None:
        class _Session:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
                self.calls += 1
                return _FakeResponse(
                    status_code=200,
                    body={
                        "value": [
                            {"accountid": "abc", "jh_thinkidnbr": 1001, "jh_importid": "T20260529"},
                            {"accountid": "def", "jh_thinkidnbr": 1001, "jh_importid": "T20260529"},
                        ]
                    },
                    method="GET",
                    url="https://example.test/api/data/v9.2/accounts",
                )

        client = D365Client.__new__(D365Client)
        session = _Session()
        client.session = session  # type: ignore[attr-defined]
        client._requests = requests  # type: ignore[attr-defined]
        client.config = type(  # type: ignore[attr-defined]
            "Config",
            (),
            {
                "batch": type(
                    "Batch",
                    (),
                    {
                        "retry_attempts": 10,
                        "request_timeout_seconds": 30,
                    },
                )(),
            },
        )()
        client._default_headers = {"Authorization": "Bearer old"}  # type: ignore[attr-defined]
        client._refresh_access_token = lambda: "new-token"  # type: ignore[attr-defined]
        client.logger = _CollectorLogger()  # type: ignore[attr-defined]
        client.debug_http = False  # type: ignore[attr-defined]
        client._entity_url = lambda entity_set: "https://example.test/api/data/v9.2/accounts"  # type: ignore[attr-defined]

        table_config = type(
            "TableConfig",
            (),
            {"entity_set": "accounts", "match_field": "jh_thinkidnbr", "primary_id_field": "accountid"},
        )()

        with self.assertRaises(ValueError) as exc:
            client._find_existing_record("customer", 1, table_config, 1001)

        self.assertIn("returned 2 matches", str(exc.exception))
        self.assertEqual(session.calls, 1)

    def test_send_batch_retries_after_unauthorized(self) -> None:
        response_401 = _FakeResponse(
            status_code=401,
            text='{"error":"unauthorized"}',
            method="POST",
            url="https://example.test/api/data/v9.2/$batch",
        )
        response_200 = _FakeResponse(
            status_code=200,
            method="POST",
            url="https://example.test/api/data/v9.2/$batch",
        )
        sessions = [_RefreshingSession([response_401]), _RefreshingSession([response_200])]

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_path = Path(temp_dir.name) / "batch.log"
        logger = StructuredLogger(D365LogConfig(log_path=str(log_path), log_dir=temp_dir.name))
        auth_refresh_calls: list[str] = []
        runner = D365BatchRunner(
            requests_module=requests,
            session_factory=lambda: sessions.pop(0),
            batch_url="https://example.test/api/data/v9.2/$batch",
            entity_url=lambda entity_set: f"https://example.test/api/data/v9.2/{entity_set}",
            record_url=lambda entity_set, record_id: f"https://example.test/api/data/v9.2/{entity_set}({record_id})",
            config=D365BatchConfig(
                enabled=True,
                parallel=False,
                batch_size=50,
                max_workers=1,
                retry_attempts=3,
            ),
            logger=logger,
            debug_http=False,
            auth_refresh=lambda: auth_refresh_calls.append("refresh"),
        )

        response = runner._send_batch("batch_test", "payload", "customer", "BATCH upsert records")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(auth_refresh_calls, ["refresh"])
        self.assertEqual(len(sessions), 0)

    def test_send_batch_does_not_retry_on_not_found(self) -> None:
        response_404 = _FakeResponse(
            status_code=404,
            text='{"error":"not found"}',
            method="POST",
            url="https://example.test/api/data/v9.2/$batch",
        )
        session = _RefreshingSession([response_404])

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_path = Path(temp_dir.name) / "batch.log"
        logger = StructuredLogger(D365LogConfig(log_path=str(log_path), log_dir=temp_dir.name))
        auth_refresh_calls: list[str] = []
        runner = D365BatchRunner(
            requests_module=requests,
            session_factory=lambda: session,
            batch_url="https://example.test/api/data/v9.2/$batch",
            entity_url=lambda entity_set: f"https://example.test/api/data/v9.2/{entity_set}",
            record_url=lambda entity_set, record_id: f"https://example.test/api/data/v9.2/{entity_set}({record_id})",
            config=D365BatchConfig(
                enabled=True,
                parallel=False,
                batch_size=50,
                max_workers=1,
                retry_attempts=3,
            ),
            logger=logger,
            debug_http=False,
            auth_refresh=lambda: auth_refresh_calls.append("refresh"),
        )

        with self.assertRaises(requests.exceptions.HTTPError):
            runner._send_batch("batch_test", "payload", "customer", "BATCH upsert records")

        self.assertEqual(session.calls, 1)
        self.assertEqual(auth_refresh_calls, [])

    def test_send_batch_backoff_grows_and_caps_at_ten_attempts(self) -> None:
        session = _FlakySession(
            [requests.exceptions.ConnectionError("temporary dns failure")] * 11
        )
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_path = Path(temp_dir.name) / "batch.log"
        logger = StructuredLogger(D365LogConfig(log_path=str(log_path), log_dir=temp_dir.name))
        runner = D365BatchRunner(
            requests_module=requests,
            session_factory=lambda: session,
            batch_url="https://example.test/api/data/v9.2/$batch",
            entity_url=lambda entity_set: f"https://example.test/api/data/v9.2/{entity_set}",
            record_url=lambda entity_set, record_id: f"https://example.test/api/data/v9.2/{entity_set}({record_id})",
            config=D365BatchConfig(
                enabled=True,
                parallel=False,
                batch_size=50,
                max_workers=1,
                retry_attempts=50,
            ),
            logger=logger,
            debug_http=False,
            auth_refresh=None,
        )

        with patch("crm_json_converter.d365.batch.time.sleep") as sleep_mock, patch(
            "crm_json_converter.d365.batch.random.uniform",
            return_value=0.0,
        ):
            with self.assertRaises(requests.exceptions.ConnectionError):
                runner._send_batch("batch_test", "payload", "customer", "BATCH upsert records")

        self.assertEqual(session.calls, 10)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0])

    def test_find_existing_record_backoff_grows_between_unauthorized_retries(self) -> None:
        class _Session:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
                self.calls += 1
                if self.calls < 3:
                    return _FakeResponse(
                        status_code=401,
                        text='{"error":"unauthorized"}',
                        method="GET",
                        url="https://example.test/api/data/v9.2/accounts",
                    )
                return _FakeResponse(
                    status_code=200,
                    body={"value": [{"accountid": "abc", "jh_thinkidnbr": 1001, "jh_importid": "T20260529"}]},
                    method="GET",
                    url="https://example.test/api/data/v9.2/accounts",
                )

        client = D365Client.__new__(D365Client)
        session = _Session()
        client.session = session  # type: ignore[attr-defined]
        client._requests = requests  # type: ignore[attr-defined]
        client.config = type(  # type: ignore[attr-defined]
            "Config",
            (),
            {
                "batch": type(
                    "Batch",
                    (),
                    {
                        "retry_attempts": 10,
                        "request_timeout_seconds": 30,
                    },
                )(),
            },
        )()
        client._default_headers = {"Authorization": "Bearer old"}  # type: ignore[attr-defined]
        refresh_calls: list[str] = []
        client._refresh_access_token = lambda: refresh_calls.append("refresh") or "new-token"  # type: ignore[attr-defined]
        client.logger = _CollectorLogger()  # type: ignore[attr-defined]
        client.debug_http = False  # type: ignore[attr-defined]
        client._entity_url = lambda entity_set: "https://example.test/api/data/v9.2/accounts"  # type: ignore[attr-defined]

        table_config = type(
            "TableConfig",
            (),
            {"entity_set": "accounts", "match_field": "jh_thinkidnbr", "primary_id_field": "accountid"},
        )()

        with patch("crm_json_converter.d365.client.time.sleep") as sleep_mock, patch(
            "crm_json_converter.d365.client.random.uniform",
            return_value=0.0,
        ):
            record_id, import_id = client._find_existing_record("customer", 1, table_config, 1001)

        self.assertEqual(record_id, "abc")
        self.assertEqual(import_id, "T20260529")
        self.assertEqual(session.calls, 3)
        self.assertEqual(refresh_calls, ["refresh", "refresh"])
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [0.5, 1.0])

if __name__ == "__main__":
    unittest.main()
