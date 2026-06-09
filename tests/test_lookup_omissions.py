from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crm_json_converter.converter.payload import build_d365_payload
from crm_json_converter.converter.sanitize import sanitize_record
from crm_json_converter.converter.mappings import get_table_mapping


class LookupOmissionTests(unittest.TestCase):
    def test_customer_customer_id_is_emitted_as_jh_thinkidnbr(self) -> None:
        mapping = get_table_mapping("customer")
        field_map = {field.source_column: field for field in mapping.fields}
        errors: list[str] = []
        sanitized = sanitize_record(
            {
                "customer_id": 94,
                "company": "Example Company",
            },
            field_map,
            1,
            errors,
        )

        payload = build_d365_payload("customer", sanitized)

        self.assertEqual(errors, [])
        self.assertEqual(payload["jh_thinkidnbr"], 94)
        self.assertNotIn("jh_museid", payload)

    def test_payment_table_targets_entitlement(self) -> None:
        mapping = get_table_mapping("payment")
        self.assertEqual(mapping.target_entity, "jh_entitlement")

    def test_payment_fields_are_emitted(self) -> None:
        mapping = get_table_mapping("payment")
        field_map = {field.source_column: field for field in mapping.fields}
        errors: list[str] = []
        sanitized = sanitize_record(
            {
                "customer_id": 94,
                "orderhdr_id": 19555989,
                "order_item_seq": 1,
                "payment_amount": "240.3600",
                "payment_date": "Nov 28 2023 10:41:21:000AM",
            },
            field_map,
            1,
            errors,
        )

        payload = build_d365_payload("payment", sanitized)

        self.assertEqual(errors, [])
        self.assertEqual(payload["jh_accountid@odata.bind"], "/accounts(jh_thinkidnbr=94)")
        self.assertEqual(payload["jh_orderid"], "19555989")
        self.assertEqual(payload["jh_name"], "19555989:1")
        self.assertEqual(payload["jh_sequence"], 1)
        self.assertEqual(payload["jh_paidamt"], 240.36)
        self.assertEqual(payload["jh_lastpaymenton"], "2023-11-28T10:41:21")

    def test_payment_name_requires_both_fields_and_enforces_length(self) -> None:
        mapping = get_table_mapping("payment")
        field_map = {field.source_column: field for field in mapping.fields}

        missing_errors: list[str] = []
        sanitized_missing = sanitize_record(
            {
                "orderhdr_id": 19555989,
                "payment_amount": "240.3600",
                "payment_date": "Nov 28 2023 10:41:21:000AM",
            },
            field_map,
            1,
            missing_errors,
        )
        with self.assertRaises(ValueError) as missing_exc:
            build_d365_payload("payment", sanitized_missing)

        long_errors: list[str] = []
        sanitized_long = sanitize_record(
            {
                "orderhdr_id": "X" * 20,
                "order_item_seq": "9" * 90,
                "payment_amount": "240.3600",
                "payment_date": "Nov 28 2023 10:41:21:000AM",
            },
            field_map,
            2,
            long_errors,
        )
        with self.assertRaises(ValueError) as long_exc:
            build_d365_payload("payment", sanitized_long)

        self.assertEqual(missing_errors, [])
        self.assertEqual(long_errors, [])
        self.assertIn("build jh_name", str(missing_exc.exception))
        self.assertIn("exceeds 100 characters", str(long_exc.exception))

    def test_payment_skips_blank_or_missing_customer_lookup(self) -> None:
        mapping = get_table_mapping("payment")
        field_map = {field.source_column: field for field in mapping.fields}
        errors: list[str] = []
        sanitized_blank = sanitize_record(
            {
                "customer_id": "",
                "orderhdr_id": 19555989,
                "order_item_seq": 7,
                "payment_amount": "240.3600",
                "payment_date": "Nov 28 2023 10:41:21:000AM",
            },
            field_map,
            1,
            errors,
        )
        payload_blank = build_d365_payload("payment", sanitized_blank)

        sanitized_missing = sanitize_record(
            {
                "orderhdr_id": 19555989,
                "order_item_seq": 7,
                "payment_amount": "240.3600",
                "payment_date": "Nov 28 2023 10:41:21:000AM",
            },
            field_map,
            2,
            errors,
        )
        payload_missing = build_d365_payload("payment", sanitized_missing)

        self.assertEqual(errors, [])
        self.assertNotIn("jh_accountid@odata.bind", payload_blank)
        self.assertNotIn("jh_accountid@odata.bind", payload_missing)

    def test_payment_item_fields_are_emitted(self) -> None:
        mapping = get_table_mapping("payment_item")
        field_map = {field.source_column: field for field in mapping.fields}
        errors: list[str] = []
        sanitized = sanitize_record(
            {
                "oc_desc": "Project MUSE Premium Collection",
                "orderhdr_id": 20831552,
                "order_item_seq": 3,
                "description": " Example   item ",
                "order_status": 6,
                "payment_status": 1,
                "company": "Example Company",
                "_jh_entitlement_record_id": "edbb81d1-365b-f111-bec6-000d3a3428b3",
            },
            field_map,
            1,
            errors,
        )

        payload = build_d365_payload("payment_item", sanitized)

        self.assertEqual(errors, [])
        self.assertEqual(
            payload["jh_itemid_jh_collection@odata.bind"],
            "/jh_collections(jh_name='Project MUSE Premium Collection')",
        )
        self.assertEqual(
            payload["jh_entitlementid@odata.bind"],
            "/jh_entitlements(edbb81d1-365b-f111-bec6-000d3a3428b3)",
        )
        self.assertEqual(payload["jh_name"], "20831552:3")
        self.assertEqual(payload["jh_orderstatus"], 6)
        self.assertEqual(payload["jh_paymentstatus"], 1)
        self.assertEqual(payload["jh_sequence"], 3)
        self.assertEqual(
            field_map["orderhdr_id"].lookup_bind_key,
            "jh_orderid",
        )
        name_field = next(field for field in mapping.fields if field.crm_schema_name == "jh_name")
        self.assertIsNone(name_field.source_column)
        self.assertIn("Computed from orderhdr_id and order_item_seq", name_field.notes or "")

    def test_payment_item_choice_fields_reject_labels(self) -> None:
        mapping = get_table_mapping("payment_item")
        field_map = {field.source_column: field for field in mapping.fields}
        errors: list[str] = []
        sanitized = sanitize_record(
            {
                "orderhdr_id": 20831552,
                "order_item_seq": 3,
                "order_status": "Active / Shipping",
                "payment_status": "Paid - Overpayment",
                "_jh_entitlement_record_id": "edbb81d1-365b-f111-bec6-000d3a3428b3",
            },
            field_map,
            1,
            errors,
        )

        payload = build_d365_payload("payment_item", sanitized)

        self.assertEqual(
            errors,
            [
                "record 1 field 'order_status': invalid option value, set to null",
                "record 1 field 'payment_status': invalid option value, set to null",
            ],
        )
        self.assertNotIn("jh_orderstatus", payload)
        self.assertNotIn("jh_paymentstatus", payload)


if __name__ == "__main__":
    unittest.main()
